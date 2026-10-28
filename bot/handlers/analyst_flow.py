"""📊 Получить сигнал — analyst-desk mode (🧠 Авто).

The user picked a pair and let the analyst choose the expiration. Pipeline:

  one instant snapshot of ALL timeframes (1M…1H) from the live feed
    → the model runs for every timeframe trio            (analysis.*)
    → consensus direction + expiration from the market state, with reasons
      and warnings                                       (analyst.advisor)
    → the signal is shown immediately
    → Claude, if configured, adds a senior-analyst note by editing the
      message                                            (analyst.claude_desk)

Manual mode (bot/handlers/signal.py) is a different, untouched path: the
user picks the expiration and gets the model's own verdict.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from analysis import AnalysisError
from analysis.calibration import calibrate
from analyst.advisor import ALL_TIMEFRAMES, DESK_VERSION, DeskDecision, analyze_trios, decide
from analyst.claude_desk import SupportsAnalyst, ask_analyst, render_note
from bot.keyboards import main_menu_keyboard, new_signal_keyboard
from bot.rendering import parse_selection
from bot.states import SignalSetup
from bot.utils import safe_edit_text
from market_data.models import MarketDataError
from market_data.provider import MarketDataProvider
from storage.models import SignalRecord
from storage.repository import SignalRepository

logger = logging.getLogger(__name__)

ANALYST_TIMEOUT_SECONDS = 12.0
_MAX_REASON_LINES = 5


def desk_lines(decision: DeskDecision) -> list[str]:
    lines = ["🧭 Решение аналитика:"]
    lines += [f" • {r}" for r in decision.reasons[:_MAX_REASON_LINES]]
    if decision.warnings:
        lines += ["", "⚠️ Внимание:"]
        lines += [f" • {w}" for w in decision.warnings]
    return lines


def _features(decision: DeskDecision) -> dict:
    return {
        "desk": {
            "consensus": decision.consensus,
            "trios": {t.key: {"direction": t.direction, "probability": t.result.probability,
                              "logit": t.result.logit} for t in decision.trios},
            "horizon": decision.horizon,
            "regime": decision.market.regime,
            "efficiency": decision.market.efficiency,
            "volatility": decision.market.volatility,
            "vol_ratio": decision.market.vol_ratio,
            "opposing_atr": decision.market.opposing_atr,
            "chosen_trio": decision.trio.key,
        },
        "logit": decision.logit,
    }


async def run_auto_signal(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    repo: SignalRepository | None,
    analyst: SupportsAnalyst | None,
) -> None:
    # Imported here: signal.py imports this module lazily, so a top-level
    # import the other way round would be a cycle.
    from bot.handlers.signal import _OUTPUT_SIZE, _fetch_sequentially, _format_result

    data = await state.get_data()
    pair, _ = parse_selection(data)
    if pair is None:
        await callback.answer("Сначала выберите валюту (💱)", show_alert=True)
        return

    await callback.answer("Аналитик изучает рынок…")
    started = time.perf_counter()

    live_price: float | None = None
    tick_age: float | None = None
    try:
        snapshot = getattr(provider, "get_live_snapshot", None)
        if snapshot is not None:
            candles, live_price, tick_age = await snapshot(pair, list(ALL_TIMEFRAMES), _OUTPUT_SIZE)
        else:
            candles = await _fetch_sequentially(provider, pair, ALL_TIMEFRAMES)
    except MarketDataError as exc:
        await safe_edit_text(callback.message, f"⚠️ Не удалось получить рыночные данные.\n{exc}", main_menu_keyboard())
        await state.set_state(SignalSetup.menu)
        return

    try:
        decision = decide(analyze_trios(pair, candles, live_price))
    except AnalysisError as exc:
        await safe_edit_text(
            callback.message,
            f"⚠️ Недостаточно истории для анализа: {exc}\nПопробуйте ещё раз через пару минут.",
            main_menu_keyboard(),
        )
        await state.set_state(SignalSetup.menu)
        return

    result = decision.as_result()
    outcomes = await repo.resolved_outcomes(DESK_VERSION) if repo is not None else []
    calibration = calibrate(result.probability, outcomes)
    elapsed = time.perf_counter() - started

    if repo is not None:
        primary = result.primary
        await repo.save_signal(
            SignalRecord(
                created_at=datetime.now(timezone.utc),
                pair=pair,
                timeframe=primary.timeframe,
                expiration=decision.expiration,
                rule_direction=primary.confirmation.direction,
                final_direction=decision.direction,
                confidence=decision.probability,
                claude_consulted=False,  # Claude only comments; it never takes part in the decision
                entry_price=primary.current_price,
                indicators=primary.indicators,
                reason=(f"Аналитик: консенсус {decision.consensus}/{len(decision.trios)} → "
                        f"{decision.direction}, экспирация {decision.expiration.label}, "
                        f"модель {decision.probability:.0f}%, показано {calibration.probability:.0f}%."),
                model_version=DESK_VERSION,
                features=_features(decision),
            )
        )

    gap_fn = getattr(provider, "history_gap_seconds", None)
    gap = gap_fn(pair) if gap_fn is not None else 0.0

    def render(analyst_lines: list[str] | None = None) -> str:
        return _format_result(
            result, calibration, tick_age, elapsed, candles, gap,
            expiration_note="подобрано аналитиком", desk_lines=desk_lines(decision), analyst_lines=analyst_lines,
        )

    # the signal goes out first — Claude never delays it
    await safe_edit_text(callback.message, render(), new_signal_keyboard())

    note = await ask_analyst(analyst, pair, decision, ANALYST_TIMEOUT_SECONDS)
    if note is not None:
        await safe_edit_text(callback.message, render(render_note(note)), new_signal_keyboard())
