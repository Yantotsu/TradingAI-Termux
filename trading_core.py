"""Transparent indicators and position-sizing rules for the spot monitor."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Sequence


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Signal:
    action: Action
    score: float
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    explanation: str


@dataclass(frozen=True)
class RiskLimits:
    """Conservative paper defaults, expressed as fractions of equity."""
    max_order_fraction: float = 0.05
    max_position_fraction: float = 0.05
    max_daily_loss_fraction: float = 0.01

    def __post_init__(self) -> None:
        for name, value in (
            ("max_order_fraction", self.max_order_fraction),
            ("max_position_fraction", self.max_position_fraction),
            ("max_daily_loss_fraction", self.max_daily_loss_fraction),
        ):
            if not isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} harus berada di antara 0 dan 1")

    def allowed_order_value(self, *, equity: float, current_position_value: float,
                            daily_pnl: float) -> float:
        """Maximum new buy value, or zero when the daily loss limit is hit."""
        if not all(isfinite(v) for v in (equity, current_position_value, daily_pnl)):
            return 0.0
        if equity <= 0 or current_position_value < 0:
            return 0.0
        if daily_pnl <= -(equity * self.max_daily_loss_fraction):
            return 0.0
        remaining = max(0.0, equity * self.max_position_fraction - current_position_value)
        return min(equity * self.max_order_fraction, remaining)


def _ema(values: Sequence[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Diperlukan setidaknya {period} data untuk EMA")
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError(f"Diperlukan setidaknya {period + 1} data untuk RSI")
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def analyze_closes(closes: Sequence[float]) -> Signal:
    """Rule-based trend signal from >=50 positive closes, oldest to newest.

    The score is an indicator strength, never a probability of profit.
    """
    prices = [float(value) for value in closes]
    if len(prices) < 50:
        raise ValueError("Diperlukan minimal 50 harga penutupan")
    if any(not isfinite(value) or value <= 0 for value in prices):
        raise ValueError("Semua harga harus positif dan finite")
    fast, slow, rsi, close = _ema(prices, 20), _ema(prices, 50), _rsi(prices), prices[-1]
    score, reasons = 0.0, []
    if fast > slow:
        score += 1.0
        reasons.append("EMA20 di atas EMA50")
    else:
        score -= 1.0
        reasons.append("EMA20 di bawah EMA50")
    if close > fast:
        score += 0.5
        reasons.append("harga di atas EMA20")
    else:
        score -= 0.5
        reasons.append("harga di bawah EMA20")
    if rsi < 35:
        score += 0.5
        reasons.append("RSI rendah")
    elif rsi > 70:
        score -= 0.5
        reasons.append("RSI tinggi")
    else:
        reasons.append("RSI di rentang tengah")
    action = Action.BUY if score >= 1 else Action.SELL if score <= -1 else Action.HOLD
    return Signal(action, score, close, fast, slow, rsi, "; ".join(reasons))
