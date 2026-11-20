"""Fail-closed spot execution orchestration for Indodax Trade API v2."""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from indodax_api import IndodaxAPI, IndodaxAPIError


class LiveSafetyStop(RuntimeError):
    """Stop automation when account state or order outcome is uncertain."""


class LiveStore:
    def __init__(self, db_path: Path, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.base_asset = self.symbol.removesuffix("IDR")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL, price REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS samples_symbol_id ON samples(symbol, id);
            CREATE TABLE IF NOT EXISTS live_positions (
                symbol TEXT PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0,
                entry_price REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS live_orders (
                client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                side TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                response_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS live_trades (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL,
                side TEXT NOT NULL, quantity REAL NOT NULL, quote_idr REAL NOT NULL,
                fee_idr REAL NOT NULL, pnl_idr REAL NOT NULL, client_order_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_daily_marks (
                symbol TEXT NOT NULL, day TEXT NOT NULL, start_equity REAL NOT NULL,
                PRIMARY KEY(symbol, day)
            );
        """)
        pending = self.db.execute(
            "SELECT client_order_id FROM live_orders WHERE symbol=? AND status IN ('PENDING','OPEN')",
            (self.symbol,),
        ).fetchone()
        if pending:
            self.db.close()
            raise LiveSafetyStop(
                f"Order {pending['client_order_id']} masih belum terkonfirmasi. "
                "Periksa order dan trade Indodax secara manual sebelum melanjutkan."
            )
        other = self.db.execute("SELECT symbol FROM live_positions WHERE quantity>0 AND symbol<>? LIMIT 1",
                                (self.symbol,)).fetchone()
        if other:
            self.db.close()
            raise LiveSafetyStop(f"Posisi live {other['symbol']} masih tercatat; bot hanya boleh mengelola satu pair")
        self.db.execute("INSERT OR IGNORE INTO live_positions(symbol) VALUES(?)", (self.symbol,))
        self.db.commit()

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

    def position(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM live_positions WHERE symbol=?", (self.symbol,)).fetchone()

    def has_open_order(self) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM live_orders WHERE symbol=? AND status IN ('PENDING','OPEN') LIMIT 1",
            (self.symbol,),
        ).fetchone())

    def daily_pnl(self, equity: float, loss_fraction: float) -> tuple[bool, float]:
        day = datetime.now().astimezone().date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO live_daily_marks(symbol,day,start_equity) VALUES(?,?,?)",
                        (self.symbol, day, equity))
        self.db.commit()
        start = float(self.db.execute(
            "SELECT start_equity FROM live_daily_marks WHERE symbol=? AND day=?",
            (self.symbol, day),
        ).fetchone()["start_equity"])
        pnl = equity - start
        return pnl <= -(start * loss_fraction), pnl

    def cooldown_remaining_minutes(self, minutes: float) -> float:
        row = self.db.execute("SELECT at FROM live_trades WHERE symbol=? AND side='SELL' ORDER BY id DESC LIMIT 1",
                              (self.symbol,)).fetchone()
        if row is None:
            return 0.0
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])).total_seconds() / 60
        return max(0.0, minutes - elapsed)

    def begin_order(self, side: str) -> str:
        client_id = "ta-" + uuid.uuid4().hex[:30]
        self.db.execute("INSERT INTO live_orders(client_order_id,symbol,side,status,created_at) VALUES(?,?,?,?,?)",
                        (client_id, self.symbol, side, "PENDING", datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        return client_id

    def settle_order(self, client_id: str, side: str, fills: list[dict[str, Any]], *, is_open: bool) -> str:
        if not fills:
            raise LiveSafetyStop(f"Order {client_id} belum memiliki fill; automation dihentikan")
        position = self.position()
        old_qty, old_entry = float(position["quantity"]), float(position["entry_price"])
        fill_qty = quote_total = fee_idr = fee_base = 0.0
        seen_trade_ids: set[str] = set()
        for fill in fills:
            returned_client_id = fill.get("clientOrderId")
            if returned_client_id is not None and str(returned_client_id) != client_id:
                raise LiveSafetyStop(f"API mengembalikan fill milik order lain saat memeriksa {client_id}")
            trade_id = fill.get("tradeId")
            if trade_id is not None:
                if str(trade_id) in seen_trade_ids:
                    continue
                seen_trade_ids.add(str(trade_id))
            try:
                qty = float(fill.get("qty", 0))
                quote = float(fill.get("quoteQty", 0))
                fee = float(fill.get("commission", 0))
            except (TypeError, ValueError) as exc:
                raise LiveSafetyStop(f"Fill order {client_id} berisi angka tidak valid") from exc
            fee_asset = str(fill.get("commissionAsset", "IDR")).upper()
            is_buyer = fill.get("isBuyer")
            if is_buyer is not None and is_buyer is not (side == "BUY"):
                raise LiveSafetyStop(f"Fill order {client_id} tidak cocok dengan sisi order")
            if (not all(math.isfinite(value) for value in (qty, quote, fee))
                    or qty <= 0 or quote <= 0 or fee < 0):
                raise LiveSafetyStop(f"Fill order {client_id} berisi kuantitas/nilai/fee tidak valid")
            fill_qty += qty
            quote_total += quote
            if fee_asset == "IDR":
                fee_idr += fee
            elif fee_asset == self.base_asset:
                fee_base += fee
            else:
                raise LiveSafetyStop(f"Fee memakai aset tak dikenal ({fee_asset}); cek posisi manual")
        at = datetime.now(timezone.utc).isoformat()
        if side == "BUY":
            if old_qty > 0 and old_entry <= 0:
                raise LiveSafetyStop("Posisi BUY lama memiliki harga masuk tidak valid")
            net_qty = fill_qty - fee_base
            basis = quote_total + fee_idr
            if net_qty <= 0:
                raise LiveSafetyStop("Fill BUY setelah fee tidak memiliki jumlah bersih positif")
            new_qty = old_qty + net_qty
            new_entry = (old_qty * old_entry + basis) / new_qty
            pnl = 0.0
        elif side == "SELL":
            if old_qty <= 0 or old_entry <= 0:
                raise LiveSafetyStop("Fill SELL tidak memiliki posisi bot yang valid")
            removed_qty = fill_qty + fee_base
            if removed_qty > old_qty + 1e-12:
                raise LiveSafetyStop("Fill SELL melebihi posisi bot yang tercatat")
            net_proceeds = quote_total - fee_idr
            pnl = net_proceeds - fill_qty * old_entry - fee_base * old_entry
            new_qty = max(0.0, old_qty - removed_qty)
            new_entry = old_entry if new_qty > 1e-12 else 0.0
        else:
            raise LiveSafetyStop(f"Sisi order tidak didukung: {side}")
        self.db.execute("UPDATE live_positions SET quantity=?,entry_price=? WHERE symbol=?",
                        (new_qty, new_entry, self.symbol))
        self.db.execute("UPDATE live_orders SET status=?,response_json=? WHERE client_order_id=?",
                        ("OPEN" if is_open else "FILLED", json.dumps(fills), client_id))
        self.db.execute("INSERT INTO live_trades(symbol,at,side,quantity,quote_idr,fee_idr,pnl_idr,client_order_id) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (self.symbol, at, side, fill_qty, quote_total, fee_idr, pnl, client_id))
        self.db.commit()
        return (f"LIVE {side}: fill {fill_qty:.8f}, nilai Rp{quote_total:,.0f}, "
                f"fee IDR Rp{fee_idr:,.0f}, P/L terealisasi Rp{pnl:,.0f}")

    def close(self) -> None:
        self.db.close()


def _balance(account: dict[str, object], asset: str, field: str = "free") -> float:
    rows = account.get("balances", [])
    if not isinstance(rows, list):
        raise LiveSafetyStop("Format saldo akun Indodax tidak dikenal")
    for row in rows:
        if isinstance(row, dict) and str(row.get("asset", "")).upper() == asset.upper():
            try:
                value = float(row.get(field, 0))
            except (TypeError, ValueError) as exc:
                raise LiveSafetyStop(f"Saldo {asset} tidak valid") from exc
            if not math.isfinite(value) or value < 0:
                raise LiveSafetyStop(f"Saldo {asset} tidak valid")
            return value
    return 0.0


def _confirm_account(api: IndodaxAPI) -> dict[str, object]:
    account = api.account()
    if account.get("canTrade") is not True:
        raise LiveSafetyStop("API key tidak memiliki izin trading")
    if account.get("canWithdraw") is not False or account.get("canWithdrawFiat") is not False:
        raise LiveSafetyStop("API key harus tanpa izin crypto maupun IDR withdrawal")
    return account


def _rounded_quantity(quantity: float, increment: str) -> str:
    q, step = Decimal(str(quantity)), Decimal(str(increment))
    if not step.is_finite() or step <= 0 or not q.is_finite() or q < 0:
        raise LiveSafetyStop("Kuantitas/increment pair tidak valid")
    units = (q / step).to_integral_value(rounding=ROUND_DOWN)
    return format(units * step, "f")


def _execute(api: IndodaxAPI, store: LiveStore, side: str, *, quote_idr: int | None = None,
             quantity: str | None = None) -> str:
    client_id = store.begin_order(side)
    try:
        api.create_market_order(store.symbol, side, quote_idr=quote_idr, quantity=quantity,
                                client_order_id=client_id)
    except Exception as exc:
        raise LiveSafetyStop(
            f"Order {client_id} mungkin telah diterima tetapi respons gagal. Jangan jalankan ulang; "
            "periksa order/trade Indodax terlebih dahulu."
        ) from exc
    fills: list[dict[str, Any]] = []
    for _ in range(8):
        try:
            fills = api.my_trades(store.symbol, client_id)
        except IndodaxAPIError:
            time.sleep(1)
            continue
        if fills:
            break
        time.sleep(1)
    open_orders = api.open_orders(store.symbol)
    is_open = any(str(row.get("clientOrderId", row.get("newClientOrderId", ""))) == client_id
                  for row in open_orders)
    result = store.settle_order(client_id, side, fills, is_open=is_open)
    if is_open:
        raise LiveSafetyStop(f"Order {client_id} masih terbuka/parsial. Posisi tercatat; periksa manual.")
    return result
