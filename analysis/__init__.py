"""Rule-based technical analysis layer.

Pure Python, no pandas/numpy — the candle counts involved (a few hundred at
most) don't need it, and it keeps the dependency list short.

This layer computes indicators and a weighted, always-decided CALL/PUT
verdict from them, with a 0-100 confidence score (see
`analysis.confirmation`). It does NOT call Claude — that's the next stage.
The verdict here is a transparent, deterministic heuristic meant to either
stand on its own for now, or later feed Claude's reasoning /act as a
guardrail on top of it.
"""
from analysis.engine import AnalysisResult, analyze
from analysis.errors import AnalysisError
from analysis.multi_timeframe import (
    MultiTimeframeResult,
    analyze_multi_timeframe,
    combine_timeframe_directions,
    combine_timeframe_results,
    timeframe_trio_for,
)

__all__ = [
    "AnalysisError",
    "AnalysisResult",
    "MultiTimeframeResult",
    "analyze",
    "analyze_multi_timeframe",
    "combine_timeframe_directions",
    "combine_timeframe_results",
    "timeframe_trio_for",
]
