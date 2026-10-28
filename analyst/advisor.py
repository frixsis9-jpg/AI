"""The desk's decision layer: one direction, one expiration, with reasons.

It sits ON TOP of the quantitative model (analysis.*) and never changes it.
The model analyses three timeframe "trios" (fast 1M·5M·15M for 1-5 min
expirations, medium 5M·15M·30M for 10-15 min, slow 15M·30M·1H for 30-60
min). A trader looks at all of them at once; this module does the same:

1. CONSENSUS DIRECTION. The three trios are averaged in logit space (equal
   weights — they share timeframes, so summing would count the same candles
   twice). Averaging independent-ish noisy readings is the one thing here
   that is statistically principled: noise cancels, a real tilt doesn't.
2. EXPIRATION. Picked from the state of the market, not from "which trio is
   most confident" (choosing the loudest of three noisy readings would
   inflate the shown probability). A horizon preference h ∈ [0, 1] —
   0 = shortest, 1 = longest — is built from three questions an analyst asks:

     * is the market trending or ranging?    trend → longer, range → shorter
     * how volatile is it right now?         spike → shorter, dead → longer
     * how close is the next level in the    near → shorter (take the move
       trade's way?                          before the level gets in the way)

   and mapped onto the 8 available expirations, restricted to trios that
   AGREE with the consensus direction.
3. WARNINGS. Disagreement between trios, a near-coin-flip probability,
   a volatility spike, an incomplete history — said plainly.

Honesty note: the expiration rules are reasoned heuristics. Historical
checks (see README) found no expiration that wins more often than another,
so this layer's value is consistency and clarity — a horizon matched to the
market's rhythm — not a proven edge. It never promises one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from analysis.confirmation import MODEL_VERSION, Direction, probability_for
from analysis.engine import AnalysisResult
from analysis.errors import AnalysisError
from analysis.multi_timeframe import MultiTimeframeResult, analyze_multi_timeframe, timeframe_trio_for
from domain import CurrencyPair, Expiration, Timeframe
from market_data.models import Candle

# Stored with every desk signal so its outcomes are calibrated separately from
# manual-mode ones (the consensus probability is a different quantity).
DESK_VERSION = f"{MODEL_VERSION}+desk"

LADDER: tuple[Expiration, ...] = tuple(Expiration.selectable())  # 1m … 1h, ascending

# Every timeframe any trio needs.
ALL_TIMEFRAMES: tuple[Timeframe, ...] = tuple(
    sorted({tf for e in LADDER for tf in timeframe_trio_for(e)}, key=lambda t: t.seconds)
)

# Market-state cutoffs — the same ones the error analysis used for its 6 market
# regimes (HIGH_VOL was the one condition that was consistently weaker).
TREND_EFFICIENCY = 0.33  # Kaufman efficiency ≥ this → trending
RANGE_EFFICIENCY = 0.225  # < this → ranging
HIGH_VOL = 1.3  # ATR / its recent average (analysis.indicators.atr_ratio)
LOW_VOL = 0.8

# Horizon-preference adjustments.
H_TREND, H_RANGE = +0.30, -0.25
H_HIGH_VOL, H_LOW_VOL = -0.30, +0.20
H_LEVEL_NEAR, H_LEVEL_CLOSE = -0.40, -0.25  # ≤ 0.5 ATR / ≤ 1.0 ATR to the level ahead

COIN_FLIP = 53.0  # below this the model sees almost no edge

TRIO_NAMES = {"fast": "быстрая", "medium": "средняя", "slow": "медленная"}


def side_word(direction: str) -> str:
    return "ВВЕРХ" if direction == "CALL" else "ВНИЗ"


@dataclass(frozen=True)
class TrioView:
    key: str  # "fast" | "medium" | "slow"
    result: MultiTimeframeResult
    expirations: tuple[Expiration, ...]

    @property
    def direction(self) -> Direction:
        return self.result.final_direction


@dataclass(frozen=True)
class MarketRead:
    regime: str  # "тренд" | "флэт" | "переходный"
    efficiency: float
    volatility: str  # "высокая" | "нормальная" | "низкая"
    vol_ratio: float
    opposing_atr: float | None  # distance to the nearest level in the trade's way, in ATR


@dataclass(frozen=True)
class DeskDecision:
    direction: Direction
    probability: float  # of the consensus, %
    logit: float
    expiration: Expiration
    trio: TrioView  # the trio the chosen expiration belongs to
    trios: list[TrioView]
    consensus: int  # trios agreeing with `direction`
    market: MarketRead
    horizon: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_result(self) -> MultiTimeframeResult:
        """The chosen trio's result, restated with the consensus verdict and
        the chosen expiration — so the existing message formatting, storage
        and calibration keep working unchanged. Its pros / cons were computed
        against the trio's own direction, which equals the consensus by
        construction (only agreeing trios are eligible)."""
        return replace(
            self.trio.result,
            expiration=self.expiration,
            final_direction=self.direction,
            probability=self.probability,
            logit=self.logit,
        )


# --- grouping the 8 expirations into the 3 trios ----------------------------

def trio_groups() -> list[tuple[str, tuple[Expiration, ...]]]:
    groups: dict[tuple, list[Expiration]] = {}
    for e in LADDER:
        groups.setdefault(timeframe_trio_for(e), []).append(e)
    names = ("fast", "medium", "slow")
    return [(names[i], tuple(es)) for i, es in enumerate(groups.values())]


def analyze_trios(
    pair: CurrencyPair,
    candles_by_timeframe: dict[Timeframe, list[Candle]],
    live_price: float | None = None,
) -> list[TrioView]:
    """Runs the model for every trio that has enough data. A trio whose
    candles are too short (history still loading) is skipped."""
    views = []
    for key, exps in trio_groups():
        try:
            result = analyze_multi_timeframe(pair, exps[0], candles_by_timeframe, live_price)
        except AnalysisError:
            continue
        views.append(TrioView(key, result, exps))
    return views


# --- market read -------------------------------------------------------------

def opposing_distance_atr(r: AnalysisResult, direction: Direction) -> float | None:
    """Distance from price to the nearest reference level lying in the
    trade's way, in ATR. None if there is no level that way."""
    price, atr = r.current_price, r.indicators.atr
    if atr <= 0:
        return None
    if direction == "CALL":
        gaps = [lv - price for lv in r.structure.levels if lv > price]
    else:
        gaps = [price - lv for lv in r.structure.levels if lv < price]
    return min(gaps) / atr if gaps else None


