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

    async def inspect(pair, exp, *, manual=False):
        return replace(candidate_factory(exp, clock(), str(pair)), manual=manual)

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
    options = SearchOptions(exp, mode="auto", pair=CurrencyPair("EURUSD"))
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
    await m._attempt(
        (1, 2), SearchOptions(Expiration.MIN_3, mode="auto", pair=c.snapshot.pair), c
    )
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

    async def inspect(pair, exp, *, manual=False):
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

    async def inspect(pair, exp, *, manual=False):
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


@pytest.mark.asyncio
async def test_manual_check_reports_weak_direction_entry_time_and_allows_another_request(
    setup, candidate_factory, monkeypatch
):
    m, p, b, repo, clock = setup
    c = candidate_factory(now=clock())
    weak = replace(c.result, logit=0.01, agreement=1)
    monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *args: weak)
    monkeypatch.setattr(
        "scanner.service.evaluate",
        Mock(side_effect=AssertionError("Automatic filters used for manual report")),
    )

    async def snapshot(pair, timeframes):
        return candidate_factory(now=clock(), pair=str(pair)).snapshot

    p.get_snapshot = snapshot
    del m.inspect
    m.cooldowns[((1, 2), "EURUSD", 180, "CALL")] = clock() + 1000
    m.progress[(1, 2)] = (7, "active_markup", "idle_markup")
    m.last_progress[(1, 2)] = 0
    b.edit_message_text = AsyncMock()
    for _ in range(2):
        await m._run(
            (1, 2), SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD")), True
        )
    messages = [call.args[1] for call in b.send_message.call_args_list]
    assert len(messages) == 4
    assert all("ВВЕРХ" in text and "Вход:" in text for text in messages)
    assert all("Перевес модели слабый" in text for text in messages)
    assert all("Качество отбора" not in text for text in messages)
    assert b.edit_message_text.call_args.kwargs["reply_markup"] == "idle_markup"
    assert repo.save_signal.await_count == 2
    assert repo.save_signal.call_args.args[0].model_version == "manual-v1"


@pytest.mark.asyncio
async def test_data_wait_is_visible_and_not_described_as_a_market_entry(setup):
    from market_data.models import MarketDataError

    m, p, b, repo, clock = setup
    m.inspect.side_effect = MarketDataError("1M: история догружается (0/120 свечей)")
    m.progress[(1, 2)] = (7, "active", "idle")
    m.last_progress[(1, 2)] = 0
    b.edit_message_text = AsyncMock()
    await m._run(
        (1, 2), SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD")), True
    )
    text = b.edit_message_text.call_args.kwargs["text"]
    assert "Данные ещё не готовы" in text and "0/120" in text
    assert "Ожидаю подходящий вход" not in text
    repo.save_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_auto_pairs_complete_a_round_without_ninety_second_holds(setup):
    from analysis.entry import EntryRejected

    m, p, _, repo, clock = setup
    pairs = [
        CurrencyPair(s)
        for s in ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDCAD_otc", "GBPJPY_otc")
    ]
    p.list_active_pairs.return_value = pairs
    calls = []
    started = clock()

    async def inspect(pair, exp, *, manual=False):
        calls.append(pair)
        raise EntryRejected("Перевес модели слабый")

    async def pause(seconds):
        await clock.sleep(seconds)
        if set(calls) == set(pairs):
            raise asyncio.CancelledError

    m.inspect.side_effect = inspect
    m.sleep = pause
    with pytest.raises(asyncio.CancelledError):
        await m._run((1, 2), SearchOptions(Expiration.MIN_3, "auto"), False)
    assert calls == pairs
    assert clock() - started < 30
    assert m.coverage[(1, 2)] == (1, 6, 6)
    repo.save_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_auto_group_has_bounded_warmup_before_trying_other_pairs(
    setup,
):
    from analysis.entry import EntryRejected
    from market_data.models import MarketDataError

    m, p, _, _, clock = setup
    pairs = [CurrencyPair(s) for s in ("EURUSD", "GBPUSD", "USDJPY", "USDCAD")]
    p.list_active_pairs.return_value = pairs
    arrived = {}
    started = clock()

    async def inspect(pair, exp, *, manual=False):
        arrived.setdefault(pair, clock() - started)
        if pair in pairs[:2]:
            raise MarketDataError("история недоступна")
        raise EntryRejected("Перевес модели слабый")

    async def pause(seconds):
        await clock.sleep(seconds)
        if set(arrived) == set(pairs):
            raise asyncio.CancelledError

    m.inspect.side_effect = inspect
    m.sleep = pause
    with pytest.raises(asyncio.CancelledError):
        await m._run((1, 2), SearchOptions(Expiration.MIN_3, "auto"), False)
    assert arrived[pairs[2]] <= m.policy.warmup_seconds + m.policy.interval


@pytest.mark.asyncio
async def test_progress_keeps_updating_during_slow_data_fetch_and_stops_with_job(
    setup, monkeypatch
):
    m, _, b, repo, _ = setup
    monkeypatch.setattr("scanner.service.PROGRESS_INTERVAL_SECONDS", 0.01)
    waiting, updated = asyncio.Event(), asyncio.Event()
    calls = []

    async def edit(**kwargs):
        calls.append(kwargs["text"])
        if len(calls) >= 2:
            updated.set()

    async def inspect(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()

    b.edit_message_text = AsyncMock(side_effect=edit)
    m.inspect.side_effect = inspect
    await m.start(
        1,
        2,
        SearchOptions(Expiration.MIN_3, pair=CurrencyPair("EURUSD")),
        once=True,
        progress=(7, "active", "idle"),
    )
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.wait_for(updated.wait(), 1)
        assert all("Загружаю" in text for text in calls)
    finally:
        await m.stop(1, 2)
    assert not any(
        t.get_name().startswith("search-progress:") for t in asyncio.all_tasks()
    )
    repo.save_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_confirmation_uses_updated_direction_without_market_selection_veto(
    setup, candidate_factory
):
    m, p, b, repo, clock = setup
    c = candidate_factory(now=clock())
    fresh = candidate_factory(now=clock(), direction="PUT")
    fresh = replace(
        fresh,
        manual=True,
        snapshot=replace(
            fresh.snapshot, price=c.snapshot.price + c.result.primary.indicators.atr * 3
        ),
    )

    async def inspect(*args, **kwargs):
        return replace(
            fresh,
            snapshot=replace(
                fresh.snapshot, captured_at=clock(), tick_time=clock() - 0.1
            ),
        )

    m.inspect.side_effect = inspect
    p.price_at.return_value = fresh.snapshot.price
    await m._attempt((1, 2), SearchOptions(Expiration.MIN_3, pair=c.snapshot.pair), c)
    record = repo.save_signal.call_args.args[0]
    assert record.final_direction == "PUT" and record.model_version == "manual-v1"
    final = b.send_message.call_args.args[1]
    assert "ВНИЗ" in final and "Направление обновлено" in final
    assert "Вход:" in final
