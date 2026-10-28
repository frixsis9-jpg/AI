"""Turns every graded factor into one CALL/PUT decision with a probability.

Model: a logistic score. Each factor reads -1 (bearish) .. +1 (bullish);
each has two weights — one for trending markets, one for ranging ones — and
they're blended by the regime (0 = range .. 1 = trend, from Kaufman's
efficiency ratio, see analysis.structure). That matters because the same
reading means opposite things in the two regimes: in a trend, momentum and
structure breaks continue; in a range, extremes, sweeps and overextension
revert. One fixed weight per factor (the old design) averages those two
opposite behaviours into mush.

    logit  = Σ w_f(regime) · x_f
    P(CALL) = 1 / (1 + e^-logit)

The direction is the side with P > 50% — by request the bot always answers
CALL or PUT, never "skip". `probability` is the estimated chance that the
chosen direction wins (always ≥ 50), and is what the user sees.

Weights: the defaults below are priors — hand-set from how these setups are
known to behave, NOT fitted to data. `backtest/fit_weights.py` fits real
weights on recorded Pocket Option candles and writes
`analysis/model_weights.json`; when that file exists it replaces the
defaults automatically. Separately, the shown probability is recalibrated
against the bot's own resolved signals once enough of them exist (see
storage.repository.calibration_table / bot/handlers/signal.py), so over time
the number converges to the real hit rate instead of the model's opinion.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from analysis.indicators import IndicatorSnapshot
from analysis.price_action import Bias
from analysis.structure import Finding
from analysis.trend import Trend
from domain import Expiration

logger = logging.getLogger(__name__)

# WAIT stays in the type only for legacy rows / ai.review; evaluate() never
# returns it.
Direction = Literal["CALL", "PUT", "WAIT"]

# (trend-regime weight, range-regime weight) per factor, in logit units at
# full factor strength.
DEFAULT_WEIGHTS: dict[str, tuple[float, float]] = {
    # trend-following
    "ema_alignment": (0.18, 0.00),
    "macd": (0.10, 0.00),
    "rsi_momentum": (0.06, 0.00),
    "higher_timeframe_trend": (0.22, 0.08),
    "structure": (0.28, 0.10),
    "impulse": (0.15, -0.05),
    # liquidity / levels — work in both regimes
    "liquidity_sweep": (0.45, 0.55),
    "level_reaction": (0.42, 0.42),
    "fvg": (0.18, 0.12),
    "room": (0.14, 0.20),
    "candle_pressure": (0.14, 0.14),
    "price_action": (0.12, 0.20),
    # mean reversion
    "rsi_extreme": (0.06, 0.26),
    "stoch_reversal": (0.06, 0.22),
    "exhaustion": (0.18, 0.30),
    "overextension": (0.08, 0.30),
    "divergence": (0.25, 0.32),
    # added with v3 — see the influence caps below; the final values are
    # chosen by backtest/compare.py on the training slice only
    "candle_patterns": (0.25, 0.25),
    "bollinger": (0.03, 0.08),
}

# v3 factors are capped RELATIVE to all the other factors of the same
# timeframe, so they can confirm or weaken a read but never make it on
# their own: |candle_patterns| ≤ 25% of Σ|others| (→ ≤ 20% of the total),
# |bollinger| ≤ 10% of Σ|others|.
INFLUENCE_CAPS: dict[str, float] = {"candle_patterns": 0.25, "bollinger": 0.10}

FACTOR_LABELS: dict[str, str] = {
    "ema_alignment": "EMA 9/20/50",
    "macd": "MACD",
    "rsi_momentum": "RSI (импульс)",
    "higher_timeframe_trend": "тренд старшего ТФ",
    "structure": "структура рынка",
    "impulse": "импульс",
    "liquidity_sweep": "снятие ликвидности",
    "level_reaction": "реакция на уровень",
    "fvg": "имбаланс (FVG)",
    "room": "запас хода до уровня",
    "candle_pressure": "давление свечей",
    "price_action": "свечной паттерн",
    "rsi_extreme": "RSI (перекупл./перепрод.)",
    "stoch_reversal": "стохастик (разворот)",
    "exhaustion": "истощение движения",
    "overextension": "перерастянутость от средней",
    "divergence": "дивергенция RSI",
    "candle_patterns": "свечные паттерны",
    "bollinger": "Bollinger Bands",
}

# Probabilities are capped: nothing reading 120 candles can honestly claim
# more than this on a binary option.
MAX_PROBABILITY = 82.0

_DEAD_ZONE = 0.05

_WEIGHTS_FILE = Path(__file__).with_name("model_weights.json")


# Global shrink applied to the prior weights. Chosen so that on a pure
# random walk — where no direction can be predicted and the honest answer
# is ~50% — the model's typical reading stays close to 50% instead of
# claiming an edge that isn't there (see tests/test_confirmation.py).
DEFAULT_SCALE = 0.5


@dataclass(frozen=True)
class Model:
    weights: dict[str, tuple[float, float]]
    scale: float = DEFAULT_SCALE
    fitted: bool = False
    # per-pattern switch (analysis.candlestick_patterns); a pattern that
    # didn't beat chance out-of-sample is 0 — detected and shown, no vote
    pattern_enabled: dict[str, float] = field(default_factory=dict)


def load_model(path: Path = _WEIGHTS_FILE) -> Model:
    if not path.exists():
        return Model(DEFAULT_WEIGHTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        weights = dict(DEFAULT_WEIGHTS)
        for name, pair in raw["weights"].items():
            if name in weights:
                weights[name] = (float(pair[0]), float(pair[1]))
        enabled = {str(k): float(v) for k, v in raw.get("pattern_enabled", {}).items()}
        return Model(weights, float(raw.get("scale", 1.0)), fitted=True, pattern_enabled=enabled)
    except Exception:  # noqa: BLE001 - a broken file must never take the bot down
        logger.exception("Could not read %s, using default weights", path)
        return Model(DEFAULT_WEIGHTS)


MODEL = load_model()

# Stored with every signal; calibration only learns from outcomes of the
# same model version.
MODEL_VERSION = "v3-fitted" if MODEL.fitted else "v3-priors"


@dataclass(frozen=True)
class ConfirmationResult:
    direction: Direction  # always "CALL" or "PUT"
    score: float  # logit clipped to -1..+1 for display/combination
    logit: float
    probability: float  # chance the chosen direction wins, 50..MAX_PROBABILITY
    votes: dict[str, float]  # each factor's graded -1..+1 reading
    contributions: dict[str, float]  # weight × reading, logit units
    regime: float
    bullish_votes: int
    bearish_votes: int
    low_volatility: bool
    findings: list[Finding] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Kept for callers that still say "confidence" — same number."""
        return self.probability

    @property
    def forced(self) -> bool:
        return self.probability < 55.0


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def probability_for(logit: float) -> tuple[Direction, float]:
    p_call = sigmoid(logit)
    direction: Direction = "CALL" if logit >= 0 else "PUT"
    p = p_call if direction == "CALL" else 1.0 - p_call
    return direction, round(min(MAX_PROBABILITY, max(50.0, 100.0 * p)), 1)


