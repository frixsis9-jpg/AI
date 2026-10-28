"""Thin async wrapper around the Anthropic SDK for the second-opinion review.

Kept deliberately small: one method, one job (turn a prompt into a strictly
validated ClaudeVerdict or raise ClaudeError). All the "what may Claude
actually change about the signal" logic lives in ai/review.py, not here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from ai.errors import ClaudeError

Direction = Literal["CALL", "PUT", "WAIT"]

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 400

_VALID_DIRECTIONS = {"CALL", "PUT", "WAIT"}


@dataclass(frozen=True)
class ClaudeVerdict:
    """Claude's own read of the same evidence — see ai/prompt.py for what it
    is and isn't allowed to do with it.
    """

    direction: Direction
    confidence: float  # 0-100, Claude's own stated confidence
    reason: str


class SupportsClaudeVerdict(Protocol):
    """The interface ai.review.review_signal actually depends on.

    Exists so tests (and any future alternative reviewer) can inject a fake
    without touching the real Anthropic SDK or needing a network/API key.
    """

    async def get_verdict(self, system_prompt: str, user_prompt: str) -> ClaudeVerdict: ...


class ClaudeReviewClient:
    """Real implementation, backed by the `anthropic` Python SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        # Imported lazily so importing this module doesn't require the
        # `anthropic` package unless Claude review is actually configured.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def get_verdict(self, system_prompt: str, user_prompt: str) -> ClaudeVerdict:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:  # SDK's own hierarchy isn't our concern here
            raise ClaudeError(f"Claude API call failed: {exc}") from exc

        text = _extract_text(response)
        return _parse_verdict(text)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _extract_text(response: object) -> str:
    content = getattr(response, "content", None)
    if not content:
        raise ClaudeError("Claude response had no content blocks")
    first = content[0]
    text = getattr(first, "text", None)
    if not text:
        raise ClaudeError("Claude response's first content block had no text")
    return text


def _parse_verdict(text: str) -> ClaudeVerdict:
    text = text.strip()
    # Claude sometimes wraps JSON in a code fence despite instructions not to
    # — strip a leading/trailing ``` fence if present rather than failing on it.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"Claude response was not valid JSON: {text!r}") from exc

    if not isinstance(data, dict):
        raise ClaudeError(f"Claude response JSON was not an object: {text!r}")

    direction = data.get("direction")
    if direction not in _VALID_DIRECTIONS:
        raise ClaudeError(f"Claude response had an invalid direction: {direction!r}")

    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ClaudeError(f"Claude response had an invalid confidence: {confidence!r}")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 100.0):
        raise ClaudeError(f"Claude response confidence out of range: {confidence!r}")

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ClaudeError("Claude response had an empty or missing reason")

    return ClaudeVerdict(direction=direction, confidence=confidence, reason=reason.strip())
