"""Always-on, in-memory candle cache fed by one persistent tick subscription
per asset.

Why this exists: the old path called the library's `get_candles_live()` on
every button press, once per timeframe, one after another. Every such call
re-subscribes and re-downloads history through three *sequential* requests
with a 3-second timeout each — up to ~9 s per timeframe, ~27 s per signal,
and the data could already be seconds old by the time analysis ran.

Here, the first request for an asset starts ONE tick subscription and loads
the newest history page of 1-minute and 5-minute native candles (~0.7 s);
deeper history (5 days, for H1) follows in the background, page by page.
From then on every tick updates both series in place, so a signal request
just reads memory: the latest 120+ candles on every timeframe, including
the still-forming one, with no network round-trip at all.

Candle times follow the same convention as the library's own
`get_candles_live()` (platform timestamps shifted by `PLATFORM_TIME_OFFSET`),
and a bar counts as closed once a tick from a later bucket has arrived —
i.e. closedness is judged by the feed's own clock, never the local PC clock,
so a skewed Windows clock can't mislabel a bar.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections import deque
from datetime import datetime, timezone

from market_data.models import Candle, MarketDataError

logger = logging.getLogger(__name__)

# Same shift the library applies in get_candles_live() (platform time is UTC+2).
PLATFORM_TIME_OFFSET = 7200

# Native periods kept per asset. Every analysis timeframe is built from one
# of these: M1 from 60 s, M5/M15/M30/H1 from 300 s (see domain.PocketOptionBase).
BASE_PERIODS = (60, 300)

# How many native candles to keep per period. 300 s x 1500 = 125 h, enough
# for 120 closed H1 bars (1440 five-minute candles) plus margin.
MAX_ROWS = {60: 400, 300: 1600}

# Raw ticks kept for exact price-at-expiration lookups (storage.resolver).
# Pocket Option sends a few ticks per second; 60k is comfortably > 1 hour.
MAX_TICKS = 60_000

# A feed that hasn't seen a tick for this long is treated as stale.
STALE_AFTER_SECONDS = 25.0

HISTORY_TIMEOUT_SECONDS = 10.0

# Pocket Option serves at most ~150 bars per history request (verified
# live), so deeper history is read page by page.
PAGE_ROWS = 150

# How often the newest history page is re-read to repair tick-built bars.
REFRESH_SECONDS = 60.0


def pages_needed(period: int) -> int:
    return -(-MAX_ROWS[period] // PAGE_ROWS)


async def fetch_history_page(
    client, asset: str, period: int, page: int, lock: asyncio.Lock, platform_now: int
) -> list[dict]:
    """One page of `PAGE_ROWS` bars ending `page` pages back from now.

    `get_candles_advanced(asset, period, offset_seconds, end_time)` takes
    the end time in PLATFORM time (UTC+2) — passing plain UTC returns data
    two hours old (verified live). The +600 s nudge makes page 0 include
    the newest closed bars. Returns [] on any failure.
    """
    span = PAGE_ROWS * period
    end = platform_now + 600 - page * span
    async with lock:
        try:
            rows = await asyncio.wait_for(
                client.get_candles_advanced(asset, period, span, end), timeout=HISTORY_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - unofficial client, best effort
            logger.debug("history page %s/%ss/%d failed: %s", asset, period, page, exc)
            return []
    return [r for r in rows or [] if isinstance(r, dict)]


def _to_seconds(raw: object) -> float:
    value = float(raw)  # type: ignore[arg-type]
    if value > 10_000_000_000:  # milliseconds
        value /= 1000.0
    return value - PLATFORM_TIME_OFFSET


def parse_tick(tick: object) -> tuple[float, float] | None:
    """(timestamp, price) from one subscription message, or None if it
    doesn't look like a price update. Accepts the dict shape the library
    yields ({"timestamp"/"time", "close"/"price"}) and the raw
    [asset, timestamp, price] list Pocket Option uses on the wire.
    """
    try:
        if isinstance(tick, dict):
            raw_ts = tick.get("timestamp", tick.get("time"))
            raw_price = tick.get("close", tick.get("price"))
            if raw_ts is None or raw_price is None:
                return None
            return _to_seconds(raw_ts), float(raw_price)
        if isinstance(tick, (list, tuple)) and len(tick) >= 3:
            return _to_seconds(tick[1]), float(tick[2])
    except (TypeError, ValueError):
        return None
    return None


class CandleSeries:
    """Native-period OHLC series: closed bars + the one currently forming."""

    def __init__(self, period: int, max_rows: int) -> None:
        self.period = period
        self._bars: deque[list[float]] = deque(maxlen=max_rows)  # [t, o, h, l, c]
        self._forming: list[float] | None = None

    def seed(self, history: list[dict]) -> None:
        """Merges server candles in. For a CLOSED bucket the server's bar
        wins over one built from our ticks: the tick-built bar may be
        partial (the feed started mid-bar, or ticks were dropped), the
        server's is complete. The forming bar always stays tick-built."""
        merged: dict[int, list[float]] = {int(bar[0]): bar for bar in self._bars}
        for item in history:
            if item.get("is_closed") is False:
                continue
            try:
                t = int(_to_seconds(item.get("time", item.get("timestamp"))))
                bar = [
                    float(t - t % self.period),
                    float(item["open"]),
                    float(item["high"]),
                    float(item["low"]),
                    float(item["close"]),
                ]
            except (KeyError, TypeError, ValueError):
                continue
            merged[int(bar[0])] = bar
        forming_start = self._forming[0] if self._forming else None
        ordered = [merged[k] for k in sorted(merged) if forming_start is None or k < forming_start]
        self._bars = deque(ordered[-self._bars.maxlen :], maxlen=self._bars.maxlen)  # type: ignore[index]

    def ingest(self, ts: float, price: float) -> None:
        start = float(int(ts) - int(ts) % self.period)
        forming = self._forming
        if forming is None:
            if self._bars and start <= self._bars[-1][0]:
                return  # late tick for a bar already sealed from history
            self._forming = [start, price, price, price, price]
            return
        if start == forming[0]:
            if price > forming[2]:
                forming[2] = price
            if price < forming[3]:
                forming[3] = price
            forming[4] = price
        elif start > forming[0]:
            self._bars.append(forming)
            self._forming = [start, price, price, price, price]
        # start < forming start: out-of-order tick, ignore

    def gap_seconds(self) -> float:
        if not self._bars or self._forming is None:
            return 0.0
        return max(0.0, self._forming[0] - self._bars[-1][0] - self.period)

    def candles(self) -> list[Candle]:
        out = [_candle(bar, True) for bar in self._bars]
        if self._forming is not None:
            out.append(_candle(self._forming, False))
        return out

    def __len__(self) -> int:
        return len(self._bars)


