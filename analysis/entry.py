"""One evaluator for manual and automatic pair search. No new probability model.

Existing fitted weights determine direction; current-price/tick rules only
admit or reject an entry. Thresholds are conservative, uncalibrated defaults.
"""

import math
from dataclasses import dataclass

from analysis.multi_timeframe import (
    MultiTimeframeResult,
    analyze_multi_timeframe,
    timeframe_trio_for,
)
from analysis.support_resistance import nearest_level
from domain import Expiration
from market_data.models import MarketDataError
from market_data.snapshot import MarketSnapshot


class EntryRejected(MarketDataError):
    """Inputs are ready, but entry conditions are not satisfied."""

    def __init__(self, reason, result=None, *, retry_after=0):
        super().__init__(reason)
        self.result = result
        self.retry_after = retry_after


VERSION = "entry-v1"
MANUAL_VERSION = "manual-v1"


def age_limit(expiration: Expiration) -> float:
    return (
        0.5
        if expiration.value <= 10
        else 1.0
        if expiration.is_short
        else 2.0
        if expiration.value <= 300
        else 5.0
    )


def quote_age_limit(expiration: Expiration) -> float:
    """Small analysis-only jitter tolerance; exact-time settlement stays strict."""
    return age_limit(expiration) + 0.25


@dataclass(frozen=True)
class Candidate:
    snapshot: MarketSnapshot
    result: MultiTimeframeResult
    quality: float  # ranking points, NEVER advertised as a win probability
    reasons: tuple[str, ...]
    calculated_ms: float = 0.0
    manual: bool = False
    loaded_ms: float = 0.0

    @property
    def direction(self):
        return self.result.final_direction


def evaluate_manual(snapshot: MarketSnapshot, expiration: Expiration) -> Candidate:
    """Return the existing model's direction, with weak factors as context.

    Quote freshness and complete candle history remain required inputs.
    Market selection gates belong to automatic search, not a manual report.
    """
    snapshot.validate(
        timeframe_trio_for(expiration), max_age=quote_age_limit(expiration)
    )
    result = analyze_multi_timeframe(
        snapshot.pair, expiration, snapshot.candles, snapshot.price
    )
    notes = [
        f"Направление поддерживают {result.agreement}/{result.analyzed_count} таймфреймов"
    ]
    if result.agreement < 2:
        notes.append("⚠ Таймфреймы противоречат друг другу")
    if abs(result.logit) < 0.20:
        notes.append("⚠ Перевес модели слабый")
    notes.append(f"Возраст котировки {snapshot.age:.2f} с")
    return Candidate(snapshot, result, 0, tuple(notes), manual=True)


def evaluate(snapshot: MarketSnapshot, expiration: Expiration) -> Candidate:
    trio = timeframe_trio_for(expiration)
    snapshot.validate(trio, max_age=quote_age_limit(expiration))
    result = analyze_multi_timeframe(
        snapshot.pair, expiration, snapshot.candles, snapshot.price
    )

    def reject(reason, *, retry_after=0):
        return EntryRejected(reason, result, retry_after=retry_after)

    if result.analyzed_count != 3 or result.agreement < 2:
        raise reject("Таймфреймы противоречат друг другу")
    if abs(result.logit) < 0.20:
        raise reject("Перевес модели слишком слабый; продолжаю наблюдение")
    primary = result.primary
    atr = primary.indicators.atr
    if not math.isfinite(atr) or atr <= 0:
        raise reject("Недостаточная волатильность")
    ratio = primary.indicators.atr_ratio
    if ratio < 0.65 or ratio > 1.8:
        raise reject("Слишком тихий рынок или всплеск волатильности")
    sign = 1 if result.final_direction == "CALL" else -1
    opposing = "resistance" if sign > 0 else "support"
    opposing_levels = [
        lv for lv in primary.levels if (lv.price - snapshot.price) * sign > 0
    ]
    level = nearest_level(opposing_levels, snapshot.price, opposing)
    room = abs(level.price - snapshot.price) / atr if level else 3.0
    if level and room < 0.35:
        raise reject("Противоположный уровень слишком близко к цене входа")
    closed = [c for c in snapshot.candles[primary.timeframe] if c.is_closed]
    if abs(snapshot.price - closed[-1].close) > 1.5 * atr:
        raise reject("Цена уже ушла далеко; запоздалый вход пропущен")
    forming = [c for c in snapshot.candles[primary.timeframe] if not c.is_closed]
    if forming and (snapshot.price - forming[-1].open) * sign < -0.2 * atr:
        raise reject("Текущая свеча движется против направления")
    window = min(30.0, max(3.0, expiration.value / 2))
    ticks = [
        (t, p)
        for t, p in snapshot.ticks
        if snapshot.captured_at - window <= t <= snapshot.captured_at + 0.5
    ]
    if (
        len(ticks) < (8 if expiration.is_short else 3)
        or ticks[-1][0] - ticks[0][0] < window * 0.5
    ):
        raise reject(
            "Поток ещё прогревается: недостаточно свежих тиков",
            retry_after=window * 0.5,
        )
    movement = (ticks[-1][1] - ticks[0][1]) * sign
    if movement <= 0:
        raise reject("Последние тики не подтверждают направление")
    path = sum(abs(b[1] - a[1]) for a, b in zip(ticks, ticks[1:]))
    efficiency = movement / path if path else 0
    if expiration.is_short and efficiency < 0.25:
        raise reject("Тики слишком хаотичны для секундной экспирации")
    quality = (
        20 * result.agreement
        + min(20, abs(result.logit) * 20)
        + min(10, room * 3)
        + 10 * efficiency
    )
    return Candidate(
        snapshot,
        result,
        round(quality, 1),
        (
            f"Направление поддерживают {result.agreement}/3 таймфреймов",
            "Последние тики подтверждают движение",
            f"Возраст котировки {snapshot.age:.2f} с; расчёт на полных свечах",
        ),
    )
