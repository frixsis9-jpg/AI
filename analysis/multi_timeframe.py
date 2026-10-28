"""Combines three timeframes into one CALL/PUT verdict with a probability.

Which three are analyzed is picked from the expiration (`timeframe_trio_for`)
so they stay at the same "zoom level" as the trade: the fastest one is the
primary (entry timing), the other two give context. Each timeframe's own
candles double as the higher-timeframe trend input for the one below it.

Combination is done in logit space (see analysis.confirmation): each
timeframe's logit is weighted (primary counts most) and summed, then turned
back into a probability. Agreement between timeframes therefore raises the
probability naturally, and disagreement pulls it toward 50% — no separate
voting rule needed. The result is always CALL or PUT.
"""
from __future__ import annotations

from dataclasses import dataclass

from analysis.confirmation import Direction, probability_for
from analysis.engine import AnalysisResult, analyze
from analysis.errors import AnalysisError
from analysis.structure import Finding
from domain import CurrencyPair, Expiration, Timeframe
from market_data.models import Candle

_TRIOS_BY_MAX_EXPIRATION_SECONDS: tuple[tuple[int, tuple[Timeframe, Timeframe, Timeframe]], ...] = (
    (300, (Timeframe.M1, Timeframe.M5, Timeframe.M15)),  # 1-5 min
    (900, (Timeframe.M5, Timeframe.M15, Timeframe.M30)),  # 10-15 min
    (3600, (Timeframe.M15, Timeframe.M30, Timeframe.H1)),  # 30 min - 1 h
)

# Logit weight per trio position (primary, middle, slowest). Sums to a bit
# over 1 so full agreement is rewarded slightly beyond any single timeframe.
TIMEFRAME_WEIGHTS = (0.55, 0.30, 0.20)

# Candlestick patterns on a higher timeframe of the trio weigh more than the
# same pattern on the fastest one (they summarize more trading); combined
# with TIMEFRAME_WEIGHTS a pattern's total pull per trio slot is roughly
# 0.55 / 0.45 / 0.40 instead of shrinking with the slot.
PATTERN_TF_SCALE = (1.0, 1.5, 2.0)


def timeframe_trio_for(expiration: Expiration) -> tuple[Timeframe, Timeframe, Timeframe]:
    for max_seconds, trio in _TRIOS_BY_MAX_EXPIRATION_SECONDS:
        if expiration.value <= max_seconds:
            return trio
    return _TRIOS_BY_MAX_EXPIRATION_SECONDS[-1][1]


def combine_timeframe_directions(directions: list[Direction]) -> Direction:
    """Plain majority (tie → CALL). Kept as a simple building block."""
    return "CALL" if directions.count("CALL") >= directions.count("PUT") else "PUT"


def combine_timeframe_results(
    results: list[AnalysisResult], weights: list[float] | None = None
) -> tuple[Direction, float, float]:
    """(direction, probability %, combined logit)."""
    if weights is None:
        weights = list(TIMEFRAME_WEIGHTS[: len(results)])
    logit = sum(w * r.confirmation.logit for w, r in zip(weights, results))
    if logit == 0.0:
        logit = 1e-6 if results[0].confirmation.direction == "CALL" else -1e-6
    direction, probability = probability_for(logit)
    return direction, probability, logit


@dataclass(frozen=True)
class FactorNote:
    timeframe: Timeframe
    text: str
    weight: float  # signed, in favour (+) / against (-) the final direction


@dataclass(frozen=True)
class MultiTimeframeResult:
    pair: CurrencyPair
    expiration: Expiration
    per_timeframe: list[AnalysisResult]
    final_direction: Direction
    probability: float  # model estimate that final_direction wins, %
    logit: float
    agreement: int
    pros: list[FactorNote]
    cons: list[FactorNote]

    @property
    def confidence(self) -> float:
        return self.probability

    @property
    def primary(self) -> AnalysisResult:
        return self.per_timeframe[0]

    @property
    def analyzed_count(self) -> int:
        return len(self.per_timeframe)


