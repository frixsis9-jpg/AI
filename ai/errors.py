"""Errors raised by the ai/ package."""


class ClaudeError(Exception):
    """Anything that went wrong asking Claude for a second opinion.

    Covers network/API failures *and* a response that doesn't parse into a
    valid verdict — either way, the caller (ai.review.review_signal) treats
    this as "could not get a second opinion" and fails safe to WAIT rather
    than trusting the rule-only signal or guessing at what Claude meant.
    """