def factor_contributions(
    votes: dict[str, float],
    regime: float,
    model: Model,
    factor_scale: dict[str, float] | None = None,
    replace_price_action: bool = False,
) -> dict[str, float]:
    """weight(regime) × reading per factor, with the v3 influence caps."""
    contributions: dict[str, float] = {}
    for name, value in votes.items():
        w_trend, w_range = model.weights.get(name, (0.0, 0.0))
        contributions[name] = (regime * w_trend + (1.0 - regime) * w_range) * value
    # The candle-pattern factor replaces the old 4-pattern price_action
    # vote only while it actually votes: with a zero pattern weight,
    # price_action stays active exactly as in the baseline.
    if replace_price_action and any(model.weights.get("candle_patterns", (0.0, 0.0))):
        contributions["price_action"] = 0.0
    for name, scale in (factor_scale or {}).items():
        if name in contributions:
            contributions[name] *= scale
    others = sum(abs(v) for k, v in contributions.items() if k not in INFLUENCE_CAPS and k != "price_action")
    for name, cap in INFLUENCE_CAPS.items():
        if name in contributions:
            limit = cap * others
            contributions[name] = max(-limit, min(limit, contributions[name]))
    return contributions


def indicator_votes(ind: IndicatorSnapshot) -> dict[str, float]:
    return {
        "ema_alignment": _ema_alignment_score(ind),
        "macd": _macd_score(ind),
        "rsi_momentum": _clamp((ind.rsi - 50.0) / 25.0),
        "rsi_extreme": _rsi_extreme_score(ind),
        "stoch_reversal": _stoch_reversal_score(ind),
        "bollinger": _bollinger_score(ind),
    }