def _candle(bar: list[float], is_closed: bool) -> Candle:
    return Candle(
        time=datetime.fromtimestamp(bar[0], tz=timezone.utc),
        open=bar[1],
        high=bar[2],
        low=bar[3],
        close=bar[4],
        is_closed=is_closed,
    )


def aggregate_by_time(base: list[Candle], period_seconds: int, now_ts: float | None) -> list[Candle]:
    """Groups native candles into `period_seconds` bars aligned to clock
    boundaries (a 15-minute bar is always :00-:15, :15-:30, ...), not
    "every N candles from wherever the list happens to start" — the old
    grouping produced bars like 10:05-10:20 whenever the history's first
    candle wasn't on a boundary, which skews every level and indicator on
    M15/M30/H1.

    A bar is closed once `now_ts` (the feed's latest tick time) has reached
    its end; a bar with gaps inside (market pause) still closes by time.
    """
    grouped: list[Candle] = []
    current_key: int | None = None
    o = h = l = c = 0.0
    for candle in base:
        ts = int(candle.time.timestamp())
        key = ts - ts % period_seconds
        if key != current_key:
            if current_key is not None:
                grouped.append(_bar(current_key, o, h, l, c, period_seconds, now_ts))
            current_key, o, h, l, c = key, candle.open, candle.high, candle.low, candle.close
        else:
            h = max(h, candle.high)
            l = min(l, candle.low)
            c = candle.close
    if current_key is not None:
        grouped.append(_bar(current_key, o, h, l, c, period_seconds, now_ts))
        if base and not base[-1].is_closed and grouped[-1].is_closed:
            # the underlying native bar is still forming, so this group is too
            grouped[-1] = Candle(grouped[-1].time, o, h, l, c, is_closed=False)
    return grouped


def _bar(key: int, o: float, h: float, l: float, c: float, period: int, now_ts: float | None) -> Candle:
    closed = now_ts is None or key + period <= now_ts
    return Candle(
        time=datetime.fromtimestamp(key, tz=timezone.utc),
        open=o, high=h, low=l, close=c, is_closed=closed,
    )


