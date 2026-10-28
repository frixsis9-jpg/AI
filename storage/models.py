"""Dataclasses used by the storage layer.

Kept independent of the sqlite/aiosqlite specifics in repository.py — these
are what the rest of the app (bot handlers, resolver) actually talk in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from analysis.confirmation import Direction
from analysis.indicators import IndicatorSnapshot
from domain import CurrencyPair, Expiration, Timeframe


@dataclass(frozen=True)
class SignalRecord:
    """Everything about one generated signal, ready to hand to
    SignalRepository.save_signal.
    """

    created_at: datetime
    pair: CurrencyPair
    timeframe: Timeframe
    expiration: Expiration
    rule_direction: Direction
    final_direction: Direction
    confidence: float | None
    claude_consulted: bool
    entry_price: float
    indicators: IndicatorSnapshot
    reason: str
    model_version: str | None = None
    features: dict | None = None

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=int(self.expiration.value))


@dataclass(frozen=True)
class PendingSignal:
    """A saved CALL/PUT signal whose expiration has passed but whose outcome
    hasn't been determined yet — what `storage.resolver` works through.
    """

    id: int
    pair: CurrencyPair
    timeframe: Timeframe
    expiration: Expiration
    final_direction: Direction
    entry_price: float
    expires_at: datetime


@dataclass(frozen=True)
class StatsRow:
    """Raw outcome counts for one (pair, timeframe, expiration) combination.

    No accuracy/win-rate percentage is computed here on purpose — the project
    explicitly does not promise predictive accuracy, only collects real
    outcomes over time (see README).
    """

    pair: CurrencyPair
    timeframe: Timeframe
    expiration: Expiration
    wins: int
    losses: int
    unknown: int
    pending: int

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.unknown + self.pending

    @property
    def winrate(self) -> float | None:
        decided = self.wins + self.losses
        return 100.0 * self.wins / decided if decided else None

    @property
    def label(self) -> str:
        return f"{self.pair.label} + {self.timeframe.value} + {self.expiration.label}"
