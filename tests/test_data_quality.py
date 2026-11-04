import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from domain import Timeframe
from market_data.live_feed import (
    AssetFeed,
    CandleSeries,
    aggregate_by_time,
    fetch_history_page,
    parse_tick,
)
from market_data.models import Candle, MarketDataError


def test_utc_seconds_and_milliseconds_are_not_shifted():
    assert parse_tick({"timestamp": 1800000000, "price": 1.2}) == (1800000000, 1.2)
    assert parse_tick(["EURUSD", 1800000000000, 1.2]) == (1800000000, 1.2)


def test_old_or_future_ticks_cannot_appear_fresh(snapshot_factory):
    s = snapshot_factory()
    with pytest.raises(MarketDataError, match="устарела"):
        replace(s, tick_time=s.captured_at - 60).validate(s.candles, max_age=2)
    with pytest.raises(MarketDataError, match="будущем"):
        replace(s, tick_time=s.captured_at + 2).validate(s.candles, max_age=2)
    f = AssetFeed("EURUSD")
    f.on_tick(time.time() - 60, 1.1)
    assert f.is_stale
    old = f.feed_now
    f.on_tick(old - 1, 1.2)
    assert f.last_price == 1.1
    f.on_tick(time.time() + 100, 1.3)
    assert f.last_price == 1.1


@pytest.mark.parametrize("now", [None, 1_800_000_000.05])
def test_full_contiguous_and_recent_history_required(snapshot_factory, now):
    s = snapshot_factory(now=now)
    s.validate(s.candles, max_age=2)
    tf = Timeframe.M1
    for rows in [
        s.candles[tf][-61:],
        s.candles[tf][:50] + s.candles[tf][51:],
        s.candles[tf][:-2],
    ]:
        with pytest.raises(MarketDataError):
            replace(s, candles={**s.candles, tf: rows}).validate(s.candles, max_age=2)


def test_missing_native_bars_never_make_a_closed_higher_bar():
    def c(t):
        return Candle(datetime.fromtimestamp(t, timezone.utc), 1, 2, 0.5, 1.5)

    assert not aggregate_by_time([c(0), c(600)], 900, 900)[0].is_closed
    assert aggregate_by_time([c(0), c(300), c(600)], 900, 900)[0].is_closed
    partial = replace(c(300), is_closed=False)
    assert not aggregate_by_time([c(0), partial, c(600)], 900, 900)[0].is_closed


def test_internal_gap_stays_visible_and_first_bar_is_partial():
    s = CandleSeries(60, 400)
    s.ingest(60, 1)
    s.ingest(240, 1)
    s.ingest(300, 1)
    assert s.gap_seconds() == 120
    assert not s.candles()[0].is_closed
    s.seed([dict(time=60, open=1, high=1, low=1, close=1)])
    assert s.candles()[0].is_closed


def test_expiration_price_requires_close_ticks_on_both_sides():
    f = AssetFeed("EURUSD")
    f.on_tick(100, 1.1)
    f.on_tick(110, 1.2)
    assert f.price_at(105, max_age=0.5) is None
    f.on_tick(111, 1.3)
    assert f.price_at(110.8, max_age=1) == 1.2


@pytest.mark.asyncio
async def test_ohlc_history_uses_seconds_window_and_utc_pagination():
    client = AsyncMock()
    client.get_candles_advanced.return_value = [
        dict(time=100, open=1, high=1, low=1, close=1)
    ]
    lock = asyncio.Lock()
    rows = await fetch_history_page(
        client, "EURUSD", 300, 0, lock, 1800000000, count=1600
    )
    assert rows == client.get_candles_advanced.return_value
    client.get_candles_advanced.assert_awaited_with(
        "EURUSD", 300, 150 * 300, 1800000000
    )
    await fetch_history_page(client, "EURUSD", 300, 2, lock, 1800000000)
    client.get_candles_advanced.assert_awaited_with(
        "EURUSD", 300, 150 * 300, 1800000000 - 2 * 150 * 300
    )
    client.compile_candles.assert_not_awaited()
    client.history.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsubscribe_is_explicit_even_after_reader_failure():
    f = AssetFeed("EURUSD")
    stream = AsyncMock()
    f._stream = stream
    await f.stop(None)
    stream.unsubscribe.assert_awaited_once()


