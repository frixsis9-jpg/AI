"""Persist menu choices across Docker restarts, never persist live tasks."""

import asyncio
import json
from dataclasses import asdict

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str):
        self.path = path
        self.conn = None
        self.lock = asyncio.Lock()

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS menu_state (key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}')"
        )
        await self.conn.commit()

    @staticmethod
    def key(key: StorageKey):
        return json.dumps(asdict(key), sort_keys=True)

    async def set_state(self, key, state=None):
        if hasattr(state, "state"):
            state = state.state
        async with self.lock:
            await self.conn.execute(
                "INSERT INTO menu_state(key,state) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                (self.key(key), state),
            )
            await self.conn.commit()

    async def get_state(self, key):
        async with self.lock:
            cursor = await self.conn.execute(
                "SELECT state FROM menu_state WHERE key=?", (self.key(key),)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def _get_data(self, key):
        cursor = await self.conn.execute(
            "SELECT data FROM menu_state WHERE key=?", (self.key(key),)
        )
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else {}

    async def get_data(self, key):
        async with self.lock:
            return await self._get_data(key)

    async def _set_data(self, key, data):
        await self.conn.execute(
            "INSERT INTO menu_state(key,data) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (self.key(key), json.dumps(data)),
        )
        await self.conn.commit()

    async def set_data(self, key, data):
        async with self.lock:
            await self._set_data(key, data)

    async def update_data(self, key, data):
        async with self.lock:
            current = await self._get_data(key)
            current.update(data)
            await self._set_data(key, current)
            return current.copy()

    async def close(self):
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
