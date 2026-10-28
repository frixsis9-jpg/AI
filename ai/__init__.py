"""Claude as a *second opinion* on top of `analysis.confirmation`.

This package never replaces the rule-based verdict and never invents one on
its own. It is only consulted when `analysis.confirmation.evaluate()` already
produced a CALL/PUT, and it may only confirm that verdict or downgrade it to
WAIT — it can never turn a WAIT into a trade signal, and it can never flip
CALL to PUT or vice versa. See `ai/review.py` for the exact rules.
"""
from ai.claude_client import ClaudeReviewClient, ClaudeVerdict, SupportsClaudeVerdict
from ai.errors import ClaudeError
from ai.review import SignalDecision, review_signal

__all__ = [
    "ClaudeError",
    "ClaudeReviewClient",
    "ClaudeVerdict",
    "SignalDecision",
    "SupportsClaudeVerdict",
    "review_signal",
]
