import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from domain import CurrencyPair, Expiration
from scanner.service import SearchManager, SearchOptions


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def setup(candidate_factory):
    clock = Clock()
    provider = Mock(max_feeds=2)
    provider.pin.return_value = "lease"
    provider.price_at = AsyncMock(return_value=1.1001)
    provider.list_active_pairs = AsyncMock(
        return_value=[
            CurrencyPair(p)
            for p in [
                "EURUSD",
                "GBPUSD",
                "EURUSD_otc",
                "AUDCAD_otc",
                "XAUUSD_otc",
                "CHFNOK",
            ]
        ]
    )
    bot = Mock()
    bot.send_message = AsyncMock()
    repo = Mock()
    repo.log_search = AsyncMock()
    repo.save_signal = AsyncMock(return_value=7)
    manager = SearchManager(provider, bot, repo, clock=clock, sleep=clock.sleep)

    async def inspect(pair, exp):
        return candidate_factory(exp, clock(), str(pair))

    manager.inspect = AsyncMock(side_effect=inspect)
    return manager, provider, bot, repo, clock


@pytest.mark.asyncio
async def test_active_allowlist_market_watchlist_and_fixed_expiry(setup):
    m, p, _, _, _ = setup
    options = SearchOptions(Expiration.SEC_10, "auto", market="otc")
    assert await m._pairs(options) == ["EURUSD_otc", "AUDCAD_otc"]
    assert await m._pairs(replace(options, watchlist=("AUDCAD_otc",))) == ["AUDCAD_otc"]
    assert await m._pairs(replace(options, market="regular")) == ["EURUSD", "GBPUSD"]
    assert await m._pairs(
        SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD"))
    ) == ["EURUSD"]
    assert (
        await m._pairs(SearchOptions(Expiration.MIN_3, pair=CurrencyPair("USDJPY")))
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [5, 10, 15, 30, 60, 180, 300, 3600])
async def test_scheduled_entry_rechecks_and_records_target_not_notice_time(
    setup, candidate_factory, seconds
):
    m, p, b, repo, clock = setup
    exp = Expiration(seconds)
    options = SearchOptions(exp, pair=CurrencyPair("EURUSD"))
    c = candidate_factory(exp, clock())
    notice_time = clock()
    await m._attempt((1, 2), options, c)
    repo.save_signal.assert_awaited_once()
    record = repo.save_signal.call_args.args[0]
    assert record.created_at.timestamp() >= notice_time + 20
    assert (record.expires_at - record.created_at).total_seconds() == seconds
    assert record.confidence is None
    assert record.model_version == "entry-v1"
    assert len(b.send_message.call_args_list) == 2
    p.release.assert_not_called()  # retained until the expiry lookup
    m.inspect.assert_awaited_once()
    assert p.price_at.call_args.kwargs["max_age"] <= 5


@pytest.mark.asyncio
async def test_changed_direction_cancels_and_releases_lease(setup, candidate_factory):
    m, p, b, repo, clock = setup
    c = candidate_factory(now=clock())
    m.inspect.side_effect = None
    m.inspect.return_value = candidate_factory(now=clock(), direction="PUT")
    await m._attempt((1, 2), SearchOptions(Expiration.MIN_3, pair=c.snapshot.pair), c)
    repo.save_signal.assert_not_awaited()
    p.release.assert_called_once_with("lease")
    assert "отменён" in b.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_late_notification_never_records_a_trade(setup, candidate_factory):
    m, p, _, repo, clock = setup
    m._notify = AsyncMock(return_value=3.0)
    await m._attempt(
        (1, 2),
        SearchOptions(Expiration.SEC_5, pair=CurrencyPair("EURUSD")),
        candidate_factory(Expiration.SEC_5, clock()),
    )
    repo.save_signal.assert_not_awaited()
    m.inspect.assert_not_awaited()
    p.release.assert_called_once()


@pytest.mark.asyncio
async def test_missing_tick_at_target_cancels_instead_of_using_later_price(
    setup, candidate_factory
):
    m, p, _, repo, clock = setup
    p.price_at.return_value = None
    await m._attempt(
        (1, 2),
        SearchOptions(Expiration.SEC_5, pair=CurrencyPair("EURUSD")),
        candidate_factory(Expiration.SEC_5, clock()),
    )
    repo.save_signal.assert_not_awaited()
    p.release.assert_called_once()