def evaluate(
    indicators: IndicatorSnapshot,
    price_action_bias: Bias,
    higher_trend: Trend,
    structure_scores: dict[str, float],
    regime: float,
    expiration: Expiration,
    *,
    findings: list[Finding] | None = None,
    tiebreak_bias: int = 0,
    model: Model | None = None,
    candle_score: float | None = None,
    factor_scale: dict[str, float] | None = None,
) -> ConfirmationResult:
    """`candle_score` is analysis.candlestick_patterns' aggregated factor.
    When given it REPLACES the old 4-pattern `price_action` vote (which is
    kept in `votes` for display but contributes 0), so the same candles are
    never counted twice. `factor_scale` multiplies a factor's contribution
    before the influence cap (used for the higher-timeframe pattern weight).
    """
    model = model or MODEL
    votes: dict[str, float] = {
        **indicator_votes(indicators),
        "price_action": _bias_score(price_action_bias),
        "higher_timeframe_trend": _trend_score(higher_trend),
    }
    for name, value in structure_scores.items():
        votes[name] = _clamp(value)
    if candle_score is not None:
        votes["candle_patterns"] = _clamp(candle_score)

    regime = _clamp(regime, 0.0, 1.0)
    contributions = factor_contributions(votes, regime, model, factor_scale,
                                         replace_price_action=candle_score is not None)
    logit = sum(contributions.values()) * model.scale

    # Very quiet market: every reading is less trustworthy.
    low_volatility = indicators.atr_ratio < 0.5
    if low_volatility:
        logit *= 0.7

    if logit == 0.0:
        logit = 1e-6 if tiebreak_bias >= 0 else -1e-6
    direction, probability = probability_for(logit)

    bullish = sum(1 for v in votes.values() if v > _DEAD_ZONE)
    bearish = sum(1 for v in votes.values() if v < -_DEAD_ZONE)

    reasons = [
        f"{FACTOR_LABELS.get(name, name)}: {'бычий' if c > 0 else 'медвежий'} ({c:+.2f})"
        for name, c in sorted(contributions.items(), key=lambda kv: -abs(kv[1]))
        if abs(c) >= 0.02
    ]

    return ConfirmationResult(
        direction=direction,
        score=_clamp(logit),
        logit=logit,
        probability=probability,
        votes=votes,
        contributions=contributions,
        regime=regime,
        bullish_votes=bullish,
        bearish_votes=bearish,
        low_volatility=low_volatility,
        findings=list(findings or []),
        reasons=reasons,
    )


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ema_alignment_score(ind: IndicatorSnapshot) -> float:
    atr = ind.atr if ind.atr > 0 else 1e-9
    magnitude = _clamp((ind.ema9 - ind.ema50) / (atr * 2))
    aligned_up = ind.ema9 > ind.ema20 > ind.ema50
    aligned_down = ind.ema9 < ind.ema20 < ind.ema50
    if magnitude > 0 and not aligned_up or magnitude < 0 and not aligned_down:
        magnitude *= 0.5
    return magnitude


def _macd_score(ind: IndicatorSnapshot) -> float:
    atr = ind.atr if ind.atr > 0 else 1e-9
    magnitude = _clamp(ind.macd_histogram / (atr * 0.5))
    if magnitude > 0:
        building = ind.macd_histogram >= ind.macd_histogram_prev
    elif magnitude < 0:
        building = ind.macd_histogram <= ind.macd_histogram_prev
    else:
        building = True
    return magnitude if building else magnitude * 0.5


def _rsi_extreme_score(ind: IndicatorSnapshot) -> float:
    """Mean reversion from overbought/oversold: >70 bearish, <30 bullish."""
    if ind.rsi > 70:
        return -_clamp((ind.rsi - 70) / 15.0 + 0.3, 0.0, 1.0)
    if ind.rsi < 30:
        return _clamp((30 - ind.rsi) / 15.0 + 0.3, 0.0, 1.0)
    return 0.0


def _stoch_reversal_score(ind: IndicatorSnapshot) -> float:
    """%K crossing back under %D inside the overbought zone (or over it in
    oversold) — the turn, not merely being extreme."""
    k, d = ind.stochastic_k, ind.stochastic_d
    if k >= 75 and k < d:
        return -_clamp(0.4 + (k - 75) / 25.0, 0.0, 1.0)
    if k <= 25 and k > d:
        return _clamp(0.4 + (25 - k) / 25.0, 0.0, 1.0)
    return 0.0


def _bollinger_score(ind: IndicatorSnapshot) -> float:
    """Distance of the close from the middle band: +1 at/below the lower
    band, −1 at/above the upper (mean-reversion sign; its trend weight lets
    the model read it the other way in trends). 0 if bands are unknown."""
    if ind.bollinger_width <= 0:
        return 0.0
    return _clamp(-(ind.bollinger_pct_b - 0.5) * 2.0)


def _bias_score(bias: Bias) -> float:
    return 1.0 if bias == Bias.BULLISH else -1.0 if bias == Bias.BEARISH else 0.0


def _trend_score(trend: Trend) -> float:
    return 1.0 if trend == Trend.UP else -1.0 if trend == Trend.DOWN else 0.0
