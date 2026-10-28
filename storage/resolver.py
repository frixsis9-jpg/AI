"""Determines real trade outcomes for signals whose expiration has passed.

Never simulates or guesses a result — it always fetches a real, current
Pocket Option price via the same `MarketDataProvider` the rest of the bot
uses, and if that fetch fails the outcome is honestly recorded as UNKNOWN
rather than invented.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from domain import CurrencyPair, Timeframe
from market_data.models import MarketDataError
from market_data.provider import MarketDataProvider
from storage.repository import SignalRepository

logger = logging.getLogger(__name__)


async def resolve_pending_signals(
    repo: SignalRepository,
    provider: MarketDataProvider,
    now: datetime | None = None,
) -> int:
    """Resolves every expired, not-yet-resolved CALL/PUT signal.

    Returns how many signals were resolved (WIN, LOSS or UNKNOWN — all three
    count as "resolved", since UNKNOWN is an honest outcome, not a retry state).
    """
    now = now or datetime.now(timezone.utc)
    pending = await repo.get_pending_signals(now)
    if not pending:
        return 0

    # Fallback price (latest candle close) is fetched at most once per pair.
    price_cache: dict[CurrencyPair, float | None] = {}
    resolved_at = datetime.now(timezone.utc)
    resolved = 0

    for signal in pending:
        late_by = (now - signal.expires_at).total_seconds()

        # Preferred: the exact tick price at the expiration instant, from
        # the live feed's tick history (market_data/live_feed.py) — this is
        # how the broker itself settles the option.
        price_at = getattr(provider, "price_at", None)
        if price_at is not None:
            passed = provider.feed_has_passed(signal.pair, signal.expires_at)
            if passed is False and late_by < _TICK_WAIT_SECONDS:
                continue  # no tick after expiry yet — try again next round
            price = await price_at(signal.pair, signal.expires_at)
            if price is not None:
                outcome = _determine_outcome(signal.final_direction, signal.entry_price, price)
                await repo.mark_resolved(signal.id, outcome, price, resolved_at)
                resolved += 1
                continue

        # Fallback: current price, but only if we're still close enough to
        # the expiration for it to mean the same thing; otherwise record an
        # honest UNKNOWN rather than a result measured at the wrong time.
        if late_by > _max_fallback_lateness(signal.expiration.value):
            await repo.mark_resolved(signal.id, "UNKNOWN", None, resolved_at)
            resolved += 1
            continue
        if signal.pair not in price_cache:
            price_cache[signal.pair] = await _fetch_latest_price(provider, signal.pair)
        price = price_cache[signal.pair]
        if price is None:
            await repo.mark_resolved(signal.id, "UNKNOWN", None, resolved_at)
        else:
            outcome = _determine_outcome(signal.final_direction, signal.entry_price, price)
            await repo.mark_resolved(signal.id, outcome, price, resolved_at)
        resolved += 1

    return resolved


# How long to wait for the first tick after an expiration before falling back.
_TICK_WAIT_SECONDS = 20.0


def _max_fallback_lateness(expiration_seconds: int) -> float:
    """A late price is only acceptable if the delay is small relative to
    the trade itself: 5 s on a 1-minute option, 30 s on a 1-hour one."""
    return max(5.0, min(30.0, expiration_seconds * 0.05))


async def _fetch_latest_price(provider: MarketDataProvider, pair: CurrencyPair) -> float | None:
    try:
        candles = await provider.get_candles(pair, Timeframe.M1, output_size=1)
    except MarketDataError:
        logger.warning("Could not fetch a resolution price for %s", pair.label)
        return None
    if not candles:
        return None
    return candles[-1].close


def _determine_outcome(direction: str, entry_price: float, exit_price: float) -> str:
    if exit_price == entry_price:
        return "UNKNOWN"
    if direction == "CALL":
        return "WIN" if exit_price > entry_price else "LOSS"
    if direction == "PUT":
        return "WIN" if exit_price < entry_price else "LOSS"
    return "UNKNOWN"  # defensive: WAIT signals are never pending in the first place


async def run_resolver_loop(
    repo: SignalRepository,
    provider: MarketDataProvider,
    interval_seconds: float = 15.0,
) -> None:
    """Long-lived background loop: periodically resolves expired signals.

    Meant to be started with `asyncio.create_task` alongside
    `dispatcher.start_polling()` in main.py, and cancelled on shutdown — a
    `CancelledError` propagates out of the `await asyncio.sleep(...)` and lets
    the loop exit cleanly rather than being swallowed.
    """
    while True:
        try:
            resolved = await resolve_pending_signals(repo, provider)
            if resolved:
                logger.info("Resolved %d expired signal(s)", resolved)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error while resolving pending signals")
        await asyncio.sleep(interval_seconds)