def test_invalid_numeric_timestamps_are_rejected(snapshot_factory):
    s = snapshot_factory()
    for bad in [float("nan"), float("inf")]:
        with pytest.raises(MarketDataError):
            replace(s, tick_time=bad).validate(s.candles, max_age=2)


@pytest.mark.asyncio
async def test_real_feed_provider_backfills_all_timeframes_from_native_ohlc():
    from unittest.mock import Mock

    from domain import CurrencyPair
    from market_data.pocket_option import PocketOptionProvider

    class Stream:
        def __init__(self):
            self.unsubscribe = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.01)
            return {"time": time.time(), "price": 1.1}

    stream = Stream()
    client = Mock()
    client.subscribe_symbol = AsyncMock(return_value=stream)
    client.wait_for_assets = AsyncMock()
    client.active_assets = AsyncMock(
        return_value=[{"symbol": "EURUSD", "is_active": True}]
    )
    requests = []

    async def page(asset, period, span, end):
        requests.append((period, span, end))
        await asyncio.sleep(0)
        start = int(end) // period * period
        return [
            dict(
                timestamp=start - i * period,
                open=1.1,
                high=1.1002,
                low=1.0998,
                close=1.1,
            )
            for i in range(150)
        ]

    client.get_candles_advanced = page
    provider = PocketOptionProvider("dummy", client=client)
    try:
        snap = await asyncio.wait_for(
            provider.get_snapshot(
                CurrencyPair("EURUSD"),
                [Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1],
            ),
            3,
        )
        snap.validate(snap.candles, max_age=2)
        await asyncio.wait_for(provider._feeds["EURUSD"].backfill_attempted.wait(), 1)
        assert len(requests) >= 3 + 2 + 1 + 10
        assert all(span == 150 * period for period, span, end in requests)
        assert all(end <= time.time() for period, span, end in requests)
    finally:
        await provider.aclose()
    stream.unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_short_snapshot_does_not_wait_for_deep_unused_history(
    snapshot_factory,
):
    from unittest.mock import Mock

    from domain import Expiration
    from market_data.pocket_option import PocketOptionProvider

    s = snapshot_factory(Expiration.SEC_15)
    rows = {tf.seconds: cs for tf, cs in s.candles.items()}
    f = Mock()
    f.closed = False
    f.backfill_attempted = asyncio.Event()
    f.candles = lambda seconds, base: rows[seconds]
    f.last_price = s.price
    f.feed_now = s.tick_time
    f.last_tick_monotonic = time.monotonic()
    f._tick_ts = [t for t, p in s.ticks]
    f._tick_px = [p for t, p in s.ticks]
    provider = PocketOptionProvider("dummy", client=Mock())
    provider._ensure_feed = AsyncMock(return_value=f)
    snap = await asyncio.wait_for(provider.get_snapshot(s.pair, s.candles), 0.2)
    snap.validate(s.candles, max_age=1)


@pytest.mark.asyncio
async def test_historical_tick_warmup_does_not_refresh_quote_age_or_change_last_price():
    f = AssetFeed("EURUSD")
    now = time.time()
    f.on_tick(now - 0.1, 1.1)
    received = f.last_tick_monotonic
    client = AsyncMock()
    client.get_ticks.return_value = [
        [now - 25, 1.09],
        [now - 10, 1.095],
        [now - 0.1, 9],
        [now + 100, 9],
    ]
    await f._prime_recent_ticks(client, asyncio.Lock())
    assert f.last_tick_monotonic == received
    assert f.last_price == 1.1 and f.feed_now == now - 0.1
    assert len(f._tick_ts) == 3
    assert list(f._tick_ts) == sorted(f._tick_ts)