class AssetFeed:
    """One asset's live subscription + candle series + recent ticks."""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self.series = {p: CandleSeries(p, MAX_ROWS[p]) for p in BASE_PERIODS}
        self._tick_ts: deque[float] = deque(maxlen=MAX_TICKS)
        self._tick_px: deque[float] = deque(maxlen=MAX_TICKS)
        self.last_tick_monotonic: float | None = None
        # feed-clock minus local-clock, smoothed; lets callers translate a
        # local datetime (e.g. a signal's expires_at) into feed time.
        self.clock_offset: float | None = None
        self.last_used_monotonic = time.monotonic()
        self.history_loaded = False
        self._first_tick = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._maintainer: asyncio.Task | None = None
        self._stream = None
        self.closed = False
        self.error: str | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self, client, lock: asyncio.Lock | None = None) -> None:
        """Subscribes to ticks, loads the newest history page of each period
        (~0.7 s — enough for M1/M5 right away), then deepens the history in
        the background. `lock` serializes history requests: the library
        mixes up concurrent history responses (verified live — parallel
        requests all time out), so every request goes one at a time."""
        lock = lock or asyncio.Lock()
        self._stream = await client.subscribe_symbol(self.asset)
        self._reader = asyncio.create_task(self._read(), name=f"ticks:{self.asset}")
        for period in BASE_PERIODS:
            await self._load_pages(client, lock, period, range(0, 1))
        self.history_loaded = any(len(s) > 0 for s in self.series.values())
        logger.info(
            "Feed %s seeded: %s", self.asset,
            ", ".join(f"{p}s={len(self.series[p])}" for p in BASE_PERIODS),
        )
        self._maintainer = asyncio.create_task(self._maintain(client, lock), name=f"history:{self.asset}")

    async def _read(self) -> None:
        try:
            async for tick in self._stream:
                parsed = parse_tick(tick)
                if parsed is not None:
                    self.on_tick(*parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # unofficial client: no fixed exception hierarchy
            self.error = str(exc)
            logger.warning("Tick stream for %s ended: %s", self.asset, exc)
        finally:
            self.closed = True

    def on_tick(self, ts: float, price: float) -> None:
        for s in self.series.values():
            s.ingest(ts, price)
        self._tick_ts.append(ts)
        self._tick_px.append(price)
        now_mono = time.monotonic()
        self.last_tick_monotonic = now_mono
        offset = ts - time.time()
        if self.clock_offset is None:
            self.clock_offset = offset
        else:
            # latency only ever makes a tick look *older*, so track the
            # upper envelope with slow decay rather than a plain mean
            self.clock_offset = max(offset, self.clock_offset - 0.05)
        self._first_tick.set()

    async def _load_pages(self, client, lock: asyncio.Lock, period: int, pages: range) -> int:
        loaded = 0
        platform_now = int(time.time()) + PLATFORM_TIME_OFFSET
        for page in pages:
            rows = await fetch_history_page(client, self.asset, period, page, lock, platform_now)
            if not rows:
                break  # no older data (or a failure) — deeper pages won't help
            self.series[period].seed(rows)
            loaded += len(rows)
        return loaded

    async def _maintain(self, client, lock: asyncio.Lock) -> None:
        """Background: first deepen the history to the full MAX_ROWS, then
        every REFRESH_SECONDS re-read the newest page so any bar built from
        an incomplete set of ticks gets replaced by the server's bar."""
        try:
            for period in BASE_PERIODS:
                await self._load_pages(client, lock, period, range(1, pages_needed(period)))
            logger.info(
                "Feed %s history complete: %s", self.asset,
                ", ".join(f"{p}s={len(self.series[p])}" for p in BASE_PERIODS),
            )
            # The server publishes its newest bars ~2-4 minutes late
            # (verified live), so right after start there's a hole between
            # its last bar and our first tick. Re-read the newest page soon
            # and often until that hole is closed, then once a minute.
            delays = [15.0, 15.0, 30.0, 30.0, 30.0]
            while not self.closed:
                await asyncio.sleep(delays.pop(0) if delays and self.gap_seconds(60) > 0 else REFRESH_SECONDS)
                for period in BASE_PERIODS:
                    await self._load_pages(client, lock, period, range(0, 1))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - maintenance must never kill the feed
            logger.exception("History maintenance for %s failed", self.asset)
    async def wait_first_tick(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._first_tick.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def stop(self, client) -> None:
        self.closed = True
        for task in (self._maintainer, self._reader):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._stream = None  # dropping the subscription unsubscribes it

    # -- reads -----------------------------------------------------------
    @property
    def tick_age(self) -> float | None:
        if self.last_tick_monotonic is None:
            return None
        return time.monotonic() - self.last_tick_monotonic

    @property
    def is_stale(self) -> bool:
        age = self.tick_age
        return self.closed or age is None or age > STALE_AFTER_SECONDS

    @property
    def last_price(self) -> float | None:
        return self._tick_px[-1] if self._tick_px else None

    @property
    def feed_now(self) -> float | None:
        return self._tick_ts[-1] if self._tick_ts else None

    def gap_seconds(self, base_period: int) -> float:
        """Missing time between the last closed bar and the forming one
        (0 when the series is continuous)."""
        return self.series[base_period].gap_seconds()

    def candles(self, period_seconds: int, base_period: int) -> list[Candle]:
        base = self.series[base_period].candles()
        if period_seconds == base_period:
            return base
        return aggregate_by_time(base, period_seconds, self.feed_now)

    def local_to_feed_time(self, when: datetime) -> float:
        offset = self.clock_offset or 0.0
        return when.timestamp() + offset

    def price_at(self, feed_ts: float) -> float | None:
        """Last tick price at or before `feed_ts`, or None if the tick
        history doesn't reach back that far / hasn't reached it yet."""
        if not self._tick_ts or feed_ts < self._tick_ts[0] or feed_ts > self._tick_ts[-1]:
            return None
        idx = bisect.bisect_right(self._tick_ts, feed_ts) - 1
        if idx < 0:
            return None
        return self._tick_px[idx]


class FeedUnavailableError(MarketDataError):
    """No live quotes arrive for an asset (dead session or closed market)."""
