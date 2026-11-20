import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from live_trader import LiveSafetyStop, LiveStore, _confirm_account, _execute, _rounded_quantity
from trading_core import RiskLimits
from trained_model import fetch_candles, predict


def candles(count=15, gap_at=None):
    result = []
    for i in range(count):
        stamp = i * 3600
        if gap_at is not None and i >= gap_at:
            stamp += 3600
        value = 100 + i
        result.append({"time": float(stamp), "open": value, "high": value + 1,
                       "low": value - 1, "close": value, "volume": 10})
    return result


class SafetyTests(unittest.TestCase):
    def test_risk_sizing_caps_single_order_and_position(self):
        limits = RiskLimits(.05, .05, .01)
        self.assertEqual(limits.allowed_order_value(equity=1_000_000,
                         current_position_value=0, daily_pnl=0), 50_000)
        self.assertEqual(limits.allowed_order_value(equity=1_000_000,
                         current_position_value=40_000, daily_pnl=0), 10_000)
        self.assertEqual(limits.allowed_order_value(equity=1_000_000,
                         current_position_value=0, daily_pnl=-10_000), 0)

    def test_candle_fetch_drops_open_hour_and_malformed_ohlc(self):
        now = 100 * 3600
        rows = []
        for i in range(22):
            stamp = now - (21 - i) * 3600
            value = 100 + i
            rows.append({"Time": stamp, "Open": value, "High": value + 1,
                         "Low": value - 1, "Close": value, "Volume": 5})
        rows[0]["High"] = rows[0]["Close"] - 1
        with patch("trained_model.time.time", return_value=now), \
             patch("trained_model._get_json", return_value=rows):
            result = fetch_candles("BTCIDR", now - 30 * 3600, now)
        self.assertEqual(len(result), 20)
        self.assertLessEqual(result[-1]["time"] + 3600, now)

    def test_model_holds_when_validation_is_not_above_baseline(self):
        model = {"means": [0] * 6, "scales": [1] * 6,
                 "weights": [2] + [0] * 6,
                 "validation_balanced_accuracy": .5,
                 "validation_baseline": .5, "validation_samples": 300}
        result = predict(model, candles())
        self.assertEqual(result.action, "HOLD")

    def test_live_model_holds_even_if_one_holdout_is_above_random_without_promotion(self):
        model = {"means": [0] * 6, "scales": [1] * 6,
                 "weights": [2] + [0] * 6,
                 "validation_balanced_accuracy": .8,
                 "validation_baseline": .5, "validation_samples": 300}
        result = predict(model, candles(), allow_unvalidated=False)
        self.assertEqual(result.action, "HOLD")
        self.assertIn("belum disetujui", result.explanation)

    def test_promoted_model_still_requires_validation_above_baseline(self):
        model = {"means": [0] * 6, "scales": [1] * 6,
                 "weights": [2] + [0] * 6, "live_approved": True,
                 "validation_balanced_accuracy": .5,
                 "validation_baseline": .5, "validation_samples": 300}
        self.assertEqual(predict(model, candles()).action, "HOLD")

    def test_model_rejects_gapped_hourly_window(self):
        model = {"means": [0] * 6, "scales": [1] * 6,
                 "weights": [0] * 7,
                 "validation_balanced_accuracy": .6,
                 "validation_baseline": .5, "validation_samples": 300}
        with self.assertRaisesRegex(RuntimeError, "tidak berurutan"):
            predict(model, candles(gap_at=8))

    def test_live_requires_withdrawal_permissions_disabled(self):
        class API:
            def account(self):
                return {"canTrade": True, "canWithdraw": True, "canWithdrawFiat": False}
        with self.assertRaisesRegex(LiveSafetyStop, "tanpa izin"):
            _confirm_account(API())

    def test_quantity_rounds_down(self):
        self.assertEqual(_rounded_quantity(.123456789, "0.001"), "0.123")

    def test_duplicate_local_process_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.lock"
            with bot.single_instance_lock(path):
                with self.assertRaisesRegex(LiveSafetyStop, "Bot lain masih berjalan"):
                    with bot.single_instance_lock(path):
                        pass

    def test_paper_daily_equity_includes_exit_fee(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            with patch.object(bot, "DB_PATH", db_path):
                store = bot.PaperStore("BTCIDR", 1000)
                try:
                    store.db.execute("UPDATE paper_accounts SET cash_idr=0, quantity=1, entry_price=100")
                    store.db.commit()
                    hit, pnl = store.daily_pnl(100, .05, .02)
                    self.assertFalse(hit)
                    self.assertEqual(pnl, 0)
                    hit, pnl = store.daily_pnl(94, .05, .02)
                    self.assertTrue(hit)
                    self.assertAlmostEqual(pnl, -5.88)
                finally:
                    store.close()

    def test_live_order_must_be_terminal_and_match_reported_fill_quantity(self):
        class FakeAPI:
            def __init__(self, status, executed="0.01"):
                self.status, self.executed = status, executed

            def create_market_order(self, *args, **kwargs):
                return {"clientOrderId": kwargs["client_order_id"]}

            def my_trades(self, symbol, client_id):
                return [{"clientOrderId": client_id, "tradeId": 1, "qty": "0.01",
                         "quoteQty": "100000", "commission": "1000",
                         "commissionAsset": "IDR", "isBuyer": True}]

            def order_status(self, symbol, client_id):
                return {"clientOrderId": client_id, "symbol": symbol, "side": "BUY",
                        "status": self.status, "executedQty": self.executed}

            def open_orders(self, symbol):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStore(Path(tmp) / "live.sqlite3", "BTCIDR")
            try:
                with patch("live_trader.time.sleep"):
                    with self.assertRaises(LiveSafetyStop):
                        _execute(FakeAPI("NEW"), store, "BUY", quote_idr=100_000)
                self.assertEqual(store.position()["quantity"], 0)
                self.assertEqual(store.db.execute("SELECT status FROM live_orders").fetchone()[0], "PENDING")
            finally:
                store.close()

    def test_live_order_terminal_fill_is_reconciled_before_settlement(self):
        class FakeAPI:
            def create_market_order(self, *args, **kwargs):
                return {"clientOrderId": kwargs["client_order_id"]}

            def my_trades(self, symbol, client_id):
                return [{"clientOrderId": client_id, "tradeId": 1, "qty": "0.01",
                         "quoteQty": "100000", "commission": "1000",
                         "commissionAsset": "IDR", "isBuyer": True}]

            def order_status(self, symbol, client_id):
                return {"clientOrderId": client_id, "symbol": symbol, "side": "BUY",
                        "status": "FILLED", "executedQty": "0.02"}

            def open_orders(self, symbol):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStore(Path(tmp) / "live.sqlite3", "BTCIDR")
            try:
                with patch("live_trader.time.sleep"):
                    with self.assertRaises(LiveSafetyStop):
                        _execute(FakeAPI(), store, "BUY", quote_idr=100_000)
                self.assertEqual(store.position()["quantity"], 0)
                self.assertEqual(store.db.execute("SELECT status FROM live_orders").fetchone()[0], "PENDING")
            finally:
                store.close()

    def test_live_fill_updates_position_once_from_unique_trade_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveStore(Path(tmp) / "live.sqlite3", "BTCIDR")
            try:
                client_id = store.begin_order("BUY")
                fill = {"clientOrderId": client_id, "tradeId": 7, "qty": "0.01",
                        "quoteQty": "100000", "commission": "1000",
                        "commissionAsset": "IDR", "isBuyer": True}
                store.settle_order(client_id, "BUY", [fill, fill], is_open=False)
                position = store.position()
                self.assertAlmostEqual(position["quantity"], .01)
                self.assertAlmostEqual(position["entry_price"], 10_100_000)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT status FROM live_orders").fetchone()[0], "FILLED")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
