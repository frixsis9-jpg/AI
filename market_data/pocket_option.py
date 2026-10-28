"""Pocket Option implementation of MarketDataProvider.

Talks to Pocket Option's own (unofficial, reverse-engineered) WebSocket feed
via the `binaryoptionstoolsv2` package, authenticated with a session id
(SSID) copied from an already-logged-in browser — see README for how to get
one. This is NOT an official Pocket Option API; there isn't one.

Candles come from `market_data.live_feed`: one persistent tick subscription
per asset keeps 1-minute and 5-minute series current in memory, so a signal
request reads ready data instantly instead of re-downloading history per
timeframe (see that module's docstring for the numbers behind this change).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Protocol

from domain import REGULAR_PAIRS, CurrencyPair, Timeframe
from market_data.live_feed import AssetFeed, FeedUnavailableError
from market_data.models import Candle, MarketDataError
from market_data.provider import MarketDataProvider

logger = logging.getLogger(__name__)

# How long a list_available_pairs() result stays cached. That call is made
# every time the user opens the 💱 menu.
_PAIR_LIST_CACHE_SECONDS = 300.0

# How long to wait for the first tick of a freshly started feed.
_FIRST_TICK_TIMEOUT_SECONDS = 12.0

# Feeds not used for this long are closed (resolver lookups count as use,
# and the longest expiration is 1 h, so pending signals keep theirs alive).
_FEED_IDLE_SECONDS = 75 * 60

# Hard cap on simultaneously open subscriptions.
_MAX_FEEDS = 12

_PROBE_PREFERENCE = ("EURUSD_otc", "EURUSD", "GBPUSD_otc", "AUDCAD_otc")


def _looks_like_currency_pair(symbol: str) -> bool:
    """Keeps active_assets() entries that look like a plain currency pair
    (6 letters, optionally with an "_otc" suffix) and drops everything else
    (commodities, indices, crypto, stocks — the analysis was built for
    forex-style price action only).
    """
    base = symbol.removesuffix("_otc")
    return len(base) == 6 and base.isalpha()


def _pairs_from_active_assets(assets: list) -> list[CurrencyPair]:
    seen: set[str] = set()
    pairs: list[CurrencyPair] = []
    for item in assets:
        if not isinstance(item, dict) or not item.get("is_active"):
            continue
        symbol = item.get("symbol")
        if not symbol or symbol in seen or not _looks_like_currency_pair(symbol):
            continue
        seen.add(symbol)
        pairs.append(CurrencyPair(symbol))
    pairs.sort(key=lambda p: (p.is_otc, p.label))
    return pairs


def _patch_broken_history_method(client) -> None:
    """binaryoptionstoolsv2 0.2.14 shipped `history()` with an empty body
    (always returned None, crashing get_candles_live). Wrap it so a None or
    a failure degrades to "no rows" instead of an exception; harmless on
    0.2.15+, where the method works.
    """
    original = getattr(client, "history", None)
    if original is None or getattr(original, "_none_safe", False):
        return

    async def safe_history(asset: str, period: int) -> list[dict]:
        try:
            result = await original(asset, period)
        except Exception:  # noqa: BLE001 - optional history source
            return []
        return result or []

    safe_history._none_safe = True  # type: ignore[attr-defined]
    client.history = safe_history  # type: ignore[method-assign]


class SupportsPocketOptionClient(Protocol):
    """The subset of PocketOptionAsync's interface this provider relies on."""

    async def subscribe_symbol(self, asset: str): ...

    async def get_candles_advanced(self, asset: str, period: int, offset: int, time: int) -> list[dict]: ...

    async def history(self, asset: str, period: int) -> list[dict]: ...

    async def active_assets(self) -> list[dict]: ...

    async def wait_for_assets(self, timeout: float = 60.0) -> None: ...

    async def shutdown(self) -> None: ...


