"""Claude as the desk's senior analyst — a second pair of eyes, not a second
decision-maker.

Who decides what, so nothing gets muddled:

* direction and expiration      → analyst.advisor (deterministic, instant,
                                  reproducible, covered by tests);
* explanation, challenge, risks → this module (Claude).

Claude receives the finished decision with its evidence and answers in
plain Russian: does the picture hang together, what is the main risk, and
does it agree. It CANNOT change the direction or the expiration — if it
disagrees it says so (stance "caution") and the user sees both. Why: there
is no evidence that a language model reading indicator summaries predicts
price better than the model that produced them, and a silent override
would make results impossible to audit.

The call is made AFTER the signal has already been shown (the message is
edited to add the note), so Claude never delays the signal; any failure —
no key, timeout, malformed answer — simply leaves the message as it was.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from ai.errors import ClaudeError
from analyst.advisor import DeskDecision, side_word
from analysis.retest import describe as describe_retest
from domain import CurrencyPair

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 500

SYSTEM_PROMPT = """\
Ты — старший трейдер-аналитик с 15-летним стажем на валютном рынке. Тебе приходит \
готовое решение количественной системы по бинарному опциону (направление и время \
экспирации) вместе с данными, на которых оно основано. Твоя задача — как опытный \
человек коротко и честно разобрать картину для трейдера.

Правила:
1. Направление и экспирацию выбрала система. Ты их НЕ меняешь и не предлагаешь другие \
   цифры. Если не согласен — скажи это словами (stance = "caution").
2. Опирайся ТОЛЬКО на данные из сообщения. Не выдумывай уровни, новости, события, \
   числа, которых там нет.
3. Вероятность модели обычно 50–58%: это почти монетка. Если перевес слабый — прямо \
   скажи, что уверенности нет. Не рисуй уверенность, не обещай результат, не используй \
   слова «гарантия», «точно», «наверняка».
4. Обрати внимание на то, что опытный человек заметил бы: расхождение таймфреймов, \
   близкий уровень на пути сделки, всплеск волатильности, ловушку у пробоя, \
   слишком длинную или короткую экспирацию для такого рынка.
5. Пиши по-русски, спокойно, без маркдауна и эмодзи.

Ответ — строго один JSON-объект, без текста вокруг:
{"stance": "agree" | "caution", "summary": "2-4 предложения, до 380 символов", \
"risks": ["до 3 коротких пунктов, каждый до 120 символов"]}
"""

_MAX_SUMMARY = 420
_MAX_RISK = 140
_MAX_RISKS = 3


@dataclass(frozen=True)
class AnalystNote:
    stance: str  # "agree" | "caution"
    summary: str
    risks: list[str] = field(default_factory=list)


class SupportsAnalyst(Protocol):
    """What the handler depends on; tests inject a fake."""

    async def write_note(self, system_prompt: str, user_prompt: str) -> str: ...


class ClaudeAnalyst:
    """Real implementation on the `anthropic` SDK."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, max_tokens: int = MAX_TOKENS) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def write_note(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:  # the SDK's hierarchy isn't our concern
            raise ClaudeError(f"Claude API call failed: {exc}") from exc
        content = getattr(response, "content", None)
        text = getattr(content[0], "text", None) if content else None
        if not text:
            raise ClaudeError("Claude response had no text")
        return text

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def looks_like_api_key(key: str | None) -> bool:
    """Real Anthropic keys start with 'sk-ant-' and are long. A placeholder
    or a key from another service would fail on every signal — better to
    leave the analyst off and say so once at startup."""
    return bool(key) and key.strip().startswith("sk-ant-") and len(key.strip()) > 40


# --- the case file ---------------------------------------------------------------

def build_case(pair: CurrencyPair, decision: DeskDecision) -> str:
    """The decision and its evidence as plain text. Only numbers the system
    itself computed — nothing here comes from outside."""
    d = decision
    r = d.trio.result
    m = d.market
    lines = [
        f"Пара: {pair.label}" + (" (OTC — котировки генерирует брокер)" if pair.is_otc else ""),
        f"Решение системы: {side_word(d.direction)}, вероятность {d.probability:.1f}% "
        f"({'близко к монетке' if d.probability < 53 else 'слабый перевес' if d.probability < 58 else 'заметный перевес'})",
        f"Выбранная экспирация: {d.expiration.label}",
        f"Согласие троек таймфреймов: {d.consensus} из {len(d.trios)}",
        f"Рынок: режим {m.regime} (эффективность {m.efficiency:.2f}), волатильность {m.volatility} "
        f"({m.vol_ratio:.1f}× нормы), "
        + (f"ближайший уровень на пути сделки в {m.opposing_atr:.1f} ATR" if m.opposing_atr is not None
           else "уровней на пути сделки нет"),
    ]
    if r.pros:
        lines.append("Доводы за: " + "; ".join(f"[{n.timeframe.value}] {n.text}" for n in r.pros[:4]))
    if r.cons:
        lines.append("Доводы против: " + "; ".join(f"[{n.timeframe.value}] {n.text}" for n in r.cons[:4]))
    for per in r.per_timeframe:
        if per.structure.retest is not None:
            lines.append(f"Ретест [{per.timeframe.value}]: {describe_retest(per.structure.retest)}")
            break
    lines.append("Почему такая экспирация: " + "; ".join(d.reasons[1:]))
    if d.warnings:
        lines.append("Предупреждения системы: " + " ".join(d.warnings))
    return "\n".join(lines)


def parse_note(text: str) -> AnalystNote:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"analyst answer was not valid JSON: {text[:80]!r}") from exc
    if not isinstance(data, dict):
        raise ClaudeError("analyst answer was not a JSON object")
    stance = data.get("stance")
    if stance not in ("agree", "caution"):
        raise ClaudeError(f"invalid stance: {stance!r}")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ClaudeError("empty summary")
    risks_raw = data.get("risks", [])
    risks = [str(x).strip()[:_MAX_RISK] for x in risks_raw if isinstance(x, str) and x.strip()][:_MAX_RISKS] \
        if isinstance(risks_raw, list) else []
    return AnalystNote(stance, summary.strip()[:_MAX_SUMMARY], risks)


async def ask_analyst(
    client: SupportsAnalyst | None, pair: CurrencyPair, decision: DeskDecision, timeout: float = 12.0
) -> AnalystNote | None:
    """The analyst's note, or None. Never raises: the signal is already out,
    and a missing note must not break anything."""
    if client is None:
        return None
    try:
        raw = await asyncio.wait_for(client.write_note(SYSTEM_PROMPT, build_case(pair, decision)), timeout)
        return parse_note(raw)
    except asyncio.TimeoutError:
        logger.warning("Analyst note timed out after %.0f s", timeout)
    except ClaudeError as exc:
        logger.warning("Analyst note unavailable: %s", exc)
    except Exception:  # noqa: BLE001 - must never propagate into the handler
        logger.exception("Unexpected error while asking the analyst")
    return None


def render_note(note: AnalystNote) -> list[str]:
    icon = "🟢" if note.stance == "agree" else "🟡"
    lines = [f"🧠 Аналитик {icon}: {note.summary}"]
    lines += [f" • {r}" for r in note.risks]
    return lines
