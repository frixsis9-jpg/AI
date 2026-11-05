import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.storage.base import StorageKey

from domain import CurrencyPair, Expiration
from market_data.live_feed import AssetFeed
from market_data.pocket_option import PocketOptionProvider
from storage.fsm import SQLiteStorage
from storage.models import SignalRecord
from storage.repository import SignalRepository
from storage.resolver import resolve_pending_signals


@pytest.mark.asyncio
async def test_menu_settings_survive_restart_and_concurrent_updates(tmp_path):
    path = str(tmp_path / "signals.db")
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    s = SQLiteStorage(path)
    await s.init()
    await s.set_state(key, "SignalSetup:menu")
    await asyncio.gather(
        s.update_data(key, {"expiration": "SEC_10"}),
        s.update_data(key, {"pair_mode": "auto"}),
    )
    await s.close()
    s = SQLiteStorage(path)
    await s.init()
    try:
        assert await s.get_data(key) == {"expiration": "SEC_10", "pair_mode": "auto"}
        assert await s.get_state(key) == "SignalSetup:menu"
        assert await s.get_data(StorageKey(bot_id=1, chat_id=2, user_id=4)) == {}
    finally:
        await s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["entry-v1", "manual-v1"])
async def test_new_stats_separate_versions_and_never_fall_back_to_current_price(
    tmp_path, candidate_factory, version
):
    repo = SignalRepository(str(tmp_path / "signals.db"))
    await repo.init()
    c = candidate_factory()
    now = datetime.now(timezone.utc)
    record = SignalRecord(
        now - timedelta(seconds=6),
        c.snapshot.pair,
        c.result.primary.timeframe,
        Expiration.SEC_5,
        "CALL",
        "CALL",
        None,
        False,
        1.1,
        c.result.primary.indicators,
        "test",
        version,
    )
    await repo.save_signal(record)
    provider = Mock()
    provider.feed_has_passed.return_value = True
    provider.price_at = AsyncMock(return_value=None)
    provider.get_candles = AsyncMock()
    await resolve_pending_signals(repo, provider, now)
    provider.get_candles.assert_not_awaited()
    rows = await repo.get_stats()
    assert rows[0].unknown == 1 and rows[0].model_version == version
    await repo.save_signal(replace(record, model_version="v3-fitted", confidence=60))
    assert len(await repo.get_stats()) == 2
    await repo.aclose()


@pytest.mark.asyncio
async def test_pinned_feeds_are_not_evicted_and_cancel_releases_only_own_lease():
    p = PocketOptionProvider("dummy", client=Mock(), max_feeds=1)
    f = AssetFeed("EURUSD")
    f.on_tick(__import__("time").time(), 1.1)
    f.stop = AsyncMock()
    p._feeds["EURUSD"] = f
    a = p.pin(CurrencyPair("EURUSD"), __import__("time").time() + 30)
    b = p.pin(CurrencyPair("EURUSD"), __import__("time").time() + 60)
    p.release(a)
    with pytest.raises(Exception, match="заняты"):
        await p._reap_idle_feeds(None)
    f.stop.assert_not_awaited()
    p.release(b)
    await p._reap_idle_feeds(None)
    f.stop.assert_awaited_once()
    assert not p._feeds


@pytest.mark.asyncio
async def test_cancel_during_history_start_explicitly_unsubscribes():
    import time

    class Stream:
        def __init__(self):
            self.unsubscribe = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.01)
            return {"timestamp": time.time(), "price": 1.1}

    client = Mock()
    stream = Stream()
    client.subscribe_symbol = AsyncMock(return_value=stream)
    client.wait_for_assets = AsyncMock()
    client.active_assets = AsyncMock(
        return_value=[{"symbol": "EURUSD", "is_active": True}]
    )
    started = asyncio.Event()

    async def hold(*args):
        started.set()
        await asyncio.Event().wait()

    client.get_candles_advanced = hold
    p = PocketOptionProvider("dummy", client=client)
    task = asyncio.create_task(p._ensure_feed(CurrencyPair("EURUSD")))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream.unsubscribe.assert_awaited_once()
    assert not p._feeds