class PocketOptionProvider(MarketDataProvider):
    """Serves real Pocket Option candles from always-on live feeds."""

    def __init__(self, ssid: str, client: SupportsPocketOptionClient | None = None) -> None:
        if not ssid and client is None:
            raise ValueError("Pocket Option SSID must not be empty")
        self._ssid = ssid
        self._client = client
        self._owns_client = client is None
        self._ever_succeeded = False
        self._pair_list_cache: list[CurrencyPair] | None = None
        self._pair_list_cache_at: float = 0.0
        self._feeds: dict[str, AssetFeed] = {}
        self._feed_locks: dict[str, asyncio.Lock] = {}
        self._warmups: set[asyncio.Task] = set()
        # One history request at a time across ALL assets — the library
        # mixes up concurrent history responses (see live_feed.start).
        self._history_lock: asyncio.Lock | None = None

    async def _get_client(self) -> SupportsPocketOptionClient:
        if self._client is None:
            from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

            client = PocketOptionAsync(self._ssid)
            _patch_broken_history_method(client)
            self._client = client
        return self._client

    # -- live feeds ------------------------------------------------------
    async def _ensure_feed(self, pair: CurrencyPair) -> AssetFeed:
        asset = pair.pocket_option_symbol
        feed = self._feeds.get(asset)
        if feed is not None and not feed.is_stale:
            feed.last_used_monotonic = time.monotonic()
            return feed

        lock = self._feed_locks.setdefault(asset, asyncio.Lock())
        async with lock:
            feed = self._feeds.get(asset)
            if feed is not None and not feed.is_stale:
                feed.last_used_monotonic = time.monotonic()
                return feed

            client = await self._get_client()
            if feed is not None:
                logger.info("Restarting stale feed for %s (tick age %s)", asset, feed.tick_age)
                await feed.stop(client)
                self._feeds.pop(asset, None)

            active_symbols = await self._list_active_pocket_option_symbols(client)
            if active_symbols is not None and asset not in active_symbols:
                raise MarketDataError(self._inactive_asset_message(pair, active_symbols))

            await self._reap_idle_feeds(client)
            feed = AssetFeed(asset)
            if self._history_lock is None:
                self._history_lock = asyncio.Lock()
            try:
                await feed.start(client, self._history_lock)
            except Exception as exc:  # unofficial client: no fixed exception hierarchy
                await feed.stop(client)
                raise MarketDataError(self._connection_error_message(pair, exc)) from exc

            if not await feed.wait_first_tick(_FIRST_TICK_TIMEOUT_SECONDS):
                await feed.stop(client)
                raise FeedUnavailableError(self._no_quotes_message(pair))

            self._feeds[asset] = feed
            self._ever_succeeded = True
            return feed

    async def _reap_idle_feeds(self, client) -> None:
        now = time.monotonic()
        idle = [
            a for a, f in self._feeds.items()
            if f.closed or now - f.last_used_monotonic > _FEED_IDLE_SECONDS
        ]
        overflow = len(self._feeds) - len(idle) - (_MAX_FEEDS - 1)
        if overflow > 0:
            by_age = sorted(
                (a for a in self._feeds if a not in idle),
                key=lambda a: self._feeds[a].last_used_monotonic,
            )
            idle += by_age[:overflow]
        for asset in idle:
            feed = self._feeds.pop(asset, None)
            if feed is not None:
                await feed.stop(client)

    def warm_up(self, pair: CurrencyPair) -> None:
        """Starts the pair's feed in the background (called the moment a
        user picks a pair), so by the time they press "Получить сигнал" the
        history is already loaded. Never raises."""

        async def run() -> None:
            try:
                await self._ensure_feed(pair)
            except Exception as exc:  # noqa: BLE001 - background prefetch
                logger.info("Warm-up for %s failed: %s", pair, exc)

        task = asyncio.create_task(run(), name=f"warmup:{pair}")
        self._warmups.add(task)
        task.add_done_callback(self._warmups.discard)

    async def get_candles(
        self,
        pair: CurrencyPair,
        timeframe: Timeframe,
        output_size: int = 150,
    ) -> list[Candle]:
        feed = await self._ensure_feed(pair)
        base = timeframe.pocket_option_base
        candles = feed.candles(timeframe.seconds, base.period_seconds)
        if not candles:
            raise MarketDataError(self._empty_result_message(pair, timeframe))
        return candles[-output_size:]

    async def get_live_snapshot(
        self, pair: CurrencyPair, timeframes: list[Timeframe], output_size: int
    ) -> tuple[dict[Timeframe, list[Candle]], float, float]:
        """Every requested timeframe read from ONE consistent moment of the
        feed, plus the latest tick price and its age in seconds."""
        feed = await self._ensure_feed(pair)
        result: dict[Timeframe, list[Candle]] = {}
        for tf in timeframes:
            candles = feed.candles(tf.seconds, tf.pocket_option_base.period_seconds)
            if candles:
                result[tf] = candles[-output_size:]
        price = feed.last_price
        if price is None:
            raise FeedUnavailableError(self._no_quotes_message(pair))
        return result, price, feed.tick_age or 0.0

    def history_gap_seconds(self, pair: CurrencyPair) -> float:
        """How many seconds of 1-minute bars are still missing between the
        server's history and the live ticks (only right after a feed starts,
        see live_feed.AssetFeed._maintain)."""
        feed = self._feeds.get(pair.pocket_option_symbol)
        return feed.gap_seconds(60) if feed is not None else 0.0

    async def price_at(self, pair: CurrencyPair, when: datetime) -> float | None:
        """Price at an exact moment (tick-accurate) if the live feed still
        covers it, else None. Used by storage.resolver to settle signals at
        the real expiration instant instead of "whenever the loop ran"."""
        feed = self._feeds.get(pair.pocket_option_symbol)
        if feed is None or feed.closed:
            return None
        feed.last_used_monotonic = time.monotonic()
        return feed.price_at(feed.local_to_feed_time(when))

    def feed_has_passed(self, pair: CurrencyPair, when: datetime) -> bool | None:
        """Whether the feed has already received a tick later than `when`
        (None if there's no live feed for the pair)."""
        feed = self._feeds.get(pair.pocket_option_symbol)
        if feed is None or feed.closed or feed.feed_now is None:
            return None
        return feed.feed_now >= feed.local_to_feed_time(when)

    # -- messages --------------------------------------------------------
    def _connection_error_message(self, pair: CurrencyPair, exc: Exception) -> str:
        base = f"Pocket Option error while subscribing to {pair.label}: {exc}"
        if self._ever_succeeded:
            return base
        return (
            base + ". Это первое обращение к Pocket Option за это подключение — "
            "наиболее вероятная причина: POCKET_OPTION_SSID неверный или "
            "устарел. Скопируйте свежий SSID из браузера (см. README, раздел "
            "«Как получить SSID») и проверьте его через "
            "scripts/check_pocket_option_connection.py, прежде чем запускать бота."
        )

    @staticmethod
    def _no_quotes_message(pair: CurrencyPair) -> str:
        return (
            f"Pocket Option не присылает котировки по {pair.label}: подключение "
            "есть, но за "
            f"{_FIRST_TICK_TIMEOUT_SECONDS:.0f} сек не пришло ни одного тика. "
            "Чаще всего это значит, что сессия (POCKET_OPTION_SSID) истекла — "
            "обновите её (capture_ssid_windows.bat) и перезапустите бота. "
            "Реже — актив сейчас на паузе у брокера."
        )

    def _empty_result_message(self, pair: CurrencyPair, timeframe: Timeframe) -> str:
        return (
            f"Pocket Option returned no candles for {pair.label} {timeframe.value} "
            "(is this asset open right now?)"
        )

    async def verify_connection(self, require_quotes: bool = False) -> None:
        """Checks the session. `active_assets()` alone is NOT proof of a
        working session — Pocket Option keeps sending the asset list even
        after the SSID has stopped receiving quotes (that's exactly how the
        bot went silently dead before). With `require_quotes=True` this also
        demands real ticks from one open asset.

        Raises MarketDataError on failure.
        """
        client = await self._get_client()
        try:
            await client.wait_for_assets()
            assets = await client.active_assets()
        except Exception as exc:  # unofficial client: no fixed exception hierarchy
            raise MarketDataError(self._verify_connection_error_message(exc)) from exc

        if require_quotes:
            if any(not f.is_stale for f in self._feeds.values()):
                self._ever_succeeded = True
                return
            probe = self._pick_probe_asset(assets)
            if probe is None:
                return  # nothing is open at all; can't judge quotes right now
            await self._ensure_feed(probe)
        self._ever_succeeded = True

    @staticmethod
    def _pick_probe_asset(assets: list) -> CurrencyPair | None:
        pairs = _pairs_from_active_assets(assets)
        symbols = {str(p) for p in pairs}
        for preferred in _PROBE_PREFERENCE:
            if preferred in symbols:
                return CurrencyPair(preferred)
        return pairs[0] if pairs else None

    def _verify_connection_error_message(self, exc: Exception) -> str:
        base = f"Pocket Option connection check failed: {exc}"
        if self._ever_succeeded:
            return base
        return (
            base + ". Это первое обращение к Pocket Option за это подключение — "
            "наиболее вероятная причина: POCKET_OPTION_SSID неверный или "
            "устарел. Скопируйте свежий SSID из браузера (см. README, раздел "
            "«Как получить SSID») и проверьте его через "
            "scripts/check_pocket_option_connection.py."
        )

    async def _list_active_pocket_option_symbols(
        self, client: SupportsPocketOptionClient
    ) -> set[str] | None:
        """Which broker symbols are tradable right now, or None if the
        check itself failed ("couldn't check", not "nothing is active")."""
        try:
            await client.wait_for_assets()
            assets = await client.active_assets()
        except Exception:
            logger.debug("active_assets() pre-check failed; skipping it for this request", exc_info=True)
            return None
        return {
            item.get("symbol")
            for item in assets
            if isinstance(item, dict) and item.get("is_active") and item.get("symbol")
        }

    async def list_available_pairs(self) -> list[CurrencyPair]:
        """All currency pairs Pocket Option currently reports as active —
        regular AND OTC. Cached; falls back to REGULAR_PAIRS if the call
        fails or reports nothing."""
        now = time.monotonic()
        if (
            self._pair_list_cache is not None
            and now - self._pair_list_cache_at < _PAIR_LIST_CACHE_SECONDS
        ):
            return self._pair_list_cache

        client = await self._get_client()
        try:
            await client.wait_for_assets()
            assets = await client.active_assets()
        except Exception:
            logger.debug(
                "active_assets() failed while listing pairs; falling back to REGULAR_PAIRS",
                exc_info=True,
            )
            return list(REGULAR_PAIRS)

        pairs = _pairs_from_active_assets(assets)
        if not pairs:
            return list(REGULAR_PAIRS)

        self._pair_list_cache = pairs
        self._pair_list_cache_at = now
        return pairs

    @staticmethod
    def _inactive_asset_message(pair: CurrencyPair, active_symbols: set[str]) -> str:
        active_pairs = sorted(
            (CurrencyPair(sym) for sym in active_symbols if _looks_like_currency_pair(sym)),
            key=lambda p: (p.is_otc, p.label),
        )
        if active_pairs:
            shown = [p.label for p in active_pairs[:15]]
            more = f" и ещё {len(active_pairs) - 15}" if len(active_pairs) > 15 else ""
            suggestion = "Сейчас торгуются: " + ", ".join(shown) + more + "."
        else:
            suggestion = (
                "Ни одна валютная пара сейчас не торгуется на Pocket Option "
                "вообще — попробуйте ещё раз чуть позже."
            )
        return (
            f"{pair.label} сейчас недоступна для торговли на Pocket Option "
            f"(актив {pair.pocket_option_symbol!r} не входит в список активных "
            f"от Pocket Option). {suggestion}"
        )

    async def aclose(self) -> None:
        for task in list(self._warmups):
            task.cancel()
        if self._client is not None:
            for feed in list(self._feeds.values()):
                await feed.stop(self._client)
        self._feeds.clear()
        if self._owns_client and self._client is not None:
            shutdown = getattr(self._client, "shutdown", None)
            if shutdown is not None:
                await shutdown()
