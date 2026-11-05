"""One UTC tick subscription per asset and repairable candle caches.

Pinned binaryoptionstoolsv2 0.2.17 supplies UTC seconds. Backfill is read
serially, 15s/1m/5m candles are updated by ticks, larger bars require all
native constituents. The first partial live bar is not trusted until repaired
by historical OHLC. No source latency is hidden by clock-offset estimation.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone

from market_data.models import Candle, MarketDataError

logger = logging.getLogger(__name__)

# UTC contract of the pinned engine; UI timezone is applied only to messages.
PLATFORM_TIME_OFFSET = 0  # binaryoptionstoolsv2 0.2.17 supplies UTC

# Native periods kept per asset. Every analysis timeframe is built from one
# of these: M1 from 60 s, M5/M15/M30/H1 from 300 s (see domain.PocketOptionBase).
BASE_PERIODS = (15, 60, 300)

# How many native candles to keep per period. 300 s x 1500 = 125 h, enough
# for 120 closed H1 bars (1440 five-minute candles) plus margin.
MAX_ROWS = {15: 200, 60: 400, 300: 1600}

# Raw ticks kept for exact price-at-expiration lookups (storage.resolver).
# Pocket Option sends a few ticks per second; 60k is comfortably > 1 hour.
MAX_TICKS = 60_000

# A feed that hasn't seen a tick for this long is treated as stale.
STALE_AFTER_SECONDS = 25.0

HISTORY_TIMEOUT_SECONDS = 10.0

# Keep history requests bounded; deeper history is read page by page.
PAGE_ROWS = 150

# How often the newest history page is re-read to repair tick-built bars.
REFRESH_SECONDS = 60.0


async def fetch_history_page(
    client,
    asset: str,
    period: int,
    page: int,
    lock: asyncio.Lock,
    platform_now: int,
    count: int | None = None,
) -> list[dict]:
    """One OHLC page using the verified 0.2.17 Rust request contract.

    offset is a window in SECONDS, not a candle count. Despite stale Python
    docstrings, the pinned Rust engine sends loadHistoryPeriod (not Fast).
    compile_candles/history instead paginate raw ticks and may discard the
    whole backfill when an outer timeout expires.
    """
    wanted = min(count or PAGE_ROWS, PAGE_ROWS)
    end = platform_now - page * PAGE_ROWS * period
    async with lock:
        try:
            rows = await asyncio.wait_for(
                client.get_candles_advanced(asset, period, wanted * period, end),
                timeout=HISTORY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "OHLC history unavailable for %s/%ss/page%d (%s)",
                asset,
                period,
                page,
                type(exc).__name__,
            )
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
        self._partial: set[int] = set()

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
                if t >= int(time.time()) // self.period * self.period:
                    continue  # historical source may include the forming bucket
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
            self._partial.discard(int(bar[0]))
        forming_start = self._forming[0] if self._forming else None
        ordered = [
            merged[k]
            for k in sorted(merged)
            if forming_start is None or k < forming_start
        ]
        self._bars = deque(ordered[-self._bars.maxlen :], maxlen=self._bars.maxlen)  # type: ignore[index]

    def ingest(self, ts: float, price: float) -> None:
        start = float(int(ts) - int(ts) % self.period)
        forming = self._forming
        if forming is None:
            if self._bars and start <= self._bars[-1][0]:
                return  # late tick for a bar already sealed from history
            self._forming = [start, price, price, price, price]
            self._partial.add(int(start))  # subscription may start mid-candle
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
        times = [b[0] for b in self._bars] + [self._forming[0]]
        return sum(max(0.0, b - a - self.period) for a, b in zip(times, times[1:]))

    def candles(self) -> list[Candle]:
        out = [_candle(bar, int(bar[0]) not in self._partial) for bar in self._bars]
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


def aggregate_by_time(
    base: list[Candle], period_seconds: int, now_ts: float | None
) -> list[Candle]:
    """Groups native candles into `period_seconds` bars aligned to clock
    boundaries (a 15-minute bar is always :00-:15, :15-:30, ...), not
    "every N candles from wherever the list happens to start" — the old
    grouping produced bars like 10:05-10:20 whenever the history's first
    candle wasn't on a boundary, which skews every level and indicator on
    M15/M30/H1.

    A bar is closed once `now_ts` (the feed's latest tick time) has reached
    its end; a bar with gaps inside (market pause) still closes by time.
    """
    if not base:
        return []
    native = min(
        (
            int((b.time - a.time).total_seconds())
            for a, b in zip(base, base[1:])
            if b.time > a.time
        ),
        default=period_seconds,
    )
    # Use native periods explicitly for larger TFs, even if every other bar
    # is missing: a regular gap must not be mistaken for a wider native bar.
    native = 300 if period_seconds >= 900 else min(native, period_seconds)
    groups: dict[int, list[Candle]] = {}
    for c in base:
        key = int(c.time.timestamp()) // period_seconds * period_seconds
        groups.setdefault(key, []).append(c)
    out = []
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda c: c.time)
        expected = list(range(key, key + period_seconds, native))
        complete = [int(c.time.timestamp()) for c in rows] == expected and all(
            c.is_closed for c in rows
        )
        out.append(
            Candle(
                datetime.fromtimestamp(key, tz=timezone.utc),
                rows[0].open,
                max(c.high for c in rows),
                min(c.low for c in rows),
                rows[-1].close,
                is_closed=complete
                and now_ts is not None
                and key + period_seconds <= now_ts,
            )
        )
    return out


def _bar(
    key: int, o: float, h: float, l: float, c: float, period: int, now_ts: float | None
) -> Candle:
    closed = now_ts is None or key + period <= now_ts
    return Candle(
        time=datetime.fromtimestamp(key, tz=timezone.utc),
        open=o,
        high=h,
        low=l,
        close=c,
        is_closed=closed,
    )


class AssetFeed:
    """One asset's live subscription + candle series + recent ticks."""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self.series = {p: CandleSeries(p, MAX_ROWS[p]) for p in BASE_PERIODS}
        self._tick_ts: deque[float] = deque(maxlen=MAX_TICKS)
        self._tick_px: deque[float] = deque(maxlen=MAX_TICKS)
        self.last_tick_monotonic: float | None = None
        self.last_used_monotonic = time.monotonic()
        self.history_loaded = False
        self.backfill_attempted = asyncio.Event()
        self.required_rows = {p: 0 for p in BASE_PERIODS}
        self._history_requested = asyncio.Event()
        self._history_revision = 0
        self._first_tick = asyncio.Event()
        self._repair_needed = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._maintainer: asyncio.Task | None = None
        self._stream = None
        self.closed = False
        self.error: str | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self, client, lock: asyncio.Lock | None = None) -> None:
        """Start one tick subscription, seed recent native OHLC pages, and
        deepen history in the background using serialized requests."""
        lock = lock or asyncio.Lock()
        self._stream = await client.subscribe_symbol(self.asset)
        self._reader = asyncio.create_task(self._read(), name=f"ticks:{self.asset}")
        for period in BASE_PERIODS:
            await self._load_pages(client, lock, period, range(0, 1))
        await self._prime_recent_ticks(client, lock)
        self.history_loaded = any(len(s) > 0 for s in self.series.values())
        logger.info(
            "Feed %s seeded: %s",
            self.asset,
            ", ".join(f"{p}s={len(self.series[p])}" for p in BASE_PERIODS),
        )
        self._maintainer = asyncio.create_task(
            self._maintain(client, lock), name=f"history:{self.asset}"
        )

    def request_history(self, timeframes, count=120):
        """Load only the native depth needed by the requested analysis.

        One extra aggregate covers the incomplete oldest bucket. Requirements
        can grow if the same live subscription is later used for a longer expiry.
        """
        changed = False
        for tf in timeframes:
            period = tf.pocket_option_base.period_seconds
            needed = min(MAX_ROWS[period], (count + 1) * (tf.seconds // period))
            if needed > self.required_rows[period]:
                self.required_rows[period] = needed
                changed = True
        if changed:
            self._history_revision += 1
            self.backfill_attempted.clear()
            self._history_requested.set()

    async def _prime_recent_ticks(self, client, lock):
        """Warm the momentum window using genuine ticks, without treating a
        historical response as a newly received live quote."""
        if not self._tick_ts:
            return
        try:
            async with lock:
                rows = await asyncio.wait_for(
                    client.get_ticks(self.asset, 30), timeout=5
                )
        except Exception:
            return
        if not isinstance(rows, list):
            return
        last_live = self._tick_ts[-1]
        merged = {ts: px for ts, px in zip(self._tick_ts, self._tick_px)}
        for row in rows:
            try:
                ts, price = float(row[0]), float(row[1])
                if ts > 10_000_000_000:
                    ts /= 1000
                if (
                    last_live - 120 <= ts < last_live
                    and math.isfinite(price)
                    and price > 0
                ):
                    merged.setdefault(ts, price)
            except (TypeError, ValueError, IndexError):
                continue
        ordered = sorted(merged.items())[-MAX_TICKS:]
        self._tick_ts = deque((t for t, p in ordered), maxlen=MAX_TICKS)
        self._tick_px = deque((p for t, p in ordered), maxlen=MAX_TICKS)

    async def _read(self) -> None:
        try:
            async for tick in self._stream:
                parsed = parse_tick(tick)
                if parsed is not None:
                    self.on_tick(*parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # unofficial client: no fixed exception hierarchy
            self.error = f"Поток котировок завершился ({type(exc).__name__})"
            logger.warning(
                "Tick stream for %s ended (%s)", self.asset, type(exc).__name__
            )
        finally:
            self.closed = True

    def on_tick(self, ts: float, price: float) -> None:
        if not math.isfinite(ts) or not math.isfinite(price) or price <= 0:
            return
        if self._tick_ts and (
            ts < self._tick_ts[-1]
            or (ts == self._tick_ts[-1] and price == self._tick_px[-1])
        ):
            return  # preserve sorted tick history and the newest quote
        if ts > time.time() + 0.5:
            self.error = (
                "Время котировок в будущем: включите синхронизацию часов Windows"
            )
            return  # fail closed if machine/feed clocks disagree
        for s in self.series.values():
            s.ingest(ts, price)
            if s._bars and int(s._bars[-1][0]) in s._partial:
                self._repair_needed.set()
        self._tick_ts.append(ts)
        self._tick_px.append(price)
        now_mono = time.monotonic()
        self.last_tick_monotonic = now_mono
        self._first_tick.set()

    async def _load_pages(
        self,
        client,
        lock: asyncio.Lock,
        period: int,
        pages: range,
        *,
        recent: bool = False,
    ) -> int:
        loaded = 0
        platform_now = int(time.time()) + PLATFORM_TIME_OFFSET
        for page in pages:
            rows = await fetch_history_page(
                client,
                self.asset,
                period,
                page,
                lock,
                platform_now,
                count=PAGE_ROWS if recent else MAX_ROWS[period],
            )
            if not rows:
                break  # no older data (or a failure) — deeper pages won't help
            self.series[period].seed(rows)
            loaded += len(rows)
        return loaded

    async def _maintain(self, client, lock: asyncio.Lock) -> None:
        """Backfill requested timeframes and repair recent bars periodically."""
        try:
            while not self.closed:
                self._history_requested.clear()
                revision = self._history_revision
                targets = dict(self.required_rows)
                for period, needed in targets.items():
                    if len(self.series[period]) < needed:
                        pages = (needed + PAGE_ROWS - 1) // PAGE_ROWS
                        await self._load_pages(client, lock, period, range(1, pages))
                if revision != self._history_revision:
                    continue  # a longer expiry was requested during the fetch
                self.backfill_attempted.set()
                logger.debug(
                    "Feed %s requested history coverage: %s",
                    self.asset,
                    ", ".join(
                        f"{p}s={len(self.series[p])}/{n}"
                        for p, n in targets.items()
                        if n
                    ),
                )
                missing = any(len(self.series[p]) < n for p, n in targets.items())
                delay = 5.0 if missing else REFRESH_SECONDS
                repairs = asyncio.create_task(self._repair_needed.wait())
                requested = asyncio.create_task(self._history_requested.wait())
                try:
                    await asyncio.wait(
                        (repairs, requested),
                        timeout=delay,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for waiter in (repairs, requested):
                        waiter.cancel()
                    await asyncio.gather(repairs, requested, return_exceptions=True)
                if self._history_requested.is_set():
                    continue  # prioritize the new depth before periodic repairs
                self._repair_needed.clear()
                for period in BASE_PERIODS:
                    await self._load_pages(
                        client, lock, period, range(0, 1), recent=True
                    )
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
        stream, self._stream = self._stream, None
        if stream is not None and hasattr(stream, "unsubscribe"):
            try:
                await asyncio.wait_for(stream.unsubscribe(), timeout=5)
            except Exception:
                logger.warning("Could not unsubscribe %s", self.asset)

    # -- reads -----------------------------------------------------------
    @property
    def tick_age(self) -> float | None:
        if self.last_tick_monotonic is None:
            return None
        return max(
            time.monotonic() - self.last_tick_monotonic,
            time.time() - (self.feed_now or 0),
        )

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
        return when.timestamp()  # engine and database both use UTC

    def price_at(self, feed_ts: float, max_age: float = 2.0) -> float | None:
        """Last tick price at or before `feed_ts`, or None if the tick
        history doesn't reach back that far / hasn't reached it yet."""
        if (
            not self._tick_ts
            or feed_ts < self._tick_ts[0]
            or feed_ts > self._tick_ts[-1]
        ):
            return None
        idx = bisect.bisect_right(self._tick_ts, feed_ts) - 1
        if idx < 0 or feed_ts - self._tick_ts[idx] > max_age:
            return None
        if self._tick_ts[idx] < feed_ts and (
            idx + 1 >= len(self._tick_ts) or self._tick_ts[idx + 1] - feed_ts > max_age
        ):
            return None
        return self._tick_px[idx]


class FeedUnavailableError(MarketDataError):
    """No live quotes arrive for an asset (dead session or closed market)."""