@pytest.mark.asyncio
async def test_same_user_replacement_and_shutdown_leave_no_duplicate_jobs(setup):
    m, p, _, repo, clock = setup
    entered = asyncio.Event()
    cancelled = []

    async def block(key, options, once):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(key)

    m._run = block
    options = SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD"))
    await m.start(1, 2, options)
    await entered.wait()
    first = m.tasks[(1, 2)]
    entered.clear()
    await m.start(1, 2, replace(options, mode="auto"))
    await entered.wait()
    assert first.cancelled()
    assert len(m.tasks) == 1 and m.active(1, 2)
    await m.close()
    assert not m.active(1, 2) and len(cancelled) == 2


@pytest.mark.asyncio
async def test_one_scan_uses_same_evaluator_ranks_candidates_and_suppresses_repeats(
    setup, candidate_factory
):
    m, p, b, repo, clock = setup

    async def inspect(pair, exp):
        return candidate_factory(
            exp, clock(), str(pair), quality=90 if pair == "GBPUSD" else 70
        )

    m.inspect.side_effect = inspect
    options = SearchOptions(Expiration.MIN_3, "auto", market="regular")
    await m._run((1, 2), options, True)
    assert repo.save_signal.call_args.args[0].pair == "GBPUSD"
    # Cooldown both currently observed candidates; a new scan must wait.
    for pair in ("EURUSD", "GBPUSD"):
        m.cooldowns[((1, 2), pair, 180, "CALL")] = clock() + 100
    repo.save_signal.reset_mock()
    await m._run((1, 2), options, True)
    repo.save_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_pending_plan_delivers_cancellation_and_releases_feed(
    setup, candidate_factory
):
    m, p, b, repo, clock = setup
    waiting = asyncio.Event()

    async def hold(delay):
        waiting.set()
        await asyncio.Event().wait()

    m.sleep = hold
    options = SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD"))
    await m.start(1, 2, options)
    await asyncio.wait_for(waiting.wait(), 2)
    await m.stop(1, 2)
    repo.save_signal.assert_not_awaited()
    assert any("отменён" in c.args[1] for c in b.send_message.call_args_list)
    p.release.assert_called_once()


@pytest.mark.asyncio
async def test_comparing_pairs_refreshes_winner_before_sending_plan(
    setup, candidate_factory
):
    m, p, b, repo, clock = setup
    calls = []

    async def inspect(pair, exp):
        calls.append(str(pair))
        if len(calls) == 2:
            clock.now += 10
        return candidate_factory(
            exp, clock(), str(pair), quality=90 if pair == "EURUSD" else 70
        )

    m.inspect.side_effect = inspect
    m._attempt = AsyncMock()
    await m._run(
        (1, 2), SearchOptions(Expiration.MIN_3, "auto", market="regular"), True
    )
    assert calls == ["EURUSD", "GBPUSD", "EURUSD"]
    assert m._attempt.call_args.args[2].snapshot.captured_at == clock()


@pytest.mark.asyncio
async def test_real_async_worker_rechecks_snapshot_age_after_computation(
    setup, candidate_factory, monkeypatch
):
    m, p, b, repo, clock = setup
    c = candidate_factory(now=clock())
    p.get_snapshot = AsyncMock(return_value=c.snapshot)
    del m.inspect  # use the production implementation rather than the fixture stub
    monkeypatch.setattr("scanner.service.evaluate", lambda *args: c)
    result = await m.inspect(c.snapshot.pair, c.result.expiration)
    assert result.direction == c.direction

    def delayed(*args):
        clock.now += 10
        return c

    monkeypatch.setattr("scanner.service.evaluate", delayed)
    with pytest.raises(Exception, match="устарела"):
        await m.inspect(c.snapshot.pair, c.result.expiration)


@pytest.mark.asyncio
async def test_notification_failure_after_preliminary_cancels_plan(
    setup, candidate_factory
):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    m, p, b, repo, clock = setup
    b.send_message.side_effect = [
        Mock(),
        TelegramNetworkError(
            method=SendMessage(chat_id=1, text="test"), message="offline"
        ),
        Mock(),
    ]
    options = SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD"))
    await m._attempt((1, 2), options, candidate_factory(now=clock()))
    repo.save_signal.assert_not_awaited()
    p.release.assert_called_once()
    assert "отменён" in m.status(1, 2)
    assert "⛔" in b.send_message.call_args.args[1]


def test_message_keeps_existing_factor_explanations_without_claiming_probability(
    setup, candidate_factory
):
    from analysis.multi_timeframe import FactorNote
    from domain import Timeframe

    m, *_ = setup
    c = candidate_factory()
    result = replace(c.result, pros=[FactorNote(Timeframe.M1, "test explanation", 0.1)])
    text = m._summary(replace(c, result=result))
    assert "test explanation" in text
    assert "не вероятность выигрыша" in text
    assert "62%" not in text
