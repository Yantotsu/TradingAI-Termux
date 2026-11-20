#!/usr/bin/env python3
"""Indodax spot monitor with paper trading and explicitly gated live execution."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import shutil
import sys
import time
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from trading_core import Action, RiskLimits
from indodax_api import IndodaxAPI
from live_trader import LiveSafetyStop, LiveStore, _balance, _confirm_account, _execute, _rounded_quantity
from trained_model import fetch_candles, predict, train_from_exchange
from dashboard_server import start_dashboard_server

API_URL = "https://api.indodax.com"
LEGACY_PUBLIC_URL = "https://indodax.com"
DB_PATH = Path(__file__).resolve().parent / "data" / "trading.sqlite3"


@dataclass(frozen=True)
class Market:
    symbol: str
    last: float
    bid: float
    ask: float
    quote_volume: float
    spread_pct: float
    trade_age_seconds: float


def get_json(url: str) -> object:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "TradingAI-Termux/0.2"})
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_market(symbol: str) -> Market:
    query = urlencode({"symbol": symbol.upper()})
    payload = get_json(f"{API_URL}/api/v2/ticker/24hr?{query}")
    if not isinstance(payload, dict) or payload.get("code"):
        raise RuntimeError(f"Indodax API error: {payload}")
    last, bid, ask = (float(payload[k]) for k in ("lastPrice", "bidPrice", "askPrice"))
    volume = float(payload.get("quoteVolume", 0))
    if (not all(math.isfinite(value) for value in (last, bid, ask, volume))
            or min(last, bid, ask) <= 0 or volume < 0 or ask < bid):
        raise RuntimeError("Ticker memberi harga bid/ask yang tidak valid")
    trades = get_json(f"{API_URL}/api/v2/trades?{urlencode({'symbol': symbol.upper(), 'limit': 1})}")
    if not isinstance(trades, list) or not trades:
        raise RuntimeError("Indodax tidak mengembalikan transaksi terbaru")
    trade_time = float(trades[0]["time"]) / 1000
    age = max(0.0, time.time() - trade_time)
    if not math.isfinite(age):
        raise RuntimeError("Timestamp transaksi terbaru tidak valid")
    mid = (bid + ask) / 2
    return Market(symbol.upper(), last, bid, ask, volume, (ask - bid) / mid * 100, age)


def _enabled(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


def fetch_pair_rules(symbol: str) -> tuple[float, float, str, float]:
    """Return taker fee fraction and minimum quote value from official pair metadata."""
    pairs = get_json(f"{LEGACY_PUBLIC_URL}/api/pairs")
    if not isinstance(pairs, list):
        raise RuntimeError("Metadata pair Indodax tidak valid")
    item = next((p for p in pairs if p.get("symbol", "").upper() == symbol.upper()), None)
    if item is None:
        raise RuntimeError(f"Pair {symbol} tidak ditemukan di metadata Indodax")
    if _enabled(item.get("is_market_suspended")) or _enabled(item.get("is_maintenance")):
        raise RuntimeError(f"Pair {symbol} sedang maintenance atau dihentikan")
    fee_percent = item.get("trade_fee_percent_taker", item.get("trade_fee_percent"))
    minimum = item.get("trade_min_base_currency")
    increment = item.get("quantity_increment")
    minimum_quantity = item.get("trade_min_traded_currency")
    if fee_percent is None or minimum is None or increment is None or minimum_quantity is None:
        raise RuntimeError(f"Aturan fee/minimum/quantity untuk {symbol} tidak tersedia")
    fee_rate, min_order, min_qty = float(fee_percent) / 100, float(minimum), float(minimum_quantity)
    step = float(increment)
    if (not all(math.isfinite(v) for v in (fee_rate, min_order, min_qty, step))
            or fee_rate < 0 or min_order <= 0 or min_qty <= 0 or step <= 0):
        raise RuntimeError(f"Aturan fee/minimum/quantity untuk {symbol} tidak valid")
    return fee_rate, min_order, str(increment), min_qty


def resolve_symbol(requested: str, max_spread_pct: float, reselect: bool) -> str:
    """Select the most liquid IDR pair from the small default universe.

    Auto selection ranks BTCIDR/ETHIDR by 24h quote volume after a spread filter,
    then persists the choice so restarts continue managing the same paper position.
    """
    if requested.upper() != "AUTO":
        return requested.upper()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    saved = db.execute("SELECT value FROM bot_meta WHERE key='auto_symbol'").fetchone()
    if saved and not reselect:
        db.close()
        return str(saved[0])
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if saved and reselect:
        for table in ("paper_accounts", "live_positions"):
            if table in tables:
                position = db.execute(f"SELECT quantity FROM {table} WHERE quantity>0 LIMIT 1").fetchone()
                if position:
                    db.close()
                    raise RuntimeError("Pair auto tidak dapat diganti saat posisi masih terbuka")
    if not saved and "paper_accounts" in tables:
        open_symbols = [row[0] for row in db.execute(
            "SELECT symbol FROM paper_accounts WHERE quantity > 0 ORDER BY symbol")]
        all_symbols = [row[0] for row in db.execute("SELECT symbol FROM paper_accounts ORDER BY symbol")]
        if open_symbols and reselect:
            db.close()
            raise RuntimeError("Pair auto tidak dapat dipilih ulang saat posisi paper masih terbuka")
        existing = open_symbols if open_symbols else all_symbols
        if len(existing) == 1 and not reselect:
            db.execute("INSERT OR IGNORE INTO bot_meta(key,value) VALUES('auto_symbol',?)", (existing[0],))
            db.commit()
            db.close()
            return str(existing[0])
        if len(existing) > 1 and not reselect:
            db.close()
            raise RuntimeError("Database memiliki beberapa pair paper; pilih --symbol secara eksplisit")
    db.close()
    candidates = []
    failures = []
    for pair in ("BTCIDR", "ETHIDR"):
        try:
            market = fetch_market(pair)
            if market.spread_pct <= max_spread_pct and market.quote_volume > 0 and market.trade_age_seconds <= 300:
                candidates.append(market)
        except Exception as exc:
            failures.append(f"{pair}: {exc}")
    if not candidates:
        raise RuntimeError("Tidak ada pair kandidat yang lolos spread/volume. " + "; ".join(failures))
    selected = max(candidates, key=lambda item: item.quote_volume)
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT INTO bot_meta(key,value) VALUES('auto_symbol',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (selected.symbol,))
    db.commit()
    db.close()
    print(f"Auto-pick memilih {selected.symbol}: volume 24 jam Rp{selected.quote_volume:,.0f}, "
          f"spread {selected.spread_pct:.3f}%")
    return selected.symbol


class PaperStore:
    def __init__(self, symbol: str, initial_idr: float) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.symbol = symbol.upper()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL, price REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS samples_symbol_id ON samples(symbol, id);
            CREATE TABLE IF NOT EXISTS paper_accounts (
                symbol TEXT PRIMARY KEY, cash_idr REAL NOT NULL,
                quantity REAL NOT NULL DEFAULT 0, entry_price REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS daily_marks (
                symbol TEXT NOT NULL, day TEXT NOT NULL, start_equity REAL NOT NULL,
                PRIMARY KEY(symbol, day)
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL,
                side TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL,
                value_idr REAL NOT NULL, fee_idr REAL NOT NULL,
                pnl_idr REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL
            );
        """)
        trade_columns = {row[1] for row in self.db.execute("PRAGMA table_info(paper_trades)")}
        if "fee_idr" not in trade_columns:
            self.db.execute("ALTER TABLE paper_trades ADD COLUMN fee_idr REAL NOT NULL DEFAULT 0")
        self.db.execute("INSERT OR IGNORE INTO paper_accounts(symbol,cash_idr) VALUES(?,?)",
                        (self.symbol, initial_idr))
        self.db.commit()

    def account(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM paper_accounts WHERE symbol=?", (self.symbol,)).fetchone()

    def add_sample(self, price: float) -> None:
        self.db.execute("INSERT INTO samples(symbol,at,price) VALUES(?,?,?)",
                        (self.symbol, datetime.now(timezone.utc).isoformat(), price))
        self.db.commit()

    def trim_samples(self, max_age_seconds: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        self.db.execute("DELETE FROM samples WHERE symbol=? AND at<?", (self.symbol, cutoff))
        self.db.commit()

    def closes(self, limit: int = 200) -> list[float]:
        rows = self.db.execute("SELECT price FROM samples WHERE symbol=? ORDER BY id DESC LIMIT ?",
                               (self.symbol, limit)).fetchall()
        return [float(row["price"]) for row in reversed(rows)]

    def daily_pnl(self, liquidation_price: float, loss_limit_fraction: float) -> tuple[bool, float]:
        account = self.account()
        equity = account["cash_idr"] + account["quantity"] * liquidation_price
        day = datetime.now().astimezone().date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO daily_marks(symbol,day,start_equity) VALUES(?,?,?)",
                        (self.symbol, day, equity))
        self.db.commit()
        row = self.db.execute("SELECT start_equity FROM daily_marks WHERE symbol=? AND day=?",
                              (self.symbol, day)).fetchone()
        start = float(row["start_equity"])
        pnl = equity - start
        return pnl <= -(start * loss_limit_fraction), pnl

    def cooldown_remaining_minutes(self, cooldown_minutes: float) -> float:
        row = self.db.execute("SELECT at FROM paper_trades WHERE symbol=? AND side='SELL' ORDER BY id DESC LIMIT 1",
                              (self.symbol,)).fetchone()
        if row is None:
            return 0.0
        last_exit = datetime.fromisoformat(row["at"])
        elapsed = (datetime.now(timezone.utc) - last_exit).total_seconds() / 60
        return max(0.0, cooldown_minutes - elapsed)

    def trade(self, side: str, price: float, order_idr: float, fee_rate: float,
              reason: str) -> str:
        account = self.account()
        now = datetime.now(timezone.utc).isoformat()
        if side == "BUY":
            value = min(order_idr, account["cash_idr"])
            fee = value * fee_rate
            net_value = value - fee
            if net_value <= 0:
                return "SIMULASI: nilai order habis oleh fee"
            quantity = net_value / price
            new_qty = account["quantity"] + quantity
            entry = ((account["quantity"] * account["entry_price"]) + value) / new_qty
            self.db.execute("UPDATE paper_accounts SET cash_idr=cash_idr-?,quantity=?,entry_price=? WHERE symbol=?",
                            (value, new_qty, entry, self.symbol))
            pnl = 0.0
        else:
            quantity = account["quantity"]
            if quantity <= 0:
                return "SIMULASI: tidak ada posisi bot untuk dijual"
            value = quantity * price
            fee = value * fee_rate
            net_value = value - fee
            pnl = net_value - quantity * account["entry_price"]
            self.db.execute("UPDATE paper_accounts SET cash_idr=cash_idr+?,quantity=0,entry_price=0 WHERE symbol=?",
                            (net_value, self.symbol))
        self.db.execute("INSERT INTO paper_trades(symbol,at,side,price,quantity,value_idr,fee_idr,pnl_idr,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.symbol, now, side, price, quantity, value, fee, pnl, reason))
        self.db.commit()
        return (f"SIMULASI {side}: nilai Rp{value:,.0f}, fee Rp{fee:,.0f}, "
                f"jumlah {quantity:.8f}, P/L terealisasi Rp{pnl:,.0f}")

    def close(self) -> None:
        self.db.close()


def show_dashboard(*, symbol: str, mode: str, market: Market, action: Action,
                   score: float, validation: float, equity: float, daily_pnl: float,
                   quantity: float, entry_price: float, note: str, hit_daily_limit: bool,
                   spread_limit: float, stop_loss_pct: float) -> None:
    """Render a compact, screen-width-aware Termux metrics table."""
    interactive = sys.stdout.isatty()
    if interactive:
        sys.stdout.write("\033[2J\033[H")
    columns = shutil.get_terminal_size((60, 20)).columns
    width = max(38, min(columns, 68))
    inner = width - 4
    top = "┌" + "─" * (width - 2) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"

    def row(value: str) -> list[str]:
        wrapped = textwrap.wrap(value, width=inner, break_long_words=True, break_on_hyphens=False) or [""]
        return [f"│ {part:<{inner}} │" for part in wrapped]

    valid = validation > 0.5
    if hit_daily_limit:
        state = "BATAS HARIAN AKTIF — entry baru dihentikan"
    elif mode == "LIVE" and not valid:
        state = "LIVE TERKUNCI — validasi belum > 50%"
    elif mode == "SIMULASI" and not valid:
        state = "PAPER EKSPERIMEN — model belum lolos validasi"
    elif action == Action.BUY and quantity <= 0:
        state = "SINYAL BELI; menunggu filter risiko/order"
    elif action == Action.SELL and quantity > 0:
        state = "SINYAL JUAL; posisi bot akan ditutup"
    elif action == Action.HOLD:
        state = "MENUNGGU sinyal model"
    elif quantity > 0:
        state = "POSISI TERBUKA — menunggu jual atau stop-loss"
    else:
        state = "TIDAK ADA POSISI untuk dijual"

    stop_price = entry_price * (1 - stop_loss_pct / 100) if quantity > 0 and entry_price > 0 else 0.0
    threshold = ("BUY ≥ 0.20 | SELL ≤ 0.15 (eksperimen)" if mode == "SIMULASI"
                 else "BUY ≥ 0.60 + validasi > 0.50 | SELL ≤ 0.40")
    lines = [top]
    lines += row(f"TRADING AI  /  {symbol}  /  {mode}")
    lines.append("├" + "─" * (width - 2) + "┤")
    lines += row(f"Waktu    : {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines += row(f"Harga    : Rp{market.last:,.0f}")
    lines += row(f"Spread   : {market.spread_pct:.3f}%  (maks {spread_limit:.2f}%)")
    lines.append("├" + "─" * (width - 2) + "┤")
    lines += row(f"Sinyal   : {action.value}  |  Skor naik {score:.3f}")
    lines += row(f"Validasi : {validation:.1%}  |  Patokan > 50%")
    lines += row(f"Status   : {state}")
    lines += row(f"Ambang   : {threshold}")
    lines.append("├" + "─" * (width - 2) + "┤")
    lines += row(f"Ekuitas  : Rp{equity:,.0f}  |  P/L hari ini Rp{daily_pnl:,.0f}")
    if quantity > 0:
        base = symbol.removesuffix("IDR")
        lines += row(f"Posisi   : {quantity:.8f} {base}")
        lines += row(f"Harga masuk Rp{entry_price:,.0f}  |  Stop-loss Rp{stop_price:,.0f}")
    else:
        lines += row("Posisi   : kosong  |  Stop-loss: belum aktif")
    lines.append("├" + "─" * (width - 2) + "┤")
    lines += row(f"Kejadian : {note or 'Belum ada transaksi.'}")
    lines.append(bottom)
    print("\n".join(lines), flush=True)


def show_trade_animation(event: str, *, symbol: str, quantity: float, price: float) -> None:
    """Briefly animate a completed paper/live order or a blocked stop event."""
    if not event:
        return
    messages = {
        "BUY": f"BELI TERISI  {quantity:.8f} {symbol.removesuffix('IDR')}  @ Rp{price:,.0f}",
        "SELL": f"JUAL TERISI  {quantity:.8f} {symbol.removesuffix('IDR')}  @ Rp{price:,.0f}",
        "STOP": f"STOP LOSS — posisi dijual  {quantity:.8f} {symbol.removesuffix('IDR')}",
        "DAILY_STOP": f"BATAS RUGI HARIAN — posisi dijual  {quantity:.8f} {symbol.removesuffix('IDR')}",
        "STOP_BLOCKED": "STOP TERPICU — order belum bisa dijual (cek minimum pair)",
    }
    message = messages.get(event, event)
    if not sys.stdout.isatty():
        print(f">>> {message} <<<", flush=True)
        return
    color = "\033[32m" if event == "BUY" else "\033[31m"
    reset = "\033[0m"
    for marker in (">>>", " ✦ ", "<<<"):
        sys.stdout.write(f"\r{color}{marker} {message} {marker}{reset}")
        sys.stdout.flush()
        time.sleep(0.12)
    sys.stdout.write("\n")
    sys.stdout.flush()


def write_web_status(payload: dict[str, object]) -> None:
    status_path = DB_PATH.parent / "web_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = status_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(status_path)


def run(args: argparse.Namespace) -> None:
    if args.mode == "live" and args.live_confirm != "I_ACCEPT_REAL_TRADING_RISK":
        raise LiveSafetyStop("Live mode memerlukan --live-confirm I_ACCEPT_REAL_TRADING_RISK")
    symbol = resolve_symbol(args.symbol, args.max_spread_pct, args.reselect_auto)
    if not symbol.endswith("IDR") or len(symbol) <= 3:
        raise LiveSafetyStop("Versi ini hanya mendukung pair spot ber-quote IDR")
    fee_rate, minimum_order, quantity_increment, minimum_quantity = fetch_pair_rules(symbol)
    model_path = DB_PATH.parent / "models" / f"{symbol.lower()}_logistic.json"
    model = train_from_exchange(symbol, fee_rate=fee_rate, days=args.training_days, model_path=model_path)
    print(f"Model dilatih: {model['candle_count']} candle, {model['training_samples']} sampel latih, "
          f"akurasi berimbang validasi={model['validation_balanced_accuracy']:.1%} "
          f"(patokan acak 50%), horizon={model['horizon_candles']} jam | file={model_path}")
    api = None
    if args.mode == "live":
        try:
            api = IndodaxAPI(os.environ.get("INDODAX_API_KEY", ""),
                             os.environ.get("INDODAX_API_SECRET", ""))
        except ValueError as exc:
            raise LiveSafetyStop(str(exc)) from exc
        try:
            _confirm_account(api)
            if api.open_orders(symbol):
                raise LiveSafetyStop("Ada order pair ini yang masih terbuka. Selesaikan manual sebelum bot dimulai.")
        except LiveSafetyStop:
            raise
        except Exception as exc:
            raise LiveSafetyStop(f"Pemeriksaan awal API gagal: {exc}") from exc
        store = LiveStore(DB_PATH, symbol)
    else:
        store = PaperStore(symbol, args.initial_idr)
    limits = RiskLimits(args.max_order_pct / 100, args.max_position_pct / 100,
                        args.daily_loss_pct / 100)
    mode_label = "LIVE" if args.mode == "live" else "SIMULASI"
    print(f"Pantau {symbol} | {mode_label} | fee taker metadata={fee_rate * 100:.3f}% | "
          f"minimum order=Rp{minimum_order:,.0f} | jeda {args.interval}s")
    if args.mode == "live":
        print("Live aktif atas perintah lokal; API key hanya baca/trade, tidak boleh withdrawal.")
    print(f"Database lokal: {DB_PATH}")
    web_server = None
    if args.web:
        try:
            web_server = start_dashboard_server(DB_PATH.parent / "web_status.json", args.web_port)
        except OSError as exc:
            store.close()
            raise RuntimeError(f"Tidak bisa membuka website lokal di port {args.web_port}: {exc}") from exc
        print(f"Dashboard web lokal: http://127.0.0.1:{args.web_port}")
    event_id = int(time.time())
    try:
        while True:
            try:
                market = fetch_market(symbol)
                if market.trade_age_seconds > args.max_data_age_sec:
                    if args.mode == "live":
                        raise LiveSafetyStop("Data pasar stale; live bot dihentikan tanpa mengirim order")
                    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                          f"{symbol} dilewati: transaksi terakhir {market.trade_age_seconds:.0f}s lalu "
                          f"(batas {args.max_data_age_sec}s)", flush=True)
                    time.sleep(args.interval)
                    continue
                note = ""
                event = ""
                event_quantity = 0.0
                event_price = market.last
                if args.mode == "live":
                    assert api is not None
                    account_info = _confirm_account(api)
                    if api.open_orders(symbol):
                        raise LiveSafetyStop("Ada order terbuka di pair ini; live bot dihentikan untuk pemeriksaan")
                    position = store.position()
                    qty = float(position["quantity"])
                    base_free = _balance(account_info, symbol.removesuffix("IDR"), "free")
                    if qty > base_free + float(quantity_increment):
                        raise LiveSafetyStop("Saldo aset bebas lebih kecil dari posisi bot; rekonsiliasi manual diperlukan")
                    idr_free = _balance(account_info, "IDR", "free")
                    idr_locked = _balance(account_info, "IDR", "locked")
                    equity = idr_free + idr_locked + qty * market.bid
                    hit_limit, daily_pnl = store.daily_pnl(equity, limits.max_daily_loss_fraction)
                    if qty > 0 and hit_limit:
                        sell_qty = _rounded_quantity(min(qty, base_free), quantity_increment)
                        if Decimal(sell_qty) < Decimal(str(minimum_quantity)):
                            note = "Batas rugi harian tercapai, tetapi sisa posisi di bawah minimum jual pair"
                            event = "STOP_BLOCKED"
                        else:
                            note = _execute(api, store, "SELL", quantity=sell_qty)
                            event = "DAILY_STOP"
                            event_quantity, event_price = float(sell_qty), market.bid
                    elif qty > 0 and market.bid <= float(position["entry_price"]) * (1 - args.stop_loss_pct / 100):
                        sell_qty = _rounded_quantity(min(qty, base_free), quantity_increment)
                        if Decimal(sell_qty) < Decimal(str(minimum_quantity)):
                            note = "Stop loss aktif, tetapi sisa posisi di bawah minimum jual pair"
                            event = "STOP_BLOCKED"
                        else:
                            note = _execute(api, store, "SELL", quantity=sell_qty)
                            event = "STOP"
                            event_quantity, event_price = float(sell_qty), market.bid
                else:
                    account = store.account()
                    equity = account["cash_idr"] + account["quantity"] * market.bid
                    hit_limit, daily_pnl = store.daily_pnl(market.bid, limits.max_daily_loss_fraction)
                    if account["quantity"] > 0 and hit_limit:
                        event_quantity = float(account["quantity"])
                        event_price = market.bid
                        note = store.trade("SELL", market.bid, 0, fee_rate, "Batas rugi harian tercapai")
                        event = "DAILY_STOP"
                    elif account["quantity"] > 0 and market.bid <= account["entry_price"] * (1 - args.stop_loss_pct / 100):
                        event_quantity = float(account["quantity"])
                        event_price = market.bid
                        note = store.trade("SELL", market.bid, 0, fee_rate, "Stop loss")
                        event = "STOP"

                recent_candles = fetch_candles(symbol, int(time.time()) - 30 * 3600,
                                               int(time.time()), "60")
                is_paper = args.mode == "paper"
                prediction = predict(
                    model, recent_candles, allow_unvalidated=is_paper,
                    buy_threshold=0.20 if is_paper else 0.60,
                    sell_threshold=0.15 if is_paper else 0.40,
                )
                signal_action = Action[prediction.action]
                if prediction.validation_balanced_accuracy <= 0.5 and is_paper:
                    note = note or "Eksperimen simulasi saja: model belum unggul dari patokan acak"
                elif prediction.action == "HOLD" and prediction.validation_balanced_accuracy <= 0.5:
                    note = note or prediction.explanation
                if args.mode == "live":
                    assert api is not None
                    account_info = _confirm_account(api)
                    position = store.position()
                    qty = float(position["quantity"])
                    base_free = _balance(account_info, symbol.removesuffix("IDR"), "free")
                    idr_free = _balance(account_info, "IDR", "free")
                    idr_locked = _balance(account_info, "IDR", "locked")
                    equity = idr_free + idr_locked + qty * market.bid
                    position_value = qty * market.bid
                    order_cap = 0.0 if hit_limit else limits.allowed_order_value(
                        equity=equity, current_position_value=position_value, daily_pnl=daily_pnl
                    )
                    if signal_action == Action.BUY and qty <= 0:
                        cooldown_left = store.cooldown_remaining_minutes(args.cooldown_minutes)
                        if cooldown_left > 0:
                            note = f"BUY ditunda {cooldown_left:.0f} menit setelah penutupan posisi"
                        elif market.spread_pct > args.max_spread_pct:
                            note = f"BUY dilewati: spread {market.spread_pct:.3f}% terlalu lebar"
                        else:
                            order_cap = min(order_cap, idr_free / (1 + fee_rate))
                            if args.order_idr is not None:
                                order_cap = min(order_cap, args.order_idr)
                            if order_cap < minimum_order:
                                note = f"BUY dilewati: cap Rp{order_cap:,.0f} di bawah minimum pair"
                            else:
                                note = _execute(api, store, "BUY", quote_idr=int(order_cap))
                                event = "BUY"
                                event_price = market.ask
                    elif signal_action == Action.SELL and qty > 0:
                        sell_qty = _rounded_quantity(min(qty, base_free), quantity_increment)
                        if Decimal(sell_qty) < Decimal(str(minimum_quantity)):
                            note = "Posisi tersisa di bawah minimum jual pair"
                        else:
                            note = _execute(api, store, "SELL", quantity=sell_qty)
                            event = "SELL"
                            event_quantity, event_price = float(sell_qty), market.bid
                    position = store.position()
                    account_info = _confirm_account(api)
                    equity = (_balance(account_info, "IDR", "free")
                              + _balance(account_info, "IDR", "locked")
                              + float(position["quantity"]) * market.bid)
                else:
                    account = store.account()
                    position_value = account["quantity"] * market.bid
                    order_cap = 0.0 if hit_limit else limits.allowed_order_value(
                        equity=equity, current_position_value=position_value, daily_pnl=daily_pnl
                    )
                    if signal_action == Action.BUY and account["quantity"] == 0:
                        cooldown_left = store.cooldown_remaining_minutes(args.cooldown_minutes)
                        if cooldown_left > 0:
                            note = f"BUY ditunda {cooldown_left:.0f} menit setelah penutupan posisi"
                        elif market.spread_pct > args.max_spread_pct:
                            note = f"BUY dilewati: spread {market.spread_pct:.3f}% terlalu lebar"
                        else:
                            if args.order_idr is not None:
                                order_cap = min(order_cap, args.order_idr)
                            if order_cap < minimum_order:
                                note = f"BUY dilewati: cap Rp{order_cap:,.0f} di bawah minimum pair"
                            else:
                                note = store.trade("BUY", market.ask, order_cap, fee_rate, f"Skor ML naik={prediction.score_up:.3f}; {prediction.explanation}")
                                event = "BUY"
                                event_price = market.ask
                    elif signal_action == Action.SELL and account["quantity"] > 0:
                        event_quantity = float(account["quantity"])
                        event_price = market.bid
                        note = store.trade("SELL", market.bid, 0, fee_rate, f"Skor ML naik={prediction.score_up:.3f}; {prediction.explanation}")
                        event = "SELL"
                    updated = store.account()
                    equity = updated["cash_idr"] + updated["quantity"] * market.bid
                if args.mode == "live":
                    display_position = store.position()
                    display_quantity = float(display_position["quantity"])
                    display_entry = float(display_position["entry_price"])
                else:
                    display_position = store.account()
                    display_quantity = float(display_position["quantity"])
                    display_entry = float(display_position["entry_price"])
                show_dashboard(
                    symbol=symbol, mode=mode_label, market=market, action=signal_action,
                    score=prediction.score_up,
                    validation=prediction.validation_balanced_accuracy,
                    equity=equity, daily_pnl=daily_pnl, quantity=display_quantity,
                    entry_price=display_entry, note=note,
                    hit_daily_limit=hit_limit, spread_limit=args.max_spread_pct,
                    stop_loss_pct=args.stop_loss_pct,
                )
                if event:
                    show_trade_animation(
                        event, symbol=symbol,
                        quantity=event_quantity or display_quantity, price=event_price,
                    )
                    event_id += 1
                if args.web:
                    chart_prices = [float(candle["close"]) for candle in recent_candles[-36:]]
                    write_web_status({
                        "running": True, "updated_at": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol, "mode": mode_label, "price": market.last,
                        "bid": market.bid, "ask": market.ask, "spread_pct": market.spread_pct,
                        "max_spread_pct": args.max_spread_pct, "action": signal_action.value,
                        "score": prediction.score_up,
                        "validation": prediction.validation_balanced_accuracy,
                        "equity": equity, "daily_pnl": daily_pnl,
                        "quantity": display_quantity, "entry_price": display_entry,
                        "position_value": display_quantity * market.bid,
                        "stop_price": display_entry * (1 - args.stop_loss_pct / 100) if display_quantity > 0 else 0,
                        "stop_loss_pct": args.stop_loss_pct, "note": note,
                        "reason": prediction.explanation, "chart": chart_prices,
                        "event": event, "event_id": event_id,
                        "event_quantity": event_quantity or display_quantity,
                        "event_price": event_price,
                        "change_pct": ((chart_prices[-1] / chart_prices[0] - 1) * 100
                                        if len(chart_prices) > 1 and chart_prices[0] else 0),
                    })
            except LiveSafetyStop:
                raise
            except Exception as exc:
                if args.mode == "live":
                    raise LiveSafetyStop(f"Kesalahan live; bot dihentikan fail-closed: {exc}") from exc
                print(f"GAGAL mengambil/mengolah data: {exc}", file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Bot dihentikan.")
    finally:
        if args.web:
            try:
                write_web_status({"running": False, "mode": mode_label,
                                  "updated_at": datetime.now(timezone.utc).isoformat(),
                                  "note": "Bot dihentikan."})
            except OSError:
                pass
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        store.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Pemantau Indodax spot dengan simulasi realistis")
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--live-confirm", default="",
                        help="Ketik persis I_ACCEPT_REAL_TRADING_RISK untuk membuka order live")
    parser.add_argument("--symbol", default="AUTO", help="AUTO pilih BTCIDR/ETHIDR berdasar likuiditas atau tentukan pair")
    parser.add_argument("--reselect-auto", action="store_true",
                        help="Pilih ulang pair AUTO; ditolak bila posisi pair tersimpan masih terbuka")
    parser.add_argument("--interval", type=int, default=60, help="Jeda pengambilan harga (detik)")
    parser.add_argument("--training-days", type=int, default=365,
                        help="Riwayat candle 1 jam untuk pelatihan model; rentang 90–730 hari")
    parser.add_argument("--web", action="store_true",
                        help="Jalankan dashboard web lokal di http://127.0.0.1:8765")
    parser.add_argument("--web-port", type=int, default=8765,
                        help="Port dashboard web lokal (1024–65535)")
    parser.add_argument("--initial-idr", type=float, default=1_000_000, help="Saldo simulasi virtual")
    parser.add_argument("--order-idr", type=float, default=None,
                        help="Cap rupiah tambahan per order; batas persentase tetap berlaku")
    parser.add_argument("--max-order-pct", type=float, default=5.0,
                        help="Maksimum order sebagai persen ekuitas")
    parser.add_argument("--max-position-pct", type=float, default=5.0,
                        help="Eksposur maksimum posisi sebagai persen ekuitas")
    parser.add_argument("--daily-loss-pct", type=float, default=1.0,
                        help="Batas rugi harian terhadap ekuitas awal hari (persen)")
    parser.add_argument("--stop-loss-pct", type=float, default=2.0,
                        help="Stop loss bot dari harga masuk (persen)")
    parser.add_argument("--max-spread-pct", type=float, default=0.5,
                        help="Melewati entry baru saat spread bid-ask lebih tinggi (persen)")
    parser.add_argument("--max-data-age-sec", type=int, default=300,
                        help="Jangan membuat keputusan dari data trade lebih lama dari ini")
    parser.add_argument("--cooldown-minutes", type=float, default=30,
                        help="Waktu tunggu setelah jual sebelum membuka posisi baru")
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("--interval minimal 30 detik")
    numeric_args = (args.initial_idr, args.max_order_pct, args.max_position_pct,
                    args.daily_loss_pct, args.stop_loss_pct, args.max_spread_pct,
                    args.max_data_age_sec, args.cooldown_minutes)
    if (not all(math.isfinite(value) for value in numeric_args)
            or args.initial_idr <= 0 or (args.order_idr is not None and
            (not math.isfinite(args.order_idr) or args.order_idr <= 0))
            or not 0 < args.max_order_pct <= 5 or not 0 < args.max_position_pct <= 5
            or not 0 < args.daily_loss_pct < 100 or not 0 < args.stop_loss_pct < 50
            or not 0 < args.max_spread_pct <= 5 or args.max_data_age_sec < 30
            or args.cooldown_minutes < 0 or not 90 <= args.training_days <= 730
            or not 1024 <= args.web_port <= 65535):
        parser.error("Nilai batas risiko tidak valid")
    try:
        run(args)
    except LiveSafetyStop as exc:
        parser.exit(2, f"LIVE SAFETY STOP: {exc}\n")
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"GAGAL: {exc}\n")


if __name__ == "__main__":
    main()
