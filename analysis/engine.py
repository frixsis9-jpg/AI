"""Top-level entry point: combines every analysis.* module into one result."""
from __future__ import annotations

from dataclasses import dataclass

from analysis.candlestick_patterns import CandleContext, CandleReport, analyze_candles, describe
import analysis.confirmation as confirmation_module
from analysis.confirmation import ConfirmationResult, evaluate
from analysis.indicators import IndicatorSnapshot, compute_indicator_snapshot
from analysis.price_action import PriceActionSignal, aggregate_bias, detect_price_action
from analysis.structure import Finding, StructureReport, analyze_structure
from analysis.support_resistance import Level, find_levels
from analysis.trend import Trend, higher_timeframe_trend
from domain import CurrencyPair, Expiration, Timeframe
from market_data.models import Candle

# How many closed candles the analysis reads per timeframe.
ANALYSIS_WINDOW = 120


@dataclass(frozen=True)
class AnalysisResult:
    pair: CurrencyPair
    timeframe: Timeframe
    expiration: Expiration
    current_price: float
    indicators: IndicatorSnapshot
    levels: list[Level]
    price_action: list[PriceActionSignal]
    higher_timeframe_trend: Trend
    structure: StructureReport
    confirmation: ConfirmationResult
    closed_candles: int
    candles: CandleReport = CandleReport(0.0)


def analyze(
    pair: CurrencyPair,
    timeframe: Timeframe,
    expiration: Expiration,
    candles: list[Candle],
    higher_timeframe_candles: list[Candle] | None = None,
    live_price: float | None = None,
    pattern_scale: float = 1.0,
) -> AnalysisResult:
    """Full rule-based analysis of one timeframe.

    Reads the last ANALYSIS_WINDOW closed candles (the forming one is never
    fed to indicators — it would make every reading flap tick by tick).
    `live_price`, when given, is the latest tick and becomes
    `current_price` (the entry price shown to the user and stored for
    settlement). Raises AnalysisError if there isn't enough history.
    """
    closed = [c for c in candles if c.is_closed][-ANALYSIS_WINDOW:]
    indicators = compute_indicator_snapshot(closed)
    current_price = live_price if live_price is not None else candles[-1].close

    levels = find_levels(closed, cluster_tolerance=indicators.atr * 0.5)
    price_action_signals = detect_price_action(closed)
    trend = higher_timeframe_trend(higher_timeframe_candles)
    structure = analyze_structure(closed, indicators.atr)

    candles_report = candle_analysis(closed, indicators.atr, structure, trend)

    last = closed[-1]
    tiebreak = 1 if last.close > last.open else -1 if last.close < last.open else 0

    confirmation = evaluate(
        indicators=indicators,
        price_action_bias=aggregate_bias(price_action_signals),
        higher_trend=trend,
        structure_scores=structure.scores,
        regime=structure.regime,
        expiration=expiration,
        findings=structure.findings + _pattern_findings(candles_report),
        tiebreak_bias=tiebreak,
        candle_score=candles_report.score,
        factor_scale={"candle_patterns": pattern_scale},
    )

    return AnalysisResult(
        pair=pair,
        timeframe=timeframe,
        expiration=expiration,
        current_price=current_price,
        indicators=indicators,
        levels=levels,
        price_action=price_action_signals,
        higher_timeframe_trend=trend,
        structure=structure,
        confirmation=confirmation,
        closed_candles=len(closed),
        candles=candles_report,
    )


def candle_analysis(closed: list[Candle], atr: float, structure: StructureReport, trend: Trend) -> CandleReport:
    """Candlestick patterns read in the context the rest of the analysis
    already computed: levels, regime, higher-TF trend, structure, momentum,
    and whether a sweep / level reaction already covers the same move."""
    sweep = structure.scores.get("liquidity_sweep", 0.0)
    reaction = structure.scores.get("level_reaction", 0.0)
    ctx = CandleContext(
        levels=structure.levels,
        atr=atr,
        regime=structure.regime,
        htf_trend=1 if trend == Trend.UP else -1 if trend == Trend.DOWN else 0,
        structure=structure.scores.get("structure", 0.0),
        impulse=structure.scores.get("impulse", 0.0),
        overlap=sweep if abs(sweep) >= abs(reaction) else reaction,
    )
    return analyze_candles(closed, ctx, confirmation_module.MODEL.pattern_enabled)


def _pattern_findings(report: CandleReport) -> list[Finding]:
    """The strongest active pattern per direction, as explainable findings."""
    out = []
    for sign in (1, -1):
        side = [s for s in report.active if s.sign == sign]
        if side:
            best = max(side, key=lambda s: abs(s.score))
            out.append(Finding("candle_patterns", report.score, describe(best)))
    return out
