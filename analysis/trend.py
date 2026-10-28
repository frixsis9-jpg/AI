"""Higher-timeframe trend direction, used as one confirmation factor."""
from __future__ import annotations

from enum import Enum

from analysis.indicators import ema
from market_data.models import Candle

_EMA_PERIOD = 50
# How many EMA points back to look for the slope check — a couple of candles
# smooths out single-bar noise without lagging too much.
_SLOPE_LOOKBACK = 3
_MIN_CANDLES = _EMA_PERIOD + _SLOPE_LOOKBACK + 5


class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNKNOWN = "unknown"  # not enough higher-timeframe history to judge


def higher_timeframe_trend(candles: list[Candle] | None) -> Trend:
    """EMA50 direction + price position on the higher timeframe.

    Deliberately degrades to UNKNOWN rather than raising when there isn't
    enough history — the higher-timeframe check is one vote out of several
    in analysis.confirmation, not something worth failing the whole analysis
    over.
    """
    if not candles:
        return Trend.UNKNOWN

    closed = [c for c in candles if c.is_closed]
    if len(closed) < _MIN_CANDLES:
        return Trend.UNKNOWN

    closes = [c.close for c in closed]
    ema_values = ema(closes, _EMA_PERIOD)
    last_ema = ema_values[-1]
    prev_ema = ema_values[-1 - _SLOPE_LOOKBACK]
    if last_ema is None or prev_ema is None:
        return Trend.UNKNOWN

    last_close = closes[-1]
    rising = last_ema > prev_ema
    falling = last_ema < prev_ema

    if last_close > last_ema and rising:
        return Trend.UP
    if last_close < last_ema and falling:
        return Trend.DOWN
    return Trend.FLAT
