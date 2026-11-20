"""Build a public, account-free snapshot for the GitHub Pages dashboard."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from bot import fetch_market, fetch_pair_rules
from trained_model import fetch_candles, predict, train_from_exchange

ROOT = Path(__file__).resolve().parent
SYMBOL = "BTCIDR"


def main() -> None:
    fee_rate, _, _, _ = fetch_pair_rules(SYMBOL)
    model = train_from_exchange(
        SYMBOL, fee_rate=fee_rate, days=90,
        model_path=Path("/tmp/tradingai-public-model.json"),
    )
    market = fetch_market(SYMBOL)
    now = int(time.time())
    candles = fetch_candles(SYMBOL, now - 30 * 3600, now, "60")
    prediction = predict(model, candles)
    chart = [float(row["close"]) for row in candles[-36:]]
    payload = {
        "running": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "PUBLIC",
        "symbol": SYMBOL,
        "price": market.last,
        "bid": market.bid,
        "ask": market.ask,
        "spread_pct": market.spread_pct,
        "max_spread_pct": 0.5,
        "action": prediction.action,
        "score": prediction.score_up,
        "validation": prediction.validation_balanced_accuracy,
        "equity": None,
        "daily_pnl": None,
        "quantity": None,
        "entry_price": None,
        "position_value": None,
        "stop_price": None,
        "stop_loss_pct": None,
        "note": "Sinyal publik dari data pasar. Tidak ada data akun atau order pribadi.",
        "reason": prediction.explanation,
        "chart": chart,
        "event": "",
        "event_id": 0,
        "event_quantity": None,
        "event_price": None,
        "change_pct": ((chart[-1] / chart[0] - 1) * 100 if len(chart) > 1 else 0),
        "candle_count": model["candle_count"],
    }
    (ROOT / "site" / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
