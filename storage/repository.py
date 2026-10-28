"""Async SQLite repository for signals — the only place that touches SQL.

Uses `aiosqlite` (not the stdlib `sqlite3` directly) because aiogram handlers
are async and a blocking sqlite3 call would stall the whole bot's event loop
for every user while it runs.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import aiosqlite

from analysis.confirmation import Direction
from domain import CurrencyPair, Expiration, Timeframe
from storage.db import MIGRATION_COLUMNS, SCHEMA_SQL
from storage.models import PendingSignal, SignalRecord, StatsRow


class SignalRepository:
    """One repository per bot process, backed by one shared aiosqlite connection.

    `init()` must be awaited once before any other method is used (creates
    the file/schema if needed); `aclose()` should be called on shutdown.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        cursor = await conn.execute("PRAGMA table_info(signals)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column, sql_type in MIGRATION_COLUMNS.items():
            if column not in existing:
                await conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {sql_type}")
        await conn.commit()
        self._conn = conn

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _conn_or_raise(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SignalRepository.init() must be awaited before use")
        return self._conn

    async def save_signal(self, record: SignalRecord) -> int:
        conn = self._conn_or_raise()
        # WAIT signals never had a trade placed against them, so there is
        # nothing for the resolver to determine later — mark them settled
        # immediately rather than leaving them "pending" forever.
        is_wait = record.final_direction == "WAIT"
        outcome = "N/A" if is_wait else None
        resolved_at = record.created_at.isoformat() if is_wait else None

        cursor = await conn.execute(
            """
            INSERT INTO signals (
                created_at, pair, timeframe, expiration_seconds,
                rule_direction, final_direction, confidence, claude_consulted,
                entry_price, indicators_json, reason, expires_at,
                outcome, exit_price, resolved_at, model_version, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.created_at.isoformat(),
                str(record.pair),
                record.timeframe.value,
                int(record.expiration.value),
                record.rule_direction,
                record.final_direction,
                record.confidence,
                int(record.claude_consulted),
                record.entry_price,
                json.dumps(asdict(record.indicators)),
                record.reason,
                record.expires_at.isoformat(),
                outcome,
                None,
                resolved_at,
                record.model_version,
                json.dumps(record.features) if record.features is not None else None,
            ),
        )
        await conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_pending_signals(self, now: datetime | None = None) -> list[PendingSignal]:
        """CALL/PUT signals whose expiration has passed but have no outcome yet."""
        conn = self._conn_or_raise()
        now = now or datetime.now(timezone.utc)
        cursor = await conn.execute(
            """
            SELECT id, pair, timeframe, expiration_seconds, final_direction,
                   entry_price, expires_at
            FROM signals
            WHERE outcome IS NULL AND expires_at <= ?
            ORDER BY expires_at
            """,
            (now.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [_row_to_pending(row) for row in rows]

    async def mark_resolved(
        self,
        signal_id: int,
        outcome: str,
        exit_price: float | None,
        resolved_at: datetime | None = None,
    ) -> None:
        conn = self._conn_or_raise()
        resolved_at = resolved_at or datetime.now(timezone.utc)
        await conn.execute(
            "UPDATE signals SET outcome = ?, exit_price = ?, resolved_at = ? WHERE id = ?",
            (outcome, exit_price, resolved_at.isoformat(), signal_id),
        )
        await conn.commit()

    async def get_stats(self) -> list[StatsRow]:
        """Raw WIN/LOSS/UNKNOWN/pending counts per (pair, timeframe, expiration).

        Only covers CALL/PUT signals — WAIT is a "no trade" outcome and isn't
        part of an accuracy breakdown.
        """
        conn = self._conn_or_raise()
        cursor = await conn.execute(
            """
            SELECT
                pair, timeframe, expiration_seconds,
                SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN outcome = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown,
                SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS pending
            FROM signals
            WHERE final_direction != 'WAIT'
            GROUP BY pair, timeframe, expiration_seconds
            ORDER BY pair, timeframe, expiration_seconds
            """
        )
        rows = await cursor.fetchall()
        return [
            StatsRow(
                pair=CurrencyPair(row["pair"]),
                timeframe=Timeframe(row["timeframe"]),
                expiration=Expiration(row["expiration_seconds"]),
                wins=row["wins"],
                losses=row["losses"],
                unknown=row["unknown"],
                pending=row["pending"],
            )
            for row in rows
        ]


    async def resolved_outcomes(self, model_version: str) -> list[tuple[float, bool, int]]:
        """(model probability %, won?, expiration seconds) for every
        WIN/LOSS signal produced by `model_version` — the raw material for
        calibrating the shown probability against reality."""
        conn = self._conn_or_raise()
        cursor = await conn.execute(
            """
            SELECT confidence, outcome, expiration_seconds FROM signals
            WHERE model_version = ? AND outcome IN ('WIN', 'LOSS') AND confidence IS NOT NULL
            """,
            (model_version,),
        )
        return [
            (float(row["confidence"]), row["outcome"] == "WIN", int(row["expiration_seconds"]))
            for row in await cursor.fetchall()
        ]


def _row_to_pending(row: aiosqlite.Row) -> PendingSignal:
    direction: Direction = row["final_direction"]
    return PendingSignal(
        id=row["id"],
        pair=CurrencyPair(row["pair"]),
        timeframe=Timeframe(row["timeframe"]),
        expiration=Expiration(row["expiration_seconds"]),
        final_direction=direction,
        entry_price=row["entry_price"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )
