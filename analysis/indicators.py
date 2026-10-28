"""EMA / RSI / MACD / Stochastic / ATR — plain-Python implementations.

All the `ema`/`rsi`/`sma`/`atr` helpers return a list the same length as
their input, with `None` for the warm-up period before the indicator has
enough data. `compute_indicator_snapshot` is the entry point the rest of the
codebase should use — it takes a list of Candles and returns just the latest
values, already validated.
"""
from __future__ import annotations

from dataclasses import dataclass

from analysis.errors import AnalysisError
from market_data.models import Candle

# Below this many closed candles, EMA50/MACD/ATR warm-up hasn't stabilised
# enough to trust — see compute_indicator_snapshot.
MIN_CANDLES = 60


def ema(values: list[float | None], period: int) -> list[float | None]:
    """Exponential moving average. Leading `None`s (e.g. from MACD's line
    before it has data) are passed through; the EMA itself starts once
    `period` consecutive non-None values are available.
    """
    first_valid = next((i for i, v in enumerate(values) if v is not None), None)
    if first_valid is None:
        return [None] * len(values)

    tail = values[first_valid:]
    if len(tail) < period:
        return [None] * len(values)

    multiplier = 2 / (period + 1)
    result: list[float | None] = [None] * (period - 1)
    seed = sum(tail[:period]) / period
    result.append(seed)
    prev = seed
    for value in tail[period:]:
        prev = value * multiplier + prev * (1 - multiplier)
        result.append(prev)

    return [None] * first_valid + result


def sma(values: list[float | None], period: int) -> list[float | None]:
    """Simple moving average, same warm-up convention as `ema`."""
    first_valid = next((i for i, v in enumerate(values) if v is not None), None)
    if first_valid is None:
        return [None] * len(values)

    tail = values[first_valid:]
    result: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, len(tail)):
        window = tail[i - period + 1 : i + 1]
        result.append(sum(window) / period)

    return [None] * first_valid + result


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    result: list[float | None] = [None] * period
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result.append(_rsi_value(avg_gain, avg_loss))

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result.append(_rsi_value(avg_gain, avg_loss))

    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Returns (macd_line, signal_line, histogram), each same length as `closes`."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    signal = ema(line, signal_period)
    histogram = [
        (m - sig) if m is not None and sig is not None else None
        for m, sig in zip(line, signal)
    ]
    return line, signal, histogram


def stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[list[float | None], list[float | None]]:
    """Returns (%K, %D)."""
    if len(closes) < k_period:
        none_list: list[float | None] = [None] * len(closes)
        return none_list, none_list

    k_values: list[float | None] = [None] * (k_period - 1)
    for i in range(k_period - 1, len(closes)):
        window_high = max(highs[i - k_period + 1 : i + 1])
        window_low = min(lows[i - k_period + 1 : i + 1])
        if window_high == window_low:
            k_values.append(50.0)  # flat window: avoid /0, treat as neutral
        else:
            k_values.append((closes[i] - window_low) / (window_high - window_low) * 100)

    d_values = sma(k_values, d_period)
    return k_values, d_values


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder's Average True Range."""
    if not closes:
        return []

    true_ranges = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if len(true_ranges) < period:
        return [None] * len(closes)

    result: list[float | None] = [None] * (period - 1)
    seed = sum(true_ranges[:period]) / period
    result.append(seed)
    prev = seed
    for value in true_ranges[period:]:
        prev = (prev * (period - 1) + value) / period
        result.append(prev)

    return result


def atr_ratio(atr_values: list[float | None], lookback: int = 20) -> float:
    """Current ATR vs. its own recent average — a simple volatility gauge.

    < 1 means the market is quieter than usual right now; the confirmation
    engine treats a low ratio as a reason to prefer WAIT (see
    analysis/confirmation.py). Returns 1.0 (neutral) when there isn't enough
    history to judge, rather than raising — this is a secondary signal, not
    something worth failing the whole analysis over.
    """
    valid = [v for v in atr_values if v is not None]
    if len(valid) < 2:
        return 1.0

    lookback = min(lookback, len(valid) - 1)
    current = valid[-1]
    recent = valid[-lookback - 1 : -1]
    if not recent:
        return 1.0
    average = sum(recent) / len(recent)
    if average == 0:
        return 1.0
    return current / average


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> tuple[float, float]:
    """(%B, width) of the last close. %B = 0.5 / width = 0 when there isn't
    enough history or the window is flat."""
    if len(closes) < period:
        return 0.5, 0.0
    window = closes[-period:]
    mean = sum(window) / period
    std = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    if std == 0:
        return 0.5, 0.0
    upper, lower = mean + k * std, mean - k * std
    return (closes[-1] - lower) / (upper - lower), (upper - lower) / mean


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Latest value of every indicator, computed from closed candles only."""

    ema9: float
    ema20: float
    ema50: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    macd_histogram_prev: float
    stochastic_k: float
    stochastic_d: float
    atr: float
    atr_ratio: float
    # Bollinger(20, 2): where the close sits between the bands (0 = lower,
    # 1 = upper; <0 / >1 = outside) and band width relative to price.
    bollinger_pct_b: float = 0.5
    bollinger_width: float = 0.0


def compute_indicator_snapshot(
    candles: list[Candle], min_candles: int = MIN_CANDLES
) -> IndicatorSnapshot:
    """Computes every indicator from closed candles and returns their latest
    values. Uses only closed candles — a still-forming one would make EMA/RSI
    flap on every partial tick.

    Raises:
        AnalysisError: if there isn't enough closed-candle history to trust
            the result (EMA50/MACD/ATR all need meaningful warm-up).
    """
    closed = [c for c in candles if c.is_closed]
    if len(closed) < min_candles:
        raise AnalysisError(
            f"need at least {min_candles} closed candles to compute indicators, "
            f"got {len(closed)}"
        )

    closes = [c.close for c in closed]
    highs = [c.high for c in closed]
    lows = [c.low for c in closed]

    ema9_values = ema(closes, 9)
    ema20_values = ema(closes, 20)
    ema50_values = ema(closes, 50)
    rsi_values = rsi(closes, 14)
    macd_line, macd_signal, macd_histogram = macd(closes)
    k_values, d_values = stochastic(highs, lows, closes)
    atr_values = atr(highs, lows, closes)

    histogram_valid = [h for h in macd_histogram if h is not None]

    required_last_values = (
        ema9_values[-1],
        ema20_values[-1],
        ema50_values[-1],
        rsi_values[-1],
        macd_line[-1],
        macd_signal[-1],
        k_values[-1],
        d_values[-1],
        atr_values[-1],
    )
    if any(v is None for v in required_last_values) or len(histogram_valid) < 2:
        raise AnalysisError(
            "not enough history for a stable indicator reading even though "
            f"the minimum candle count ({min_candles}) was met — this shouldn't "
            "normally happen"
        )

    pct_b, width = bollinger(closes)
    return IndicatorSnapshot(
        bollinger_pct_b=pct_b,
        bollinger_width=width,
        ema9=ema9_values[-1],
        ema20=ema20_values[-1],
        ema50=ema50_values[-1],
        rsi=rsi_values[-1],
        macd_line=macd_line[-1],
        macd_signal=macd_signal[-1],
        macd_histogram=histogram_valid[-1],
        macd_histogram_prev=histogram_valid[-2],
        stochastic_k=k_values[-1],
        stochastic_d=d_values[-1],
        atr=atr_values[-1],
        atr_ratio=atr_ratio(atr_values),
    )
