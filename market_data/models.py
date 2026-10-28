"""Data structures shared by every market data provider implementation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLC(V) candle, chronologically ordered by `time`."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    # False for the current, still-forming candle (Pocket Option's live feed
    # can return one). Indicators should normally only use closed candles;
    # a still-forming one is useful mainly for very short expirations, where
    # the freshest possible price matters more than a fully closed bar.
    is_closed: bool = True


class MarketDataError(Exception):
    """Raised when a provider cannot return usable candle data.

    Covers both transport failures (network, HTTP errors) and provider-level
    errors (invalid symbol, rate limit exceeded, malformed response) — callers
    only need to catch this one type and decide what to tell the user.
    """
