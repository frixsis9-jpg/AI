"""Market-structure reading: liquidity, retests, breakouts, imbalances.

Everything here works on CLOSED candles only (a forming bar would make every
reading flap tick by tick) and is scaled by ATR, so the same thresholds work
on EUR/USD, USD/JPY and OTC synthetics alike. Each factor is graded on a
continuous -1 (bearish) .. +1 (bullish) scale and — when it fired for a
concrete reason — also comes with a human-readable `Finding` that the bot
shows to the user as the "why" behind the probability.

Factors:

* `structure`       — swing structure (HH/HL vs LH/LL) plus a fresh break of
                      the last swing: BOS (continuation) or CHoCH (the first
                      break against the prior structure — early reversal).
* `liquidity_sweep` — stop hunt: a wick through a liquidity pool (equal
                      highs/lows, or the range extreme) that closes back
                      inside. Stops above highs got taken → reversal down,
                      and vice versa.
* `level_reaction`  — breakout + retest (continuation), false breakout
                      (close beyond a level, then back inside → reversal),
                      or a rejection bounce off a level touched ≥2 times.
* `fvg`             — price returning into an unfilled fair-value gap
                      (imbalance), which tends to act as support/resistance.
* `impulse`         — short-term momentum of the last 3 bars.
* `exhaustion`      — a move that has run too far / too many bars in a row
                      without a pullback (mean-reversion pressure).
* `candle_pressure` — who controlled the last 5 bars (close location in the
                      range, body direction), recent bars weighted more.
* `overextension`   — distance from EMA20 in ATRs (mean reversion).
* `divergence`      — RSI divergence at the last two swings.
* `room`            — free space to the nearest opposing level: a CALL right
                      under resistance has little room to work.

`regime` (0 = range, 1 = trend) comes from Kaufman's efficiency ratio;
analysis.confirmation uses it to blend trend-following vs mean-reversion
weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from analysis.indicators import ema, rsi
from analysis.retest import RetestInfo, VolatilityContext, analyze_retest, volatility_context
from market_data.models import Candle

SWING_WINDOW = 2
LOOKBACK = 120


@dataclass(frozen=True)
class Finding:
    factor: str
    score: float  # -1..+1, sign = direction
    text: str


@dataclass(frozen=True)
class LiquidityPool:
    price: float
    side: str  # "buy" (stops above highs) | "sell" (stops below lows)
    touches: int
    last_index: int


@dataclass(frozen=True)
class StructureReport:
    scores: dict[str, float]
    findings: list[Finding] = field(default_factory=list)
    efficiency: float = 0.0
    regime: float = 0.0  # 0 = range .. 1 = trend
    pools: list[LiquidityPool] = field(default_factory=list)
    # every horizontal reference price found: key levels, liquidity pools
    # and the last swing highs/lows (used by analysis.candlestick_patterns)
    levels: list[float] = field(default_factory=list)
    # the breakout-retest story of the most relevant level, if any (see
    # analysis.retest), and the volatility it was judged against
    retest: RetestInfo | None = None
    volatility: VolatilityContext | None = None


FACTORS = (
    "structure",
    "liquidity_sweep",
    "level_reaction",
    "fvg",
    "impulse",
    "exhaustion",
    "candle_pressure",
    "overextension",
    "divergence",
    "room",
)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _sign(v: float) -> int:
    return 1 if v > 0 else -1 if v < 0 else 0


def _fmt(price: float) -> str:
    return f"{price:.5f}" if price < 50 else f"{price:.3f}"


def swing_points(cs: list[Candle], w: int = SWING_WINDOW) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed fractal swing highs/lows as (index, price)."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(w, len(cs) - w):
        window = cs[i - w : i + w + 1]
        if cs[i].high >= max(c.high for c in window) and cs[i].high > cs[i - 1].high:
            highs.append((i, cs[i].high))
        if cs[i].low <= min(c.low for c in window) and cs[i].low < cs[i - 1].low:
            lows.append((i, cs[i].low))
    return highs, lows


def _cluster(points: list[tuple[int, float]], tol: float) -> list[list[tuple[int, float]]]:
    clusters: list[list[tuple[int, float]]] = []
    for idx, price in sorted(points, key=lambda p: p[1]):
        if clusters and abs(clusters[-1][-1][1] - price) <= tol:
            clusters[-1].append((idx, price))
        else:
            clusters.append([(idx, price)])
    return clusters


def liquidity_pools(cs: list[Candle], highs, lows, atr: float) -> list[LiquidityPool]:
    """Equal highs/lows (≥2 swings within 0.15 ATR) plus the range extremes
    of the last 40 bars — the places where stop orders cluster."""
    tol = atr * 0.15
    pools: list[LiquidityPool] = []
    for group in _cluster(highs, tol):
        if len(group) >= 2:
            pools.append(LiquidityPool(max(p for _, p in group), "buy", len(group), max(i for i, _ in group)))
    for group in _cluster(lows, tol):
        if len(group) >= 2:
            pools.append(LiquidityPool(min(p for _, p in group), "sell", len(group), max(i for i, _ in group)))
    n = len(cs)
    recent_highs = [h for h in highs if h[0] >= n - 40]
    recent_lows = [l for l in lows if l[0] >= n - 40]
    if recent_highs:
        i, p = max(recent_highs, key=lambda h: h[1])
        if not any(pl.side == "buy" and abs(pl.price - p) <= tol for pl in pools):
            pools.append(LiquidityPool(p, "buy", 1, i))
    if recent_lows:
        i, p = min(recent_lows, key=lambda l: l[1])
        if not any(pl.side == "sell" and abs(pl.price - p) <= tol for pl in pools):
            pools.append(LiquidityPool(p, "sell", 1, i))
    return pools


def key_levels(highs, lows, atr: float) -> list[tuple[float, int]]:
    """Horizontal levels touched by ≥2 swings of EITHER kind (a broken
    resistance becomes support) as (price, touches)."""
    levels = []
    for group in _cluster(highs + lows, atr * 0.25):
        if len(group) >= 2:
            levels.append((sum(p for _, p in group) / len(group), len(group)))
    return levels


# --- individual factors ----------------------------------------------------

def _market_structure(cs, highs, lows, atr) -> tuple[float, Finding | None]:
    if len(highs) < 2 or len(lows) < 2:
        return 0.0, None
    (_, h1), (hi2, h2) = highs[-2], highs[-1]
    (_, l1), (li2, l2) = lows[-2], lows[-1]
    bias = 1 if (h2 > h1 and l2 > l1) else -1 if (h2 < h1 and l2 < l1) else 0
    n = len(cs)

    def fresh_break(level: float, after: int, up: bool) -> int | None:
        for j in range(after + 1, n):
            beyond = cs[j].close > level if up else cs[j].close < level
            if beyond:
                return j
        return None

    up_j = fresh_break(h2, hi2, True)
    dn_j = fresh_break(l2, li2, False)
    # a break only counts if price is still beyond the level now
    if up_j is not None and cs[-1].close <= h2:
        up_j = None
    if dn_j is not None and cs[-1].close >= l2:
        dn_j = None
    if up_j is not None and dn_j is not None:
        if up_j > dn_j:
            dn_j = None
        else:
            up_j = None

    if up_j is not None and n - 1 - up_j <= 6:
        if bias < 0:
            return 0.7, Finding("structure", 0.7, f"CHoCH вверх: сломан последний максимум {_fmt(h2)} после нисходящей структуры — ранний разворот")
        return 0.9, Finding("structure", 0.9, f"BOS вверх: пробит максимум {_fmt(h2)}, структура HH/HL продолжается")
    if dn_j is not None and n - 1 - dn_j <= 6:
        if bias > 0:
            return -0.7, Finding("structure", -0.7, f"CHoCH вниз: сломан последний минимум {_fmt(l2)} после восходящей структуры — ранний разворот")
        return -0.9, Finding("structure", -0.9, f"BOS вниз: пробит минимум {_fmt(l2)}, структура LH/LL продолжается")
    if bias > 0:
        return 0.45, Finding("structure", 0.45, "Структура восходящая: повышающиеся максимумы и минимумы")
    if bias < 0:
        return -0.45, Finding("structure", -0.45, "Структура нисходящая: понижающиеся максимумы и минимумы")
    return 0.0, None


def _liquidity_sweep(cs, pools: list[LiquidityPool], atr) -> tuple[float, Finding | None]:
    n = len(cs)
    best: tuple[float, Finding] | None = None
    decay = (1.0, 0.8, 0.6)
    for age in range(3):
        k = n - 1 - age
        if k < 1:
            break
        c = cs[k]
        rng = c.high - c.low
        if rng <= 0:
            continue
        for pool in pools:
            if pool.last_index >= k:
                continue
            between = cs[pool.last_index + 1 : k]
            if pool.side == "buy":
                if not (c.high > pool.price and c.close < pool.price):
                    continue
                if any(b.high > pool.price for b in between):
                    continue  # this liquidity was already taken earlier
                if any(x.close > pool.price for x in cs[k + 1 :]):
                    continue  # accepted above after the sweep — invalidated
                wick = (c.high - max(c.open, c.close)) / rng
                strength = 0.55 + 0.15 * min(pool.touches - 1, 2) + (0.15 if wick >= 0.45 else 0.0)
                score = -min(1.0, strength) * decay[age]
                what = "равных максимумов" if pool.touches >= 2 else "локального максимума"
                text = (f"Снятие ликвидности над уровнем {what} {_fmt(pool.price)}"
                        f"{f' ({pool.touches} касания)' if pool.touches >= 2 else ''}: прокол тенью и возврат — стопы собраны, ожидается движение вниз")
            else:
                if not (c.low < pool.price and c.close > pool.price):
                    continue
                if any(b.low < pool.price for b in between):
                    continue
                if any(x.close < pool.price for x in cs[k + 1 :]):
                    continue
                wick = (min(c.open, c.close) - c.low) / rng
                strength = 0.55 + 0.15 * min(pool.touches - 1, 2) + (0.15 if wick >= 0.45 else 0.0)
                score = min(1.0, strength) * decay[age]
                what = "равных минимумов" if pool.touches >= 2 else "локального минимума"
                text = (f"Снятие ликвидности под уровнем {what} {_fmt(pool.price)}"
                        f"{f' ({pool.touches} касания)' if pool.touches >= 2 else ''}: прокол тенью и возврат — стопы собраны, ожидается движение вверх")
            if best is None or abs(score) > abs(best[0]):
                best = (score, Finding("liquidity_sweep", score, text))
    return best if best else (0.0, None)


def _level_reaction(
    cs, levels: list[tuple[float, int]], atr, retest: RetestInfo | None = None
) -> tuple[float, Finding | None]:
    n = len(cs)
    last = cs[-1]
    best: tuple[float, Finding] | None = None

    def consider(score: float, text: str) -> None:
        nonlocal best
        if best is None or abs(score) > abs(best[0]):
            best = (score, Finding("level_reaction", score, text))

    # breakout → thrust → pullback → reaction (or its failure), judged against
    # volatility and the market regime — see analysis.retest
    if retest is not None and retest.score:
        consider(retest.score, retest.text)

    for level, touches in levels:
        weight = min(1.0, 0.75 + 0.1 * (touches - 2))
        # false breakout (close beyond, then back inside) within last 3 bars
        for k in range(max(1, n - 3), n - 1):
            if cs[k].close < level - 0.05 * atr and cs[k - 1].close > level and last.close > level:
                consider(0.75 * weight, f"Ложный пробой уровня {_fmt(level)} вниз: цена вернулась выше — ловушка для продавцов")
            if cs[k].close > level + 0.05 * atr and cs[k - 1].close < level and last.close < level:
                consider(-0.75 * weight, f"Ложный пробой уровня {_fmt(level)} вверх: цена вернулась ниже — ловушка для покупателей")
        # rejection bounce off the level on the last bar
        rng = last.high - last.low
        if rng > 0:
            lower_wick = (min(last.open, last.close) - last.low) / rng
            upper_wick = (last.high - max(last.open, last.close)) / rng
            if abs(last.low - level) <= 0.2 * atr and last.close > level and lower_wick >= 0.4:
                consider(0.35 + 0.1 * min(touches, 4), f"Отбой от поддержки {_fmt(level)} ({touches} касания) с длинной нижней тенью")
            if abs(last.high - level) <= 0.2 * atr and last.close < level and upper_wick >= 0.4:
                consider(-(0.35 + 0.1 * min(touches, 4)), f"Отбой от сопротивления {_fmt(level)} ({touches} касания) с длинной верхней тенью")
    return best if best else (0.0, None)


def _fvg(cs, atr) -> tuple[float, Finding | None]:
    n = len(cs)
    last = cs[-1]
    for i in range(n - 2, max(2, n - 30) - 1, -1):
        a, c = cs[i - 2], cs[i]
        if c.low - a.high >= 0.15 * atr:  # bullish gap [a.high, c.low]
            bottom, top = a.high, c.low
            later = cs[i + 1 :]
            if later and min(x.low for x in later) < bottom:
                continue  # fully filled
            if later and last.low <= top and last.close >= bottom:
                return 0.6, Finding("fvg", 0.6, f"Цена вернулась в бычий имбаланс (FVG) {_fmt(bottom)}–{_fmt(top)} и держится в нём — зона спроса")
        if a.low - c.high >= 0.15 * atr:  # bearish gap [c.high, a.low]
            bottom, top = c.high, a.low
            later = cs[i + 1 :]
            if later and max(x.high for x in later) > top:
                continue
            if later and last.high >= bottom and last.close <= top:
                return -0.6, Finding("fvg", -0.6, f"Цена вернулась в медвежий имбаланс (FVG) {_fmt(bottom)}–{_fmt(top)} и держится в нём — зона предложения")
    return 0.0, None


def _impulse_and_exhaustion(cs, atr) -> tuple[float, float, list[Finding]]:
    findings: list[Finding] = []
    closes = [c.close for c in cs]
    move3 = (closes[-1] - closes[-4]) / atr
    impulse = _clamp(move3 / 2.0)
    if abs(impulse) >= 0.6:
        findings.append(Finding("impulse", impulse, f"Сильный импульс {'вверх' if impulse > 0 else 'вниз'} за последние 3 свечи ({abs(move3):.1f} ATR)"))

    streak = 0
    direction = _sign(cs[-1].close - cs[-1].open)
    for c in reversed(cs):
        if direction != 0 and _sign(c.close - c.open) == direction:
            streak += 1
        else:
            break
    move8 = (closes[-1] - closes[-9]) / atr
    exhaustion = 0.0
    if streak >= 5:
        exhaustion -= direction * min(1.0, 0.3 + 0.2 * (streak - 5))
    if abs(move8) > 3.0:
        exhaustion -= _sign(move8) * min(1.0, 0.3 + (abs(move8) - 3.0) / 3.0)
    # fading bodies in the direction of the move
    bodies = [abs(c.close - c.open) for c in cs[-4:]]
    if streak >= 3 and bodies[-1] < 0.5 * (sum(bodies[:-1]) / 3):
        exhaustion -= direction * 0.25
    exhaustion = _clamp(exhaustion)
    if abs(exhaustion) >= 0.3:
        findings.append(Finding("exhaustion", exhaustion,
                                f"Истощение движения {'вверх' if exhaustion < 0 else 'вниз'}: {streak} свечей подряд / {abs(move8):.1f} ATR за 8 свечей — вероятен откат"))
    return impulse, exhaustion, findings


def _candle_pressure(cs) -> tuple[float, Finding | None]:
    recent = cs[-5:]
    weights = (1, 1, 2, 3, 4)[-len(recent):]
    clv_sum = 0.0
    body_sum = 0.0
    range_sum = 0.0
    for c, w in zip(recent, weights):
        rng = c.high - c.low
        if rng > 0:
            clv_sum += w * ((c.close - c.low) - (c.high - c.close)) / rng
        body_sum += w * (c.close - c.open)
        range_sum += w * rng
    clv = clv_sum / sum(weights)
    body = body_sum / range_sum if range_sum else 0.0
    score = _clamp(0.6 * clv + 0.4 * body * 2)
    if abs(score) >= 0.45:
        who = "покупатели" if score > 0 else "продавцы"
        return score, Finding("candle_pressure", score, f"Давление свечей: последние бары контролируют {who} (закрытия у {'максимумов' if score > 0 else 'минимумов'})")
    return score, None


def _overextension(cs, atr) -> tuple[float, Finding | None]:
    closes = [c.close for c in cs]
    e20 = ema(closes, 20)[-1]
    if e20 is None:
        return 0.0, None
    z = (closes[-1] - e20) / atr
    if abs(z) <= 1.2:
        return 0.0, None
    score = -_sign(z) * min(1.0, (abs(z) - 1.2) / 2.0)
    if abs(score) >= 0.3:
        return score, Finding("overextension", score, f"Цена ушла от EMA20 на {abs(z):.1f} ATR {'вверх' if z > 0 else 'вниз'} — перерастянута, возможен возврат к средней")
    return score, None


def _divergence(cs, highs, lows) -> tuple[float, Finding | None]:
    n = len(cs)
    r = rsi([c.close for c in cs], 14)
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        if i2 >= n - 10 and r[i1] is not None and r[i2] is not None and p2 > p1 and r[i2] < r[i1] - 3:
            return -0.7, Finding("divergence", -0.7, "Медвежья дивергенция RSI: цена обновила максимум, а RSI — нет")
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        if i2 >= n - 10 and r[i1] is not None and r[i2] is not None and p2 < p1 and r[i2] > r[i1] + 3:
            return 0.7, Finding("divergence", 0.7, "Бычья дивергенция RSI: цена обновила минимум, а RSI — нет")
    return 0.0, None


def _room(cs, levels, pools, highs, lows, atr) -> tuple[float, Finding | None]:
    price = cs[-1].close
    candidates = [p for p, _ in levels] + [pl.price for pl in pools] + [p for _, p in highs[-6:]] + [p for _, p in lows[-6:]]
    above = [p - price for p in candidates if p > price + 0.05 * atr]
    below = [price - p for p in candidates if p < price - 0.05 * atr]
    d_up = min(above) / atr if above else 3.0
    d_dn = min(below) / atr if below else 3.0
    room_up = min(d_up, 2.0) / 2.0
    room_dn = min(d_dn, 2.0) / 2.0
    score = _clamp(room_up - room_dn)
    if d_up < 0.35 and d_dn > 1.0:
        return score, Finding("room", score, f"Сопротивление совсем близко сверху ({d_up:.1f} ATR) — мало места для роста")
    if d_dn < 0.35 and d_up > 1.0:
        return score, Finding("room", score, f"Поддержка совсем близко снизу ({d_dn:.1f} ATR) — мало места для падения")
    return score, None


def efficiency_ratio(cs, period: int = 20) -> float:
    closes = [c.close for c in cs[-(period + 1):]]
    if len(closes) < 2:
        return 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / path if path else 0.0


def analyze_structure(candles: list[Candle], atr: float) -> StructureReport:
    cs = [c for c in candles if c.is_closed][-LOOKBACK:]
    if len(cs) < 30 or atr <= 0:
        return StructureReport(scores={f: 0.0 for f in FACTORS})

    highs, lows = swing_points(cs)
    pools = liquidity_pools(cs, highs, lows, atr)
    levels = key_levels(highs, lows, atr)
    er = efficiency_ratio(cs)
    vol = volatility_context(cs, atr)
    retest = analyze_retest(cs, levels, atr, vol, er)

    findings: list[Finding] = []
    scores: dict[str, float] = {}

    for name, (score, finding) in (
        ("structure", _market_structure(cs, highs, lows, atr)),
        ("liquidity_sweep", _liquidity_sweep(cs, pools, atr)),
        ("level_reaction", _level_reaction(cs, levels, atr, retest)),
        ("fvg", _fvg(cs, atr)),
    ):
        scores[name] = _clamp(score)
        if finding:
            findings.append(finding)

    impulse, exhaustion, extra = _impulse_and_exhaustion(cs, atr)
    scores["impulse"] = impulse
    scores["exhaustion"] = exhaustion
    findings += extra

    for name, (score, finding) in (
        ("candle_pressure", _candle_pressure(cs)),
        ("overextension", _overextension(cs, atr)),
        ("divergence", _divergence(cs, highs, lows)),
        ("room", _room(cs, levels, pools, highs, lows, atr)),
    ):
        scores[name] = _clamp(score)
        if finding:
            findings.append(finding)

    regime = _clamp((er - 0.15) / 0.3, 0.0, 1.0)
    ref_levels = [p for p, _ in levels] + [pl.price for pl in pools] + [p for _, p in highs[-6:]] + [p for _, p in lows[-6:]]
    return StructureReport(scores=scores, findings=findings, efficiency=er, regime=regime, pools=pools, levels=ref_levels,
                           retest=retest, volatility=vol)
