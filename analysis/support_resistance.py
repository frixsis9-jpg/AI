"""Simple swing-based support/resistance detection.

Finds local swing highs/lows over a lookback window, then clusters nearby
ones together (within an ATR-scaled tolerance) so that noisy near-duplicate
levels collapse into one, weighted by how many times price touched it.
"""
from __future__ import annotations

from dataclasses import dataclass

from market_data.models import Candle

DEFAULT_LOOKBACK = 60
DEFAULT_SWING_WINDOW = 2


@dataclass(frozen=True)
class Level:
    price: float
    kind: str  # "support" | "resistance"
    touches: int


def find_levels(
    candles: list[Candle],
    cluster_tolerance: float,
    lookback: int = DEFAULT_LOOKBACK,
    swing_window: int = DEFAULT_SWING_WINDOW,
) -> list[Level]:
    """Detects support/resistance levels from closed candles.

    `cluster_tolerance` should scale with volatility (the engine passes in
    something ATR-based) — too tight and every candle becomes its own
    "level", too loose and real levels merge into meaningless ones.
    """
    closed = [c for c in candles if c.is_closed][-lookback:]
    if len(closed) < swing_window * 2 + 3:
        return []

    resistance_prices = []
    support_prices = []
    for i in range(swing_window, len(closed) - swing_window):
        window = closed[i - swing_window : i + swing_window + 1]
        if closed[i].high == max(c.high for c in window):
            resistance_prices.append(closed[i].high)
        if closed[i].low == min(c.low for c in window):
            support_prices.append(closed[i].low)

    tolerance = cluster_tolerance if cluster_tolerance > 0 else 0.0
    resistances = _cluster(resistance_prices, "resistance", tolerance)
    supports = _cluster(support_prices, "support", tolerance)

    return sorted(supports, key=lambda lvl: -lvl.touches) + sorted(
        resistances, key=lambda lvl: -lvl.touches
    )


def nearest_level(levels: list[Level], price: float, kind: str) -> Level | None:
    candidates = [lvl for lvl in levels if lvl.kind == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda lvl: abs(lvl.price - price))


def support_resistance_vote(levels: list[Level], price: float, atr: float) -> int:
    """+1 near support (bounce expected), -1 near resistance (rejection expected).

    "Near" means within half an ATR — scales with volatility instead of a
    fixed price distance, since a good proximity threshold on JPY pairs looks
    nothing like one on EUR/USD.
    """
    tolerance = atr * 0.5 if atr else 0.0
    if tolerance <= 0:
        return 0

    support = nearest_level(levels, price, "support")
    if support is not None and 0 <= price - support.price <= tolerance:
        return 1

    resistance = nearest_level(levels, price, "resistance")
    if resistance is not None and 0 <= resistance.price - price <= tolerance:
        return -1

    return 0


def _cluster(prices: list[float], kind: str, tolerance: float) -> list[Level]:
    levels: list[Level] = []
    for price in sorted(prices):
        for i, level in enumerate(levels):
            if abs(level.price - price) <= tolerance:
                merged_price = (level.price * level.touches + price) / (level.touches + 1)
                levels[i] = Level(price=merged_price, kind=kind, touches=level.touches + 1)
                break
        else:
            levels.append(Level(price=price, kind=kind, touches=1))
    return levels