def _notes(results: list[AnalysisResult], weights: list[float], direction: Direction) -> tuple[list[FactorNote], list[FactorNote]]:
    sign = 1 if direction == "CALL" else -1
    pros: list[FactorNote] = []
    cons: list[FactorNote] = []
    for w, r in zip(weights, results):
        conf = r.confirmation
        explained: set[str] = set()
        for f in conf.findings:
            contribution = conf.contributions.get(f.factor, 0.0) * w
            if abs(contribution) < 0.01:
                continue
            explained.add(f.factor)
            text = f.text
            if f.score and contribution * f.score < 0:
                # The text describes the market ("bearish divergence"), but the
                # fitted weight of this factor is negative: the model reads it
                # as a counter-signal. Say so, instead of listing a bearish
                # fact under "for CALL" with no explanation.
                text += " — модель читает этот сигнал как контр-индикатор"
            note = FactorNote(r.timeframe, text, contribution * sign)
            (pros if contribution * sign > 0 else cons).append(note)
        # indicator-type factors have no Finding text — describe the big ones
        for name, c in conf.contributions.items():
            if name in explained or abs(c * w) < 0.04:
                continue
            text = _indicator_text(name, c, r)
            if text:
                note = FactorNote(r.timeframe, text, c * w * sign)
                (pros if c * sign > 0 else cons).append(note)
    pros.sort(key=lambda n: -n.weight)
    cons.sort(key=lambda n: n.weight)
    return pros, cons


def _indicator_text(name: str, contribution: float, r: AnalysisResult) -> str | None:
    up = contribution > 0
    ind = r.indicators
    return {
        "ema_alignment": f"EMA 9/20/50 выстроены {'вверх' if up else 'вниз'}",
        "macd": f"MACD {'растёт' if up else 'падает'} (гистограмма {ind.macd_histogram:+.5f})",
        "rsi_momentum": f"RSI {ind.rsi:.0f} — импульс {'покупателей' if up else 'продавцов'}",
        "rsi_extreme": f"RSI {ind.rsi:.0f} — {'перепроданность' if up else 'перекупленность'}, ожидается откат",
        "stoch_reversal": f"Стохастик разворачивается из зоны {'перепроданности' if up else 'перекупленности'}",
        "higher_timeframe_trend": f"Тренд старшего таймфрейма {'восходящий' if up else 'нисходящий'}",
        "price_action": "Свечной паттерн: " + ", ".join(p.pattern for p in r.price_action) if r.price_action else None,
        "room": f"Больше свободного хода {'вверх' if up else 'вниз'} до ближайшего уровня",
        "candle_pressure": f"Давление свечей в сторону {'покупок' if up else 'продаж'}",
        "impulse": f"Импульс последних свечей {'вверх' if up else 'вниз'}",
    }.get(name)


def analyze_multi_timeframe(
    pair: CurrencyPair,
    expiration: Expiration,
    candles_by_timeframe: dict[Timeframe, list[Candle]],
    live_price: float | None = None,
) -> MultiTimeframeResult:
    """Analyzes every trio timeframe that has enough candles and combines
    them. A timeframe with missing/short history just drops out; raises
    AnalysisError only if none could be analyzed."""
    trio = timeframe_trio_for(expiration)
    results: list[AnalysisResult] = []
    weights: list[float] = []
    for i, timeframe in enumerate(trio):
        candles = candles_by_timeframe.get(timeframe)
        if not candles:
            continue
        higher_timeframe = trio[i + 1] if i + 1 < len(trio) else None
        higher_candles = candles_by_timeframe.get(higher_timeframe) if higher_timeframe else None
        try:
            results.append(analyze(pair, timeframe, expiration, candles, higher_candles, live_price, PATTERN_TF_SCALE[i]))
            weights.append(TIMEFRAME_WEIGHTS[i])
        except AnalysisError:
            continue

    if not results:
        raise AnalysisError(
            "не хватило данных ни на одном из таймфреймов "
            f"({', '.join(tf.value for tf in trio)})"
        )

    direction, probability, logit = combine_timeframe_results(results, weights)
    agreement = sum(1 for r in results if r.confirmation.direction == direction)
    pros, cons = _notes(results, weights, direction)

    return MultiTimeframeResult(
        pair=pair,
        expiration=expiration,
        per_timeframe=results,
        final_direction=direction,
        probability=probability,
        logit=logit,
        agreement=agreement,
        pros=pros,
        cons=cons,
    )


__all__ = [
    "FactorNote",
    "Finding",
    "MultiTimeframeResult",
    "analyze_multi_timeframe",
    "combine_timeframe_directions",
    "combine_timeframe_results",
    "timeframe_trio_for",
]
