"""Orchestrates the Claude second-opinion review on top of the multi-timeframe
combiner (`analysis.multi_timeframe`).

This is the ONLY place that decides what the bot's final CALL/PUT/WAIT is.
The rules are intentionally hardcoded (not configurable), because they encode
a safety property the rest of the system depends on:

* The combiner's WAIT is final — Claude is never even consulted, let alone
  allowed to override it. This already covers "the timeframes disagree with
  each other", not just "no single timeframe found enough confirmations".
* The combiner's CALL/PUT is a *candidate*, not a guarantee — Claude may
  confirm it or downgrade it to WAIT, but there is no code path that lets
  Claude flip CALL<->PUT or invent a signal from WAIT.
* Any failure to get a usable opinion from Claude (no client configured, API
  error, unparseable response) fails safe to WAIT rather than falling back to
  trusting the rule-only signal blindly.
"""
from __future__ import annotations

from dataclasses import dataclass

from ai.claude_client import SupportsClaudeVerdict
from ai.errors import ClaudeError
from ai.prompt import build_prompt
from analysis.confirmation import Direction
from analysis.multi_timeframe import MultiTimeframeResult

# Claude must be at least this confident in the SAME direction the rule
# engine proposed for the signal to survive. Short expirations demand a much
# higher bar, matching the "even stricter for 5-30s" requirement — this is on
# top of analysis.confirmation's own already-stricter SHORT_THRESHOLDS, not a
# replacement for them.
NORMAL_MIN_CONFIDENCE = 60.0
SHORT_MIN_CONFIDENCE = 85.0


@dataclass(frozen=True)
class SignalDecision:
    """The outcome of running a rule-based verdict through the Claude review."""

    final_direction: Direction
    rule_direction: Direction
    claude_consulted: bool
    claude_direction: str | None
    claude_confidence: float | None
    claude_reason: str | None
    downgraded: bool  # True only when the rule verdict was CALL/PUT and got reduced to WAIT
    note: str  # short, human-readable summary for the Telegram message


def _min_confidence(result: MultiTimeframeResult) -> float:
    return SHORT_MIN_CONFIDENCE if result.expiration.is_short else NORMAL_MIN_CONFIDENCE


async def review_signal(
    result: MultiTimeframeResult,
    claude: SupportsClaudeVerdict | None,
) -> SignalDecision:
    rule_direction = result.final_direction

    if rule_direction == "WAIT":
        return SignalDecision(
            final_direction="WAIT",
            rule_direction="WAIT",
            claude_consulted=False,
            claude_direction=None,
            claude_confidence=None,
            claude_reason=None,
            downgraded=False,
            note="Правило-based вердикт уже WAIT — Claude не привлекается.",
        )

    if claude is None:
        return SignalDecision(
            final_direction=rule_direction,
            rule_direction=rule_direction,
            claude_consulted=False,
            claude_direction=None,
            claude_confidence=None,
            claude_reason=None,
            downgraded=False,
            note="Claude не настроен (нет ANTHROPIC_API_KEY) — сигнал без проверки ИИ.",
        )

    system_prompt, user_prompt = build_prompt(result)
    try:
        verdict = await claude.get_verdict(system_prompt, user_prompt)
    except ClaudeError as exc:
        return SignalDecision(
            final_direction="WAIT",
            rule_direction=rule_direction,
            claude_consulted=True,
            claude_direction=None,
            claude_confidence=None,
            claude_reason=None,
            downgraded=True,
            note=f"Claude недоступен ({exc}) — сигнал переведён в WAIT из соображений безопасности.",
        )

    threshold = _min_confidence(result)

    if verdict.direction == rule_direction and verdict.confidence >= threshold:
        return SignalDecision(
            final_direction=rule_direction,
            rule_direction=rule_direction,
            claude_consulted=True,
            claude_direction=verdict.direction,
            claude_confidence=verdict.confidence,
            claude_reason=verdict.reason,
            downgraded=False,
            note=f"Claude подтвердил сигнал (уверенность {verdict.confidence:.0f}%).",
        )

    if verdict.direction != rule_direction:
        reason = (
            f"Claude не согласился с направлением (дал {verdict.direction}) — сигнал понижен до WAIT."
        )
    else:
        reason = (
            f"Уверенность Claude ({verdict.confidence:.0f}%) ниже требуемого порога "
            f"({threshold:.0f}%) — сигнал понижен до WAIT."
        )

    return SignalDecision(
        final_direction="WAIT",
        rule_direction=rule_direction,
        claude_consulted=True,
        claude_direction=verdict.direction,
        claude_confidence=verdict.confidence,
        claude_reason=verdict.reason,
        downgraded=True,
        note=reason,
    )
