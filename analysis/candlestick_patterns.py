"""Candlestick patterns, graded by CONTEXT rather than by name.

Two layers:

1. `detect_patterns(cs, end)` — pure geometry. Recognizes 33 formations
   ending at bar `end` (29 classic ones + Belt Hold, Kicker, Rising/Falling
   Three Methods, Rejection Wick) and returns `PatternHit`s: name,
   direction, kind (reversal / continuation / indecision) and a 0..1
   `quality` saying how textbook the shape is. Uses bars ≤ `end` only.

2. `analyze_candles(cs, atr, ctx)` — puts each hit in context and folds
   everything into ONE factor for analysis.confirmation:

       final = quality × context × confirmation × (1 − conflict) × enabled

   * context      — reversal: at the right-side level (bullish at support,
                    bearish at resistance), after a move INTO the pattern,
                    better in a range; continuation: with the higher-TF
                    trend, better in a trend, worse right under a level.
   * confirmation — a pattern that ended on the previous bar is confirmed
                    (×1.0) or denied (dropped) by the last closed bar; one
                    that ended on the last bar is still pending (×0.5).
                    Three-bar formations carry their own confirmation.
   * conflict     — higher-TF trend, market structure, momentum and a close
                    opposing level all against the pattern shrink it to 0.
   * enabled      — per-pattern switch; a pattern that doesn't beat chance
                    out-of-sample is kept at 0 (still detected and shown).

   Anti double-counting: several patterns usually describe the same bars
   (Engulfing + Outside Bar + Three Outside Up…). Per direction only the
   strongest counts, the rest add at most +15% in total; bullish and bearish
   net out; the result is clamped to −1..+1. If the liquidity-sweep or
   level-reaction factor already fired the same way (a pin bar at a level IS
   that bounce), the pattern score is halved. Indecision patterns (Doji,
   Spinning Top, Inside Bar…) never give a direction.

The factor's weight and overall influence cap live in analysis.confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from market_data.models import Candle

REVERSAL = "reversal"
CONTINUATION = "continuation"
INDECISION = "indecision"

BULL = 1
BEAR = -1
NEUTRAL = 0


@dataclass(frozen=True)
class PatternHit:
    name: str
    direction: int  # +1 bullish, -1 bearish, 0 neutral
    kind: str
    start: int
    end: int
    quality: float  # 0..1, how clean the shape is
    self_confirmed: bool = False  # 3-bar formations include their confirmation


@dataclass(frozen=True)
class PatternSignal:
    """What the spec calls the structured pattern result."""

    pattern: str
    direction: str  # "UP" | "DOWN" | "NEUTRAL"
    kind: str
    strength: float  # geometry quality 0..1
    confirmed: bool | None  # None = pending (formed on the last bar)
    reversal: bool
    near_level: bool
    after_impulse: bool
    regime: str  # "trend" | "range"
    trend_conflict: bool  # against the higher-timeframe trend
    context_score: float  # 0..1
    conflict: float  # 0..1
    score: float  # signed final contribution before aggregation, −1..+1
    end: int

    @property
    def sign(self) -> int:
        return 1 if self.direction == "UP" else -1 if self.direction == "DOWN" else 0


@dataclass(frozen=True)
class CandleContext:
    levels: list[float]  # horizontal levels (key levels, pools, swings)
    atr: float
    regime: float  # 0 range .. 1 trend
    htf_trend: int  # +1 / -1 / 0
    structure: float  # analysis.structure "structure" score
    impulse: float  # analysis.structure "impulse" score
    overlap: float = 0.0  # signed: liquidity_sweep/level_reaction already firing


@dataclass(frozen=True)
class CandleReport:
    score: float  # the factor, −1..+1
    signals: list[PatternSignal] = field(default_factory=list)
    indecision: list[str] = field(default_factory=list)

    @property
    def active(self) -> list[PatternSignal]:
        return [s for s in self.signals if abs(s.score) > 0.01]


# ---------------------------------------------------------------------------
# geometry helpers

def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _rng(c: Candle) -> float:
    return c.high - c.low


def _upper(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def _lower(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def _bull(c: Candle) -> bool:
    return c.close > c.open


def _bear(c: Candle) -> bool:
    return c.close < c.open


def _mid(c: Candle) -> float:
    return (c.open + c.close) / 2


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _averages(cs: list[Candle], end: int, n: int = 10) -> tuple[float, float]:
    window = cs[max(0, end - n) : end] or cs[: end + 1]
    avg_body = sum(_body(c) for c in window) / len(window)
    avg_rng = sum(_rng(c) for c in window) / len(window)
    return max(avg_body, 1e-12), max(avg_rng, 1e-12)


def prior_move(cs: list[Candle], start: int, atr: float, lookback: int = 5) -> float:
    """Price change over the `lookback` bars before the pattern, in ATRs."""
    if start - 1 - lookback < 0 or atr <= 0:
        return 0.0
    return (cs[start - 1].close - cs[start - 1 - lookback].close) / atr


# ---------------------------------------------------------------------------
# detection

def detect_patterns(cs: list[Candle], end: int, atr: float) -> list[PatternHit]:
    """Every formation whose LAST bar is `end`. Reads only cs[..end]."""
    if end < 5 or end >= len(cs) or atr <= 0:
        return []
    hits: list[PatternHit] = []
    c = cs[end]
    p = cs[end - 1]
    a = cs[end - 2]
    avg_body, avg_rng = _averages(cs, end)
    move = prior_move(cs, end, atr)  # move into the last bar

    def add(name, direction, kind, length, quality, self_confirmed=False):
        hits.append(PatternHit(name, direction, kind, end - length + 1, end, _clamp(quality), self_confirmed))

    rng = _rng(c)
    if rng <= 0:
        return hits
    body, up, lo = _body(c), _upper(c), _lower(c)
    big_bar = rng >= 0.5 * avg_rng  # ignore dust bars

    # --- single bar -------------------------------------------------------
    if big_bar and body <= 0.1 * rng:
        if lo >= 0.6 * rng and up <= 0.1 * rng:
            add("Dragonfly Doji", BULL, REVERSAL, 1, 0.5 + 0.5 * lo / rng)
        elif up >= 0.6 * rng and lo <= 0.1 * rng:
            add("Gravestone Doji", BEAR, REVERSAL, 1, 0.5 + 0.5 * up / rng)
        elif up >= 0.3 * rng and lo >= 0.3 * rng and rng >= 1.3 * avg_rng:
            add("Long Legged Doji", NEUTRAL, INDECISION, 1, 0.6)
        else:
            add("Doji", NEUTRAL, INDECISION, 1, 0.5)
    elif big_bar and body <= 0.3 * rng and up >= 0.25 * rng and lo >= 0.25 * rng:
        add("Spinning Top", NEUTRAL, INDECISION, 1, 0.5)

    if body >= 0.9 * rng and body >= 1.2 * avg_body:
        add("Marubozu", BULL if _bull(c) else BEAR, CONTINUATION, 1, _clamp(body / (2 * avg_body)))

    if big_bar and body >= 0.08 * rng:
        # hammer shape: long lower wick, tiny upper
        if lo >= 2 * body and up <= 0.15 * rng:
            q = _clamp(lo / rng)
            if move <= -0.5:
                add("Hammer", BULL, REVERSAL, 1, q)
            elif move >= 0.5:
                add("Hanging Man", BEAR, REVERSAL, 1, q * 0.8)
        # inverted shape: long upper wick, tiny lower
        if up >= 2 * body and lo <= 0.15 * rng:
            q = _clamp(up / rng)
            if move >= 0.5:
                add("Shooting Star", BEAR, REVERSAL, 1, q)
            elif move <= -0.5:
                add("Inverted Hammer", BULL, REVERSAL, 1, q * 0.8)

    # pin bar: dominant wick + the nose sticks out beyond the previous bar
    if big_bar and body <= 0.25 * rng:
        if lo >= 0.66 * rng and c.low < p.low:
            add("Pin Bar", BULL, REVERSAL, 1, lo / rng)
        elif up >= 0.66 * rng and c.high > p.high:
            add("Pin Bar", BEAR, REVERSAL, 1, up / rng)
        # weaker cousin: long wick without the protruding nose
        elif lo >= 0.5 * rng and up <= 0.2 * rng and rng >= avg_rng:
            add("Rejection Wick", BULL, REVERSAL, 1, 0.4 + 0.4 * lo / rng)
        elif up >= 0.5 * rng and lo <= 0.2 * rng and rng >= avg_rng:
            add("Rejection Wick", BEAR, REVERSAL, 1, 0.4 + 0.4 * up / rng)

    # belt hold: opens at the extreme and drives the other way
    if body >= 0.6 * rng and body >= 1.3 * avg_body:
        if _bull(c) and lo <= 0.05 * rng and move <= -0.5:
            add("Bullish Belt Hold", BULL, REVERSAL, 1, _clamp(body / (2 * avg_body)))
        elif _bear(c) and up <= 0.05 * rng and move >= 0.5:
            add("Bearish Belt Hold", BEAR, REVERSAL, 1, _clamp(body / (2 * avg_body)))

    # --- two bars -----------------------------------------------------------
    pb = _body(p)
    if _bear(p) and _bull(c) and c.close >= p.open and c.open <= p.close and body > pb and pb > 0.1 * avg_body:
        add("Bullish Engulfing", BULL, REVERSAL, 2, _clamp(0.5 + 0.5 * (body - pb) / max(body, 1e-12) + 0.2 * (body > avg_body)))
    if _bull(p) and _bear(c) and c.close <= p.open and c.open >= p.close and body > pb and pb > 0.1 * avg_body:
        add("Bearish Engulfing", BEAR, REVERSAL, 2, _clamp(0.5 + 0.5 * (body - pb) / max(body, 1e-12) + 0.2 * (body > avg_body)))

    if pb >= avg_body and body <= 0.6 * pb and max(c.open, c.close) <= max(p.open, p.close) and min(c.open, c.close) >= min(p.open, p.close):
        if _bear(p) and not _bear(c):
            add("Bullish Harami", BULL, REVERSAL, 2, 0.4 + 0.4 * (1 - body / pb))
        elif _bull(p) and not _bull(c):
            add("Bearish Harami", BEAR, REVERSAL, 2, 0.4 + 0.4 * (1 - body / pb))

    if _bear(p) and pb >= avg_body and _bull(c) and c.open <= p.close and _mid(p) < c.close < p.open:
        add("Piercing Line", BULL, REVERSAL, 2, 0.5 + 0.5 * (c.close - _mid(p)) / max(p.open - _mid(p), 1e-12))
    if _bull(p) and pb >= avg_body and _bear(c) and c.open >= p.close and p.open < c.close < _mid(p):
        add("Dark Cloud Cover", BEAR, REVERSAL, 2, 0.5 + 0.5 * (_mid(p) - c.close) / max(_mid(p) - p.open, 1e-12))

    tweezer_tol = 0.1 * avg_rng
    if abs(p.low - c.low) <= tweezer_tol and _bear(p) and _bull(c) and move <= -0.3:
        add("Tweezer Bottom", BULL, REVERSAL, 2, 0.6)
    if abs(p.high - c.high) <= tweezer_tol and _bull(p) and _bear(c) and move >= 0.3:
        add("Tweezer Top", BEAR, REVERSAL, 2, 0.6)

    if c.high <= p.high and c.low >= p.low and _rng(p) > 0:
        add("Inside Bar", NEUTRAL, INDECISION, 2, 0.5)
    if c.high > p.high and c.low < p.low:
        pos = (c.close - c.low) / rng
        direction = BULL if pos >= 0.7 else BEAR if pos <= 0.3 else NEUTRAL
        add("Outside Bar", direction, REVERSAL if direction else INDECISION, 2, 0.4 + 0.4 * abs(pos - 0.5) * 2)

    if _bear(p) and pb >= 0.6 * _rng(p) and _bull(c) and body >= 0.6 * rng and c.open >= p.open:
        add("Bullish Kicker", BULL, REVERSAL, 2, 0.8)
    if _bull(p) and pb >= 0.6 * _rng(p) and _bear(c) and body >= 0.6 * rng and c.open <= p.open:
        add("Bearish Kicker", BEAR, REVERSAL, 2, 0.8)

    # --- three bars ---------------------------------------------------------
    ab = _body(a)
    if _bear(a) and ab >= avg_body and _body(p) <= 0.35 * ab and _bull(c) and c.close > _mid(a):
        add("Morning Star", BULL, REVERSAL, 3, 0.6 + 0.4 * _clamp((c.close - _mid(a)) / max(ab, 1e-12) * 2), True)
    if _bull(a) and ab >= avg_body and _body(p) <= 0.35 * ab and _bear(c) and c.close < _mid(a):
        add("Evening Star", BEAR, REVERSAL, 3, 0.6 + 0.4 * _clamp((_mid(a) - c.close) / max(ab, 1e-12) * 2), True)

    three = (a, p, c)
    if all(_bull(x) for x in three) and a.close < p.close < c.close and all(_body(x) >= 0.6 * avg_body for x in three) \
            and p.open >= a.open and c.open >= p.open and all(_upper(x) <= 0.35 * _body(x) for x in three):
        add("Three White Soldiers", BULL, CONTINUATION, 3, 0.8, True)
    if all(_bear(x) for x in three) and a.close > p.close > c.close and all(_body(x) >= 0.6 * avg_body for x in three) \
            and p.open <= a.open and c.open <= p.open and all(_lower(x) <= 0.35 * _body(x) for x in three):
        add("Three Black Crows", BEAR, CONTINUATION, 3, 0.8, True)

    a_top, a_bot = max(a.open, a.close), min(a.open, a.close)
    if _bear(a) and ab >= avg_body and _bull(p) and max(p.open, p.close) <= a_top and min(p.open, p.close) >= a_bot and _bull(c) and c.close > a.open:
        add("Three Inside Up", BULL, REVERSAL, 3, 0.75, True)
    if _bull(a) and ab >= avg_body and _bear(p) and max(p.open, p.close) <= a_top and min(p.open, p.close) >= a_bot and _bear(c) and c.close < a.open:
        add("Three Inside Down", BEAR, REVERSAL, 3, 0.75, True)
    if _bear(a) and _bull(p) and p.close >= a.open and p.open <= a.close and _body(p) > ab and _bull(c) and c.close > p.close:
        add("Three Outside Up", BULL, REVERSAL, 3, 0.8, True)
    if _bull(a) and _bear(p) and p.close <= a.open and p.open >= a.close and _body(p) > ab and _bear(c) and c.close < p.close:
        add("Three Outside Down", BEAR, REVERSAL, 3, 0.8, True)

    # --- five bars: rising / falling three methods --------------------------
    if end >= 4:
        f, m1, m2, m3 = cs[end - 4], cs[end - 3], cs[end - 2], cs[end - 1]
        mids = (m1, m2, m3)
        if _bull(f) and _body(f) >= avg_body and _bull(c) and body >= avg_body and c.close > f.close \
                and all(x.high <= f.high and x.low >= f.low for x in mids) and sum(_bear(x) for x in mids) >= 2:
            add("Rising Three Methods", BULL, CONTINUATION, 5, 0.8, True)
        if _bear(f) and _body(f) >= avg_body and _bear(c) and body >= avg_body and c.close < f.close \
                and all(x.high <= f.high and x.low >= f.low for x in mids) and sum(_bull(x) for x in mids) >= 2:
            add("Falling Three Methods", BEAR, CONTINUATION, 5, 0.8, True)

    return hits


ALL_PATTERNS = (
    "Doji", "Dragonfly Doji", "Gravestone Doji", "Long Legged Doji", "Spinning Top", "Marubozu",
    "Hammer", "Inverted Hammer", "Hanging Man", "Shooting Star", "Pin Bar", "Rejection Wick",
    "Bullish Belt Hold", "Bearish Belt Hold",
    "Bullish Engulfing", "Bearish Engulfing", "Bullish Harami", "Bearish Harami",
    "Piercing Line", "Dark Cloud Cover", "Tweezer Top", "Tweezer Bottom",
    "Inside Bar", "Outside Bar", "Bullish Kicker", "Bearish Kicker",
    "Morning Star", "Evening Star", "Three White Soldiers", "Three Black Crows",
    "Three Inside Up", "Three Inside Down", "Three Outside Up", "Three Outside Down",
    "Rising Three Methods", "Falling Three Methods",
)


# ---------------------------------------------------------------------------
# confirmation & context

def confirmation_state(hit: PatternHit, cs: list[Candle], last: int) -> bool | None:
    """True/False once the bar after the pattern has closed, None while the
    pattern ended on the last closed bar. Only bars ≤ `last` are read."""
    if hit.self_confirmed:
        return True
    if hit.end >= last:
        return None
    nxt, pat = cs[hit.end + 1], cs[hit.end]
    if hit.direction == BULL:
        return nxt.close > pat.close and nxt.close > nxt.open
    if hit.direction == BEAR:
        return nxt.close < pat.close and nxt.close < nxt.open
    return None


def _near_right_level(hit: PatternHit, cs: list[Candle], ctx: CandleContext) -> bool:
    span = cs[hit.start : hit.end + 1]
    tol = 0.3 * ctx.atr
    if hit.direction == BULL:
        low = min(x.low for x in span)
        return any(abs(low - lv) <= tol or (low < lv <= min(x.close for x in span)) for lv in ctx.levels)
    if hit.direction == BEAR:
        high = max(x.high for x in span)
        return any(abs(high - lv) <= tol or (max(x.close for x in span) <= lv < high) for lv in ctx.levels)
    return False


def _opposing_level_close(hit: PatternHit, cs: list[Candle], ctx: CandleContext) -> bool:
    price = cs[-1].close
    if hit.direction == BULL:
        return any(0 < lv - price <= 0.3 * ctx.atr for lv in ctx.levels)
    if hit.direction == BEAR:
        return any(0 < price - lv <= 0.3 * ctx.atr for lv in ctx.levels)
    return False


def evaluate_hit(hit: PatternHit, cs: list[Candle], ctx: CandleContext, last: int,
                 enabled: dict[str, float] | None = None) -> PatternSignal:
    d = hit.direction
    move = prior_move(cs, hit.start, ctx.atr)
    near = _near_right_level(hit, cs, ctx)
    after_impulse = abs(move) >= 1.5
    confirmed = confirmation_state(hit, cs, last)
    trend_conflict = d != 0 and ctx.htf_trend == -d

    if hit.kind == REVERSAL:
        location = 1.0 if near else 0.4
        # needs a move INTO the pattern (against its direction)
        into = _clamp(-d * move / 1.5)
        prior = 0.4 + 0.6 * into
        regime_f = 1.0 - 0.3 * ctx.regime
        context = location * prior * regime_f
    elif hit.kind == CONTINUATION:
        align = 1.0 if ctx.htf_trend == d else 0.3 if ctx.htf_trend == -d else 0.6
        regime_f = 0.6 + 0.4 * ctx.regime
        blocked = 0.5 if _opposing_level_close(hit, cs, ctx) else 1.0
        context = align * regime_f * blocked
    else:
        context = 0.0
    context = _clamp(context)

    # conflict with the rest of the market picture
    conflict = 0.0
    if d != 0:
        if trend_conflict:
            conflict += 0.35
        if ctx.structure * d <= -0.45:
            conflict += 0.25
        if hit.kind != REVERSAL and ctx.impulse * d <= -0.5:
            conflict += 0.2  # a reversal is SUPPOSED to follow an opposite move
        if _opposing_level_close(hit, cs, ctx):
            conflict += 0.2
    conflict = _clamp(conflict)

    conf_mult = 1.0 if confirmed is True else 0.0 if confirmed is False else 0.5
    weight = (enabled or {}).get(hit.name, 1.0)
    score = d * hit.quality * context * conf_mult * (1.0 - conflict) * weight

    return PatternSignal(
        pattern=hit.name,
        direction="UP" if d > 0 else "DOWN" if d < 0 else "NEUTRAL",
        kind=hit.kind,
        strength=round(hit.quality, 3),
        confirmed=confirmed,
        reversal=hit.kind == REVERSAL,
        near_level=near,
        after_impulse=after_impulse,
        regime="trend" if ctx.regime >= 0.5 else "range",
        trend_conflict=trend_conflict,
        context_score=round(context, 3),
        conflict=round(conflict, 3),
        score=score,
        end=hit.end,
    )


# Extra patterns in the same direction add at most this much in total.
_DUPLICATE_BONUS_CAP = 0.15


def aggregate(signals: list[PatternSignal], overlap: float = 0.0) -> float:
    """One number for all patterns: strongest per direction + ≤15% for the
    rest, bullish minus bearish, halved where an existing factor (sweep /
    level reaction) already covers the same move, clamped to −1..+1."""
    def side(sign: int) -> float:
        vals = sorted((abs(s.score) for s in signals if s.sign == sign and abs(s.score) > 0), reverse=True)
        if not vals:
            return 0.0
        extra = min(_DUPLICATE_BONUS_CAP, 0.1 * sum(vals[1:]))
        return vals[0] * (1.0 + extra)

    bull, bear = side(1), side(-1)
    if overlap > 0.3:
        bull *= 0.5
    elif overlap < -0.3:
        bear *= 0.5
    return max(-1.0, min(1.0, bull - bear))


def analyze_candles(cs: list[Candle], ctx: CandleContext, enabled: dict[str, float] | None = None) -> CandleReport:
    """Patterns ending on the last two CLOSED bars (the older ones get
    their confirmation from the newest), in context, folded into one
    factor. `cs` must contain closed candles only."""
    last = len(cs) - 1
    if last < 6 or ctx.atr <= 0:
        return CandleReport(0.0)
    signals: list[PatternSignal] = []
    indecision: list[str] = []
    for end in (last - 1, last):
        for hit in detect_patterns(cs, end, ctx.atr):
            if hit.direction == NEUTRAL:
                if end == last:
                    indecision.append(hit.name)
                continue
            signals.append(evaluate_hit(hit, cs, ctx, last, enabled))
    return CandleReport(aggregate(signals, ctx.overlap), signals, indecision)


def describe(signal: PatternSignal) -> str:
    dir_text = {"UP": "бычий", "DOWN": "медвежий"}.get(signal.direction, "нейтральный")
    bits = []
    if signal.near_level:
        bits.append("у уровня")
    if signal.confirmed is True:
        bits.append("подтверждён")
    elif signal.confirmed is None:
        bits.append("ждёт подтверждения")
    if signal.after_impulse:
        bits.append("после сильного движения")
    if signal.trend_conflict:
        bits.append("против старшего тренда")
    tail = f" ({', '.join(bits)})" if bits else ""
    return f"Паттерн {signal.pattern} — {dir_text}{tail}, контекст {signal.context_score:.0%}"
