"""Simple candlestick price-action patterns on the last closed candle(s)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market_data.models import Candle


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class PriceActionSignal:
    pattern: str
    bias: Bias


def detect_price_action(candles: list[Candle]) -> list[PriceActionSignal]:
    """Looks at the last two closed candles for a small set of well-known
    patterns. Deliberately conservative — only clear cases are flagged, since
    an over-eager pattern detector would just add noise to the vote count in
    analysis.confirmation.
    """
    closed = [c for c in candles if c.is_closed]
    if len(closed) < 2:
        return []

    prev, last = closed[-2], closed[-1]
    signals: list[PriceActionSignal] = []

    if _is_bullish_engulfing(prev, last):
        signals.append(PriceActionSignal("bullish_engulfing", Bias.BULLISH))
    elif _is_bearish_engulfing(prev, last):
        signals.append(PriceActionSignal("bearish_engulfing", Bias.BEARISH))

    candle_range = _range(last)
    if candle_range > 0:
        body = _body(last)
        if body / candle_range < 0.1:
            signals.append(PriceActionSignal("doji", Bias.NEUTRAL))
        else:
            lower_wick = _lower_wick(last)
            upper_wick = _upper_wick(last)
            if lower_wick >= body * 2 and upper_wick <= body * 0.5:
                signals.append(PriceActionSignal("hammer", Bias.BULLISH))
            elif upper_wick >= body * 2 and lower_wick <= body * 0.5:
                signals.append(PriceActionSignal("shooting_star", Bias.BEARISH))

    return signals


def aggregate_bias(signals: list[PriceActionSignal]) -> Bias:
    """Collapses multiple detected patterns into one bias for voting.

    Conflicting directional patterns (rare, but possible) cancel out to
    neutral rather than arbitrarily picking one.
    """
    directional = [s.bias for s in signals if s.bias != Bias.NEUTRAL]
    if not directional:
        return Bias.NEUTRAL
    if all(b == Bias.BULLISH for b in directional):
        return Bias.BULLISH
    if all(b == Bias.BEARISH for b in directional):
        return Bias.BEARISH
    return Bias.NEUTRAL


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _range(c: Candle) -> float:
    return c.high - c.low


def _upper_wick(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def _lower_wick(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def _is_bullish_engulfing(prev: Candle, last: Candle) -> bool:
    return (
        last.close > last.open
        and prev.close < prev.open
        and last.close >= prev.open
        and last.open <= prev.close
    )


def _is_bearish_engulfing(prev: Candle, last: Candle) -> bool:
    return (
        last.close < last.open
        and prev.close > prev.open
        and last.open >= prev.close
        and last.close <= prev.open
    )
