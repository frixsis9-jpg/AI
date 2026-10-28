"""Handler for 📊 Получить сигнал.

Pipeline: one instant snapshot of three timeframes (chosen from the
expiration, see analysis.multi_timeframe.timeframe_trio_for) read from the
pair's always-on live feed (market_data/live_feed.py — no history download
on the button press, the feed was warmed up when the pair was picked) →
analyze_multi_timeframe() reads the last 120 closed candles per timeframe
(liquidity, retests, structure, imbalances, indicators; see
analysis.structure / analysis.confirmation) → a probability that the chosen
direction wins, recalibrated against the bot's own settled signals
(analysis.calibration).

By request the answer is ALWAYS CALL or PUT, never "skip": how strong the
read is shows up in the probability and in the listed pros/cons.

The bot never places any trade — this is signal generation only.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from analysis import (
    AnalysisError,
    MultiTimeframeResult,
    analyze_multi_timeframe,
    timeframe_trio_for,
)
from analysis.calibration import Calibration, calibrate
from analysis.confirmation import MODEL_VERSION
from analysis.engine import ANALYSIS_WINDOW
from analysis.retest import describe as describe_retest
from analysis.support_resistance import nearest_level
from bot.keyboards import main_menu_keyboard, new_signal_keyboard
from analyst.claude_desk import SupportsAnalyst
from bot.rendering import is_auto, parse_selection
from bot.states import SignalSetup
from bot.utils import safe_edit_text
from domain import Timeframe
from market_data.models import Candle, MarketDataError
from market_data.provider import MarketDataProvider
from storage.models import SignalRecord
from storage.repository import SignalRepository

logger = logging.getLogger(__name__)

router = Router(name="signal")

# 120 closed candles for the analysis + the forming one.
_OUTPUT_SIZE = ANALYSIS_WINDOW + 1

_DIRECTION_TEXT = {"CALL": "🟢 ВВЕРХ (CALL)", "PUT": "🔴 ВНИЗ (PUT)"}

_MAX_NOTES = 4


@router.callback_query(SignalSetup.menu, F.data == "menu:signal")
async def get_signal(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    repo: SignalRepository | None = None,
    analyst: SupportsAnalyst | None = None,
) -> None:
    data = await state.get_data()
    if is_auto(data):
        # Analyst-desk mode: a separate flow (consensus across all timeframe
        # trios + the desk's choice of expiration). Manual mode below is untouched.
        from bot.handlers.analyst_flow import run_auto_signal

        await run_auto_signal(callback, state, provider, repo, analyst)
        return
    pair, expiration = parse_selection(data)

    missing = [
        label
        for label, value in (("валюту (💱)", pair), ("экспирацию (⌛)", expiration))
        if value is None
    ]
    if missing:
        await callback.answer("Сначала выберите: " + ", ".join(missing), show_alert=True)
        return

    await callback.answer("Анализирую рынок…")
    started = time.perf_counter()

    trio = timeframe_trio_for(expiration)
    live_price: float | None = None
    tick_age: float | None = None
    try:
        snapshot = getattr(provider, "get_live_snapshot", None)
        if snapshot is not None:
            candles_by_timeframe, live_price, tick_age = await snapshot(pair, list(trio), _OUTPUT_SIZE)
        else:
            candles_by_timeframe = await _fetch_sequentially(provider, pair, trio)
    except MarketDataError as exc:
        await safe_edit_text(
            callback.message,
            f"⚠️ Не удалось получить рыночные данные.\n{exc}",
            main_menu_keyboard(),
        )
        await state.set_state(SignalSetup.menu)
        return

    try:
        result = analyze_multi_timeframe(pair, expiration, candles_by_timeframe, live_price)
    except AnalysisError as exc:
        await safe_edit_text(
            callback.message,
            f"⚠️ Недостаточно истории для анализа: {exc}\nПопробуйте ещё раз через пару минут.",
            main_menu_keyboard(),
        )
        await state.set_state(SignalSetup.menu)
        return

    outcomes = await repo.resolved_outcomes(MODEL_VERSION) if repo is not None else []
    calibration = calibrate(result.probability, outcomes)
    elapsed = time.perf_counter() - started

    if repo is not None:
        primary = result.primary
        record = SignalRecord(
            created_at=datetime.now(timezone.utc),
            pair=pair,
            timeframe=primary.timeframe,
            expiration=expiration,
            rule_direction=primary.confirmation.direction,
            final_direction=result.final_direction,
            confidence=result.probability,
            claude_consulted=False,
            entry_price=primary.current_price,
            indicators=primary.indicators,
            reason=_note(result, calibration),
            model_version=MODEL_VERSION,
            features=_features(result, calibration),
        )
        await repo.save_signal(record)

    gap_fn = getattr(provider, "history_gap_seconds", None)
    gap = gap_fn(pair) if gap_fn is not None else 0.0

    await safe_edit_text(
        callback.message,
        _format_result(result, calibration, tick_age, elapsed, candles_by_timeframe, gap),
        new_signal_keyboard(),
    )


async def _fetch_sequentially(provider, pair, trio) -> dict[Timeframe, list[Candle]]:
    """Fallback for providers without a live feed (e.g. TwelveData)."""
    out: dict[Timeframe, list[Candle]] = {}
    last_error: MarketDataError | None = None
    for timeframe in trio:
        try:
            out[timeframe] = await provider.get_candles(pair, timeframe, output_size=_OUTPUT_SIZE)
        except MarketDataError as exc:
            logger.warning("Candle fetch failed for %s %s: %s", pair, timeframe.value, exc)
            last_error = exc
    if not out:
        raise last_error or MarketDataError("no data")
    return out


def _features(result: MultiTimeframeResult, calibration: Calibration) -> dict:
    return {
        "logit": result.logit,
        "shown_probability": calibration.probability,
        "timeframes": {
            r.timeframe.value: {
                "logit": r.confirmation.logit,
                "regime": r.confirmation.regime,
                "votes": r.confirmation.votes,
            }
            for r in result.per_timeframe
        },
    }


def _note(result: MultiTimeframeResult, calibration: Calibration) -> str:
    tf_labels = ", ".join(r.timeframe.value for r in result.per_timeframe)
    return (
        f"Мульти-таймфрейм ({tf_labels}): {result.agreement}/{result.analyzed_count} "
        f"согласны на {result.final_direction}, модель {result.probability:.0f}%, "
        f"показано {calibration.probability:.0f}%."
    )


def _strength_word(p: float) -> str:
    if p >= 65:
        return "сильный сигнал"
    if p >= 60:
        return "хороший сигнал"
    if p >= 55:
        return "умеренный сигнал"
    return "слабый сигнал — факторы противоречат друг другу"


def _bar(p: float) -> str:
    filled = max(0, min(10, round((p - 50) / 3.2)))  # 50% → 0, 82% → 10
    return "█" * filled + "░" * (10 - filled)


def _regime_text(result: MultiTimeframeResult) -> str:
    regime = result.primary.confirmation.regime
    if regime >= 0.6:
        return "тренд"
    if regime <= 0.25:
        return "флэт / боковик"
    return "переходный (слабый тренд)"


_MAX_RETEST_LINES = 2
_RETEST_PRIORITY = {"confirmed": 0, "failed": 0, "waiting": 1, "extended": 2}


def _retest_lines(result: MultiTimeframeResult) -> list[str]:
    """What the analysis understood about retests of broken levels, per
    timeframe. Shown regardless of how much the factor weighs in the
    verdict — it is context for the reader (analysis.retest)."""
    stories = [(r.timeframe, r.structure.retest) for r in result.per_timeframe
               if getattr(r.structure, "retest", None) is not None]
    stories.sort(key=lambda s: _RETEST_PRIORITY[s[1].state])
    decisive = [s for s in stories if s[1].state in ("confirmed", "failed")]
    # "waiting" / "extended" are only context: one line, and only when there
    # is no actual retest result to report
    shown = decisive[:_MAX_RETEST_LINES] if decisive else stories[:1]
    return [f"🔁 [{tf.value}] {describe_retest(info)}" for tf, info in shown]


def _format_result(
    result: MultiTimeframeResult,
    calibration: Calibration,
    tick_age: float | None,
    elapsed: float,
    candles_by_timeframe: dict[Timeframe, list[Candle]],
    history_gap: float = 0.0,
    *,
    expiration_note: str | None = None,
    desk_lines: list[str] | None = None,
    analyst_lines: list[str] | None = None,
) -> str:
    """`expiration_note`, `desk_lines` and `analyst_lines` are used only by the
    analyst-desk mode (bot/handlers/analyst_flow.py); the manual mode never
    passes them and its output is unchanged."""
    p = calibration.probability
    trio_text = " → ".join(r.timeframe.value for r in result.per_timeframe)
    counts = ", ".join(f"{r.timeframe.value}: {r.closed_candles}" for r in result.per_timeframe)

    lines = [
        f"💱 {result.pair.label}   ⌛ {result.expiration.label}" + (f" ({expiration_note})" if expiration_note else ""),
        "",
        f"СИГНАЛ: {_DIRECTION_TEXT[result.final_direction]}",
        f"🎯 Вероятность успеха: {p:.0f}%  {_bar(p)}",
        f"({_strength_word(p)})",
        "",
        (f"Таймфреймы экспирации: {trio_text} — согласны {result.agreement} из {result.analyzed_count}"
         if desk_lines else
         f"Таймфреймы: {trio_text} — согласны {result.agreement} из {result.analyzed_count}"),
    ]
    if desk_lines:
        # the desk states the market regime itself (from one reference
        # timeframe) — a second, differently measured "Режим рынка" line would
        # contradict it
        lines += [""] + desk_lines
    else:
        lines.append(f"Режим рынка: {_regime_text(result)}")

    if result.pros:
        lines += ["", f"✅ За {result.final_direction}:"]
        lines += [f" • [{n.timeframe.value}] {n.text}" for n in result.pros[:_MAX_NOTES]]
    if result.cons:
        lines += ["", "⚠️ Против:"]
        lines += [f" • [{n.timeframe.value}] {n.text}" for n in result.cons[:_MAX_NOTES]]

    primary = result.primary
    lines += ["", f"Цена входа: {primary.current_price:.5f}"]
    support = nearest_level(primary.levels, primary.current_price, "support")
    resistance = nearest_level(primary.levels, primary.current_price, "resistance")
    level_parts = []
    if support is not None and support.price < primary.current_price:
        level_parts.append(f"поддержка {support.price:.5f}")
    if resistance is not None and resistance.price > primary.current_price:
        level_parts.append(f"сопротивление {resistance.price:.5f}")
    if level_parts:
        lines.append("Уровни: " + "; ".join(level_parts))
    lines += _retest_lines(result)

    freshness =f"последний тик {tick_age:.1f} с назад, " if tick_age is not None else ""
    lines.append(f"Данные: {freshness}свечей {counts}; анализ {elapsed * 1000:.0f} мс")
    if history_gap > 0:
        lines.append(
            f"⏳ Pocket Option ещё не отдал последние {history_gap / 60:.0f} мин. истории "
            "(сервер публикует её с задержкой 2–4 мин) — для 1-минутного графика "
            "сигнал точнее через пару минут после выбора пары."
        )

    bucket = calibration.bucket
    if bucket is not None and bucket.total:
        lines.append(
            f"📈 Реальный винрейт сигналов с такой оценкой: {bucket.winrate:.0f}% "
            f"(из {bucket.total}); оценка модели {calibration.model_probability:.0f}%"
        )
    else:
        lines.append(
            "📈 Процент — оценка модели; по мере накопления результатов (/stats) "
            "он подстраивается под реальный винрейт."
        )

    if analyst_lines:
        lines += [""] + analyst_lines
    lines += ["", "⚪ Не финансовая рекомендация и не гарантия результата."]
    return "\n".join(lines)