def read_market(trios: list[TrioView], direction: Direction) -> MarketRead:
    """Reference timeframe: M5 when any trio analysed it (the middle of the
    market's rhythm), else the first available primary."""
    ref = None
    for t in trios:
        for r in t.result.per_timeframe:
            if r.timeframe == Timeframe.M5:
                ref = r
                break
        if ref:
            break
    ref = ref or trios[0].result.primary
    eff = ref.structure.efficiency
    ratio = ref.indicators.atr_ratio
    regime = "тренд" if eff >= TREND_EFFICIENCY else "флэт" if eff < RANGE_EFFICIENCY else "переходный"
    vol_name = "высокая" if ratio >= HIGH_VOL else "низкая" if ratio <= LOW_VOL else "нормальная"
    return MarketRead(regime, eff, vol_name, ratio, opposing_distance_atr(ref, direction))


# --- the expiration rule (pure, so it can be replayed on history) ------------

def horizon_preference(efficiency: float, vol_ratio: float, opposing_atr: float | None) -> tuple[float, list[str]]:
    """0 = shortest expiration, 1 = longest, plus the reasons in words."""
    h = 0.5
    reasons: list[str] = []
    if efficiency >= TREND_EFFICIENCY:
        h += H_TREND
        reasons.append(f"тренд (эффективность {efficiency:.2f}) — движение держится, берём горизонт подлиннее")
    elif efficiency < RANGE_EFFICIENCY:
        h += H_RANGE
        reasons.append(f"флэт (эффективность {efficiency:.2f}) — ходы короткие и возвращаются, горизонт покороче")
    else:
        reasons.append(f"переходный режим (эффективность {efficiency:.2f}) — горизонт средний")
    if vol_ratio >= HIGH_VOL:
        h += H_HIGH_VOL
        reasons.append(f"волатильность высокая ({vol_ratio:.1f}× нормы) — рынок быстрый, лучше не затягивать")
    elif vol_ratio <= LOW_VOL:
        h += H_LOW_VOL
        reasons.append(f"волатильность низкая ({vol_ratio:.1f}× нормы) — рынок вялый, цене нужно больше времени")
    if opposing_atr is not None:
        if opposing_atr <= 0.5:
            h += H_LEVEL_NEAR
            reasons.append(f"уровень на пути сигнала в {opposing_atr:.1f} ATR — забрать ход до того, как уровень помешает")
        elif opposing_atr <= 1.0:
            h += H_LEVEL_CLOSE
            reasons.append(f"уровень на пути сигнала в {opposing_atr:.1f} ATR — горизонт чуть короче")
    return max(0.0, min(1.0, h)), reasons


def choose_expiration(h: float, allowed: list[Expiration]) -> Expiration:
    """The allowed expiration whose ladder position is closest to the target
    (ties → the shorter one)."""
    target = h * (len(LADDER) - 1)
    return min(allowed, key=lambda e: (abs(LADDER.index(e) - target), LADDER.index(e)))


# --- the decision ---------------------------------------------------------------

def decide(trios: list[TrioView]) -> DeskDecision:
    if not trios:
        raise AnalysisError("ни одна из троек таймфреймов не набрала достаточно истории")

    logit = sum(t.result.logit for t in trios) / len(trios)
    if logit == 0.0:  # exact tie — the middle trio breaks it
        mid = next((t for t in trios if t.key == "medium"), trios[0])
        logit = 1e-6 if mid.direction == "CALL" else -1e-6
    direction, probability = probability_for(logit)

    agreeing = [t for t in trios if t.direction == direction]
    allowed = [e for t in agreeing for e in t.expirations]
    market = read_market(agreeing or trios, direction)
    h, reasons = horizon_preference(market.efficiency, market.vol_ratio, market.opposing_atr)
    expiration = choose_expiration(h, allowed)
    chosen = next(t for t in agreeing if expiration in t.expirations)

    views = ', '.join(f'{TRIO_NAMES[t.key]} {side_word(t.direction)} {t.result.probability:.0f}%' for t in trios)
    reasons.insert(0, f'направление {side_word(direction)}: за него {len(agreeing)} из {len(trios)} троек таймфреймов ({views})')

    warnings: list[str] = []
    against = [t for t in trios if t.direction != direction]
    if against:
        warnings.append('Тройки расходятся: ' + ', '.join(f'{TRIO_NAMES[t.key]} — {side_word(t.direction)}' for t in against)
                        + '. Консенсус слабее, чем кажется.')
    if probability < COIN_FLIP:
        warnings.append("Перевес модели минимальный: по данным это почти монетка. Сигнал — не уверенность, а лёгкий наклон.")
    if market.volatility == "высокая":
        warnings.append("Всплеск волатильности: уровни и прошлые закономерности работают хуже.")
    if len(trios) < 3:
        warnings.append(f"Проанализировано {len(trios)} из 3 троек: история ещё догружается, повторите через пару минут.")

    return DeskDecision(direction, probability, logit, expiration, chosen, trios, len(agreeing), market, h, reasons, warnings)
