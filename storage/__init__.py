"""SQLite-backed persistence for generated signals and their outcomes.

Every signal shown to a user (CALL, PUT, or WAIT) is saved via
`SignalRepository.save_signal`. For CALL/PUT signals, `storage.resolver`
later determines whether the trade would have won or lost, using real Pocket
Option prices fetched after the expiration has passed — never guessed or
simulated. `SignalRepository.get_stats` aggregates raw WIN/LOSS/UNKNOWN
counts per (pair, timeframe, expiration); it deliberately does not present
these as a promise of accuracy or profitability, per the project's own rule
about not overselling an unproven heuristic.
"""
from storage.models import PendingSignal, SignalRecord, StatsRow
from storage.repository import SignalRepository
from storage.resolver import resolve_pending_signals, run_resolver_loop

__all__ = [
    "PendingSignal",
    "SignalRecord",
    "SignalRepository",
    "StatsRow",
    "resolve_pending_signals",
    "run_resolver_loop",
]
