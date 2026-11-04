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


def test_full_contiguous_and_recent_history_required(snapshot_factory):
    s = snapshot_factory()
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
async def test_history_failure_falls_back_to_chart_history():
    client = AsyncMock()
    client.compile_candles.side_effect = RuntimeError("unavailable")
    client.history.return_value = [dict(time=100, open=1, high=1, low=1, close=1)]
    rows = await fetch_history_page(client, "EURUSD", 60, 0, asyncio.Lock(), 1000)
    assert rows == client.history.return_value


@pytest.mark.asyncio
async def test_unsubscribe_is_explicit_even_after_reader_failure():
    f = AssetFeed("EURUSD")
    stream = AsyncMock()
    f._stream = stream
    await f.stop(None)
    stream.unsubscribe.assert_awaited_once()


def test_invalid_numeric_timestamps_are_rejected(snapshot_factory):
    s=snapshot_factory()
    for bad in [float('nan'),float('inf')]:
        with pytest.raises(MarketDataError):
            replace(s,tick_time=bad).validate(s.candles,max_age=2)
