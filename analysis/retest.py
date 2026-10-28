"""Retest of a broken level — read as a sequence of stages, in context.

A real retest is a story with ordered chapters, not "price is near a level":

  1. BREAK       a bar closes beyond a key level (by more than market noise);
  2. ACCEPTANCE  closes stay on the new side (wicks through the level are fine);
  3. THRUST      price moves clearly away from the level;
  4. PULLBACK    price comes back and touches the level zone (the retest);
  5. REACTION    the last closed bar closes back in the breakout direction.

What the reader gets is a state, not just a score:

  confirmed  all five stages happened, retest on one of the last 3 bars
  failed     after the thrust, price closed back through the level — the
             breakout was a trap, the level flipped back (counter-signal)
  waiting    broke / thrust done, price hasn't come back yet — informational
  extended   price ran > 2.5 ATR away with no retest — chasing it is risky

Volatility and market context decide how far the story can be trusted:

* tolerance, break buffer and thrust distance scale with `noise`, the larger
  of the long-run ATR and the last few bars' range — in a volatility spike
  a "touch" of the level means less, so the zone widens;
* a volatility SPIKE (short-term range ≥ 1.4× normal) or a DEAD market
  (≤ 0.65×) discounts the quality — levels get overrun in spikes, and
  breakouts in a dead market are mostly noise; a reaction candle that is
  itself a spike (> 2.5 ATR) is discounted too;
* a ranging market (low efficiency) makes continuation after a breakout
  less reliable than a trending one;
* a level with more touches is stronger; a wick through the level that
  closes back on the right side (a stop-run) is a better retest than a
  gentle touch; a fresher retest is better than a stale one.

Only bars up to the last closed one are read. This module does not decide
the trade: its score feeds the existing `level_reaction` factor in
analysis.structure (with that factor's existing weight), and its text is
shown to the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from market_data.models import Candle

LOOKBACK_BARS = 20  # how far back a break may be to still matter
THRUST_ATR = 0.6  # minimum move away from the level, in `noise` units
TOUCH_ATR = 0.2  # retest zone half-width, in `noise` units
BREAK_BUFFER = 0.1  # a break must close this far beyond the level (noise units)
EXTENDED_ATR = 2.5  # farther than this from the level with no retest = "extended"
MAX_RETEST_AGE = 2  # the retest touch must be within this many bars of the last one
MAX_CROSSES = 3  # a level price keeps crossing is a noise zone, not a break
MIN_ACCEPT_BARS = 2  # a break must hold this many bars before it can 'fail'
FAIL_THRUST_ATR = 1.0  # a failed retest needs a real move away first (ATR units)
NEAR_ATR = 3.0  # waiting / failed stories matter only near the level
MAX_EXTENDED_ATR = 6.0  # beyond this the level is simply history
MIN_TOUCHES = 3  # a level formed by fewer swings is noise on a 1-minute chart
MAX_WAIT_AGE = 5  # bars after a break during which 'wait for the retest' is still news
FRESHNESS = (1.0, 0.85, 0.7)

HIGH_VOL = 1.4
LOW_VOL = 0.65


@dataclass(frozen=True)
class VolatilityContext:
    ratio: float  # short-term range / long-run range (1.0 = normal)
    noise: float  # price-noise scale: max(ATR, short-term range)
    state: str  # "high" | "normal" | "low"


@dataclass(frozen=True)
class RetestInfo:
    state: str  # "confirmed" | "failed" | "waiting" | "extended"
    direction: int  # +1 level broken upward, -1 downward (the breakout's direction)
    level: float
    touches: int  # how many swings formed this level
    quality: float  # 0..1 trust in the story (0 for waiting / extended)
    score: float  # signed factor reading: + bullish, − bearish (0 for waiting / extended)
    age: int  # bars since the break
    distance_atr: float  # current distance from the level, in ATR
    volatility: str  # "high" | "normal" | "low"
    vol_ratio: float
    text: str  # one-sentence finding for the explanation
    notes: list[str] = field(default_factory=list)  # why quality was raised / lowered


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _touches_text(n: int) -> str:
    """Russian plural: 1 касание, 2-4 касания, 5-20 касаний, 21 касание…"""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} касание"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} касания"
    return f"{n} касаний"

def _fmt(price: float) -> str:
    return f"{price:.5f}" if price < 50 else f"{price:.3f}"


def volatility_context(cs: list[Candle], atr: float) -> VolatilityContext:
    """Short-term (5 bars) vs long-run (50 bars) true range."""
    trs = []
    for i in range(1, len(cs)):
        c, p = cs[i], cs[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    if len(trs) < 10:
        return VolatilityContext(1.0, atr, "normal")
    short = sum(trs[-5:]) / 5
    long_run = sum(trs[-50:]) / len(trs[-50:])
    ratio = short / long_run if long_run > 0 else 1.0
    state = "high" if ratio >= HIGH_VOL else "low" if ratio <= LOW_VOL else "normal"
    return VolatilityContext(ratio, max(atr, short), state)


def _vol_multiplier(vol: VolatilityContext, last: Candle, atr: float, notes: list[str]) -> float:
    mult = 1.0
    if vol.state == "high":
        mult *= 0.6
        notes.append("всплеск волатильности: уровни часто проносит насквозь")
    elif vol.state == "low":
        mult *= 0.7
        notes.append("вялый рынок: пробои чаще шум")
    if atr > 0 and (last.high - last.low) > 2.5 * atr:
        mult *= 0.8
        notes.append("сама свеча реакции — выброс")
    return mult


def _regime_multiplier(efficiency: float) -> float:
    """0.85 in a pure range … 1.05 in a clean trend."""
    return 0.85 + 0.2 * _clamp((efficiency - 0.1) / 0.3)


def _last_break(cs: list[Candle], level: float, buf: float, d: int) -> int | None:
    """Index of the most recent bar that closed beyond `level` in direction
    `d` after closing on the other side, within the lookback window."""
    n = len(cs)
    for b in range(n - 1, max(1, n - LOOKBACK_BARS) - 1, -1):
        if d * (cs[b].close - level) > buf and d * (cs[b - 1].close - level) <= 0:
            return b
    return None


def _scan_level(
    cs: list[Candle], level: float, touches: int, atr: float, vol: VolatilityContext, efficiency: float, d: int
) -> RetestInfo | None:
    """The story of the latest break of `level` in direction `d`."""
    if touches < MIN_TOUCHES:
        return None
    n = len(cs)
    last = cs[-1]
    noise = vol.noise
    buf, tol, thrust = BREAK_BUFFER * noise, TOUCH_ATR * noise, THRUST_ATR * noise

    b = _last_break(cs, level, buf, d)
    if b is None:
        return None
    # a level the close keeps crossing back and forth is a noise zone — there
    # is no clean break to retest
    window = cs[-LOOKBACK_BARS:]
    crosses = sum(1 for i in range(1, len(window)) if (window[i].close - level) * (window[i - 1].close - level) < 0)
    if crosses > MAX_CROSSES:
        return None
    after = cs[b + 1 :]
    age = n - 1 - b
    distance = d * (last.close - level) / atr if atr > 0 else 0.0
    side = "вверх" if d > 0 else "вниз"
    role = "поддержка" if d > 0 else "сопротивление"
    vnote = {"high": " (всплеск волатильности — достоверность снижена)",
             "low": " (вялый рынок — достоверность снижена)"}.get(vol.state, "")

    def far(x: Candle) -> float:  # how far bar x reached in the breakout direction
        return d * ((x.high if d > 0 else x.low) - level)

    def near(x: Candle) -> float:  # how close bar x came back toward the level
        return d * ((x.low if d > 0 else x.high) - level)

    def info(state, quality, score, text, notes=()):
        return RetestInfo(state, d, level, touches, quality, score, age, distance, vol.state, vol.ratio, text, list(notes))

    # closed back through the level?
    back = [i for i, x in enumerate(after) if d * (x.close - level) < -0.05 * noise]
    thrust_idx = next((i for i, x in enumerate(after) if far(x) >= thrust), None)
    if back:
        i_back = back[-1]
        recent = i_back >= len(after) - 2 and d * (last.close - level) < -0.3 * noise
        fail_thrust = next((i for i, x in enumerate(after) if far(x) >= max(thrust, FAIL_THRUST_ATR * atr)), None)
        accepted = back[0] >= MIN_ACCEPT_BARS
        if recent and accepted and fail_thrust is not None and fail_thrust < back[0] and abs(last.close - level) <= NEAR_ATR * atr:
            notes: list[str] = []
            quality = 0.55
            body = abs(last.close - last.open)
            if (last.high - last.low) > 0 and body >= 0.5 * (last.high - last.low) and d * (last.close - last.open) < 0:
                quality += 0.1
                notes.append("свеча закрылась уверенно против пробоя")
            if vol.state == "low":
                quality *= 0.7
                notes.append("вялый рынок")
            quality = _clamp(quality, 0.0, 0.9)
            opposite = "вниз" if d > 0 else "вверх"
            return info("failed", quality, -d * quality,
                        f"Неудавшийся ретест: пробой уровня {_fmt(level)} {side} не удержался — цена вернулась "
                        f"за уровень, ловушка, возможно движение {opposite}{vnote}", notes)
        return None  # back-and-forth without a clean story, or a plain false breakout (handled elsewhere)

    if thrust_idx is None:
        if age <= MAX_WAIT_AGE and abs(last.close - level) <= NEAR_ATR * atr:
            return info("waiting", 0.0, 0.0,
                        f"Свежий пробой уровня {_fmt(level)} {side} — ждём отход и ретест, входить сейчас рано{vnote}")
        return None

    touch_ids = [i for i in range(thrust_idx + 1, len(after)) if near(after[i]) <= tol]
    if not touch_ids:
        if distance > MAX_EXTENDED_ATR:
            return None
        if distance >= EXTENDED_ATR:
            return info("extended", 0.0, 0.0,
                        f"Цена ушла на {distance:.1f} ATR от пробитого уровня {_fmt(level)} без ретеста — "
                        f"вход вдогонку рискован{vnote}")
        return info("waiting", 0.0, 0.0,
                    f"Уровень {_fmt(level)} пробит {side}, цена ушла — ждём возврат к уровню (ретест){vnote}")

    t_last = touch_ids[-1]
    touch_age = len(after) - 1 - t_last
    if touch_age > MAX_RETEST_AGE or t_last < 1:
        return None  # old news — or a wobble right at the break, not a retest
    reacted = d * (last.close - level) > 0 and d * (last.close - last.open) > 0
    if not reacted:
        return info("waiting", 0.0, 0.0,
                    f"Идёт ретест уровня {_fmt(level)} — реакции (закрытия в сторону пробоя) пока нет{vnote}")

    # confirmed retest — grade it
    notes = []
    quality = 0.6
    if touches >= 3:
        bonus = min(0.2, 0.1 * (touches - 2))
        quality += bonus
        notes.append(f"сильный уровень ({_touches_text(touches)})")
    rng = last.high - last.low
    if rng > 0 and abs(last.close - last.open) >= 0.5 * rng:
        quality += 0.1
        notes.append("уверенная свеча реакции")
    if any(d * (cs[b + 1 + i].low if d > 0 else cs[b + 1 + i].high) - d * level < 0 for i in touch_ids[-3:]):
        quality += 0.1
        notes.append("прокол уровня тенью и возврат")
    quality *= FRESHNESS[touch_age]
    quality *= _vol_multiplier(vol, last, atr, notes)
    quality *= _regime_multiplier(efficiency)
    quality = _clamp(quality, 0.0, 0.95)
    return info("confirmed", quality, d * quality,
                f"Пробой уровня {_fmt(level)} {side}, уход от него и ретест "
                f"{'сверху' if d > 0 else 'снизу'} — уровень удержан как {role}, продолжение {side}{vnote}", notes)


def analyze_retest(
    cs: list[Candle],
    levels: list[tuple[float, int]],
    atr: float,
    vol: VolatilityContext | None = None,
    efficiency: float = 0.2,
) -> RetestInfo | None:
    """The most meaningful retest story among `levels` (price, touches).
    `cs` must contain closed candles only. Returns None when no level has
    a recent break."""
    if len(cs) < 5 or atr <= 0:
        return None
    vol = vol or volatility_context(cs, atr)
    # Both directions are read for every level: a failed retest of an upward
    # break shows up as a fresh downward break, and the story that matters
    # (the trap) must not be hidden behind it.
    stories = [
        s
        for lv, t in levels
        for d in (1, -1)
        for s in (_scan_level(cs, lv, t, atr, vol, efficiency, d),)
        if s is not None
    ]
    if not stories:
        return None
    rank = {"confirmed": 3, "failed": 3, "waiting": 2, "extended": 1}
    return max(stories, key=lambda s: (rank[s.state], s.quality, -s.age))


def describe(info: RetestInfo) -> str:
    """One line for the Telegram message."""
    if info.state == "failed":
        line = f"⚠️ {info.text} (уверенность {info.quality:.0%})"
    else:
        head = {
            "confirmed": f"✅ Ретест подтверждён (качество {info.quality:.0%}): ",
            "waiting": "⏳ ",
            "extended": "⚠️ ",
        }[info.state]
        line = head + info.text
    if info.notes:
        line += " — " + "; ".join(info.notes)
    return line