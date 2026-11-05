"""One cancellable search per (chat,user), bounded feeds and conditional entry.

Only explicit Telegram Start/Check actions create jobs. No real orders are
placed. Every confirmed record is a model entry, not the user's broker fill.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from analysis.entry import (
    MANUAL_VERSION,
    VERSION,
    EntryRejected,
    age_limit,
    evaluate,
    evaluate_manual,
    quote_age_limit,
)
from analysis.errors import AnalysisError
from analysis.multi_timeframe import timeframe_trio_for
from domain import REGULAR_PAIRS, CurrencyPair, Expiration
from market_data.models import MarketDataError
from storage.models import SignalRecord

logger = logging.getLogger(__name__)
UTC = timezone.utc
ALLOWLIST = tuple(str(p) for p in REGULAR_PAIRS) + (
    "AUDCAD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
)
MARKETS = {"regular": "Обычные пары", "otc": "Только OTC", "both": "Обычные + OTC"}
PROGRESS_INTERVAL_SECONDS = 5.0
SNAPSHOT_TIMEOUT_SECONDS = 15.0
FRESH_TICK_WAIT_SECONDS = 1.0


@dataclass(frozen=True)
class SearchOptions:
    expiration: Expiration
    mode: str = "manual"
    pair: CurrencyPair | None = None
    market: str = "both"
    watchlist: tuple[str, ...] = ()  # empty means all active allowlisted pairs

    def validate(self):
        if self.expiration not in Expiration.selectable():
            raise ValueError("Выберите экспирацию в меню")
        if self.mode not in ("manual", "auto") or self.market not in MARKETS:
            raise ValueError("Некорректный режим поиска")
        if self.mode == "manual" and not self.pair:
            raise ValueError("Сначала выберите валютную пару")


@dataclass(frozen=True)
class SearchPolicy:
    interval: float = 5.0
    warmup_seconds: float = 20.0
    lead_seconds: float = 20.0
    confirm_before: float = 3.0
    cooldown_seconds: float = 60.0
    max_drift_atr: float = 0.5
    timezone: str = "Asia/Bishkek"


@dataclass
class SearchPanel:
    message_id: int
    active_markup: object = None
    idle_markup: object = None
    body: str = ""
    active: bool = True
    restored: bool = False
    rendered: tuple | None = None
    options: SearchOptions | None = None
    entry_time: float | None = None
    final_issued: bool = False


class SearchManager:
    def __init__(
        self, provider, bot, repo, policy=None, *, clock=time.time, sleep=asyncio.sleep
    ):
        self.provider, self.bot, self.repo = provider, bot, repo
        self.policy = policy or SearchPolicy()
        self.tz = ZoneInfo(self.policy.timezone)
        self.clock, self.sleep = clock, sleep
        self.tasks = {}
        self.locks = {}
        self.statuses = {}
        self.cooldowns = {}
        self.progress = {}
        self.panel_locks = {}
        self.last_progress = {}
        self.checks = {}
        self.coverage = {}
        self.analysis_lock = asyncio.Lock()
        self.loading = {}

    def status(self, chat, user):
        return self.statuses.get((chat, user), "Поиск остановлен")

    def active(self, chat, user):
        t = self.tasks.get((chat, user))
        return t is not None and not t.done()

    async def stop(self, chat, user):
        async with self.locks.setdefault((chat, user), asyncio.Lock()):
            await self._stop((chat, user))

    async def _stop(self, key):
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.statuses[key] = "Поиск остановлен"
        self.progress.pop(key, None)
        self.loading.pop(key, None)

    async def start(self, chat, user, options, *, once=False, progress=None):
        options.validate()
        key = (chat, user)
        async with self.locks.setdefault(key, asyncio.Lock()):
            await self._stop(key)
            self.progress[key] = (
                SearchPanel(*progress, options=options) if progress else None
            )
            self.last_progress[key] = 0
            self.checks[key] = 0
            self.coverage[key] = (0, 0, 0)
            self.statuses[key] = "Загружаю данные активных пар"
            self.tasks[key] = asyncio.create_task(
                self._run(key, options, once), name=f"search:{chat}:{user}"
            )

    async def close(self):
        for key in list(self.tasks):
            await self.stop(*key)

    def _panel_text(self, key, panel):
        options = panel.options
        status = self.statuses.get(key, "")
        loading = self.loading.get(key)
        if loading and status.startswith("⏳"):
            pair, started = loading
            detail = self._loading_detail(pair, options.expiration)
            status += f"\nЗагрузка: {time.monotonic() - started:.0f} с."
            if detail:
                status += "\n" + detail
        round_number, visited, total = self.coverage.get(key, (0, 0, 0))
        coverage = (
            f"\nОбход №{round_number}: {visited} из {total} пар."
            if options and options.mode == "auto" and total
            else ""
        )
        if panel.body:
            text = panel.body
            if (
                options
                and options.mode == "auto"
                and text.startswith("✅")
                and not status.startswith("Подготовка")
            ):
                text = "Последний сигнал\n\n" + text
            if status and status not in text and not status.startswith("Подготовка"):
                text += "\n\n" + status
            text += coverage
        else:
            mode = "Ручной сигнал" if options.mode == "manual" else "Автопоиск"
            pair = (
                f" · {options.pair.label}"
                if options.mode == "manual" and options.pair
                else ""
            )
            text = (
                f"{mode}{pair} · {options.expiration.label}\n\n{status}"
                f"\n\nОбновлено: {self._time(self.clock())}. Проверок: {self.checks.get(key, 0)}.{coverage}"
            )
        return text if len(text) <= 4000 else text[:3998] + "\n…"

    async def _edit_panel(self, key, panel):
        """Caller holds the panel lock; render the newest state after locking."""
        text = self._panel_text(key, panel)
        rendered = (text, panel.active)
        if rendered == panel.rendered:
            return
        markup = panel.active_markup if panel.active else panel.idle_markup
        try:
            await self.bot.edit_message_text(
                chat_id=key[0],
                message_id=panel.message_id,
                text=text,
                reply_markup=markup,
                parse_mode=None,
            )
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                pass
            elif not panel.restored and (
                "message to edit not found" in reason
                or "message can't be edited" in reason
            ):
                message = await self.bot.send_message(
                    key[0], text, reply_markup=markup, parse_mode=None
                )
                panel.message_id = message.message_id
                panel.restored = True
            else:
                raise
        panel.rendered = rendered

    async def _show_progress(
        self, key, options, *, active=None, force=False, expected=None
    ):
        panel = self.progress.get(key)
        if (
            not panel
            or (expected is not None and panel is not expected)
            or (
                not force
                and self.clock() - self.last_progress.get(key, 0)
                < PROGRESS_INTERVAL_SECONDS
            )
        ):
            return
        self.last_progress[key] = self.clock()
        try:
            async with self.panel_locks.setdefault(key, asyncio.Lock()):
                if self.progress.get(key) is not panel:
                    return  # the user has already opened another card
                panel.options = options
                if active is not None:
                    panel.active = active
                await asyncio.wait_for(self._edit_panel(key, panel), timeout=2)
        except (TimeoutError, TelegramAPIError):
            logger.debug("Progress message could not be edited")

    async def _heartbeat(self, key, options):
        panel = self.progress.get(key)
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            if self.progress.get(key) is not panel:
                return
            await self._show_progress(key, options, force=True, expected=panel)

    async def _notify(self, key, text, *, active=None):
        # One card for loading, analysis, confirmation and cancellation. Include
        # lock/API time in the delivery budget used by the entry checks.
        start = time.monotonic()
        async with self.panel_locks.setdefault(key, asyncio.Lock()):
            panel = self.progress.get(key)
            if panel is None:
                panel = SearchPanel(
                    0, body=text, active=active if active is not None else True
                )
                message = await asyncio.wait_for(
                    self.bot.send_message(
                        key[0], self._panel_text(key, panel), parse_mode=None
                    ),
                    timeout=10,
                )
                panel.message_id = message.message_id
                panel.rendered = (self._panel_text(key, panel), panel.active)
                self.progress[key] = panel
            else:
                panel.body = text
                if active is not None:
                    panel.active = active
                await asyncio.wait_for(self._edit_panel(key, panel), timeout=10)
        return time.monotonic() - start

    async def _event(self, key, event, options, candidate=None, **details):
        await self.repo.log_search(
            *key,
            event,
            pair=candidate.snapshot.pair if candidate else options.pair,
            expiration=options.expiration.value,
            **details,
        )

    def _loading_detail(self, pair, expiration):
        describe = getattr(self.provider, "loading_status", None)
        detail = (
            describe(pair, timeframe_trio_for(expiration)) if callable(describe) else ""
        )
        return detail if isinstance(detail, str) else ""

    async def inspect(self, pair, expiration, *, manual=False):
        loading_started = time.perf_counter()
        deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS

        async def read_snapshot():
            return await asyncio.wait_for(
                self.provider.get_snapshot(pair, timeframe_trio_for(expiration)),
                timeout=max(0.001, deadline - time.monotonic()),
            )

        try:
            snapshot = await read_snapshot()
            refresh_until = min(deadline, time.monotonic() + FRESH_TICK_WAIT_SECONDS)
            while (
                snapshot.age > age_limit(expiration)
                and time.monotonic() < refresh_until
            ):
                await asyncio.sleep(0.05)
                snapshot = await read_snapshot()
        except TimeoutError as exc:
            detail = self._loading_detail(pair, expiration)
            raise MarketDataError(
                f"Не удалось загрузить данные {pair.label} за {time.perf_counter() - loading_started:.1f} с. {detail}"
            ) from exc
        loaded_ms = (time.perf_counter() - loading_started) * 1000
        # History for one asset must not hold the CPU lock for every other user.
        async with self.analysis_lock:
            start = time.perf_counter()
            evaluator = evaluate_manual if manual else evaluate
            candidate = await asyncio.to_thread(evaluator, snapshot, expiration)
            # Recheck age AFTER computation and waiting for the worker thread.
            if snapshot.age + max(
                0, self.clock() - snapshot.captured_at
            ) > quote_age_limit(expiration):
                raise MarketDataError("Котировка устарела во время расчёта")
            return replace(
                candidate,
                calculated_ms=(time.perf_counter() - start) * 1000,
                loaded_ms=loaded_ms,
            )

    async def _pairs(self, options):
        active = await self.provider.list_active_pairs()
        if options.mode == "manual":
            return [options.pair] if options.pair in active else []
        selected = set(options.watchlist)
        return [
            p
            for p in active
            if str(p).removesuffix("_otc") in ALLOWLIST
            and (not selected or str(p) in selected)
            and (options.market == "both" or p.is_otc == (options.market == "otc"))
        ]

    async def _run(self, key, options, once):
        pending = None
        cycles = 0
        remaining, group = [], []
        known, visited = set(), set()
        round_number = 0
        group_started = self.clock()
        heartbeat = (
            asyncio.create_task(
                self._heartbeat(key, options), name=f"search-progress:{key}"
            )
            if self.progress.get(key)
            else None
        )
        try:
            await self._event(
                key, "started", options, mode=options.mode, market=options.market
            )
            while True:
                try:
                    pairs = await asyncio.wait_for(self._pairs(options), timeout=30)
                    if not pairs:
                        raise MarketDataError("В выбранном списке нет активных пар")
                    size = max(1, self.provider.max_feeds)
                    active = set(pairs)
                    remaining = [p for p in remaining if p in active]
                    group = [p for p in group if p in active]
                    visited.intersection_update(active)
                    remaining.extend(p for p in pairs if p not in known)
                    known = active
                    if not group:
                        if not remaining:
                            remaining = list(pairs)
                            visited.clear()
                            round_number += 1
                        elif round_number == 0:
                            round_number = 1
                        group, remaining = remaining[:size], remaining[size:]
                        group_started = self.clock()
                    self.coverage[key] = (round_number, len(visited), len(pairs))
                    candidates = []
                    failures = []
                    waiting_for_data = False
                    for pair in group:
                        self.statuses[key] = (
                            f"⏳ Загружаю и проверяю свечи {pair.label}…"
                        )
                        self.loading[key] = (pair, time.monotonic())
                        await self._show_progress(key, options, force=cycles == 0)
                        try:
                            c = await self.inspect(
                                pair,
                                options.expiration,
                                manual=options.mode == "manual",
                            )
                            self.checks[key] = self.checks.get(key, 0) + 1
                            token = (
                                key,
                                str(pair),
                                options.expiration.value,
                                c.direction,
                            )
                            if (
                                options.mode == "manual"
                                or self.clock() >= self.cooldowns.get(token, 0)
                            ):
                                candidates.append(c)
                        except (MarketDataError, AnalysisError, TimeoutError) as exc:
                            self.checks[key] = self.checks.get(key, 0) + 1
                            warming = (
                                not isinstance(exc, EntryRejected)
                                or exc.retry_after > 0
                            )
                            waiting_for_data |= warming
                            prefix = (
                                "Ситуация проверена, входа нет"
                                if not warming
                                else "Данные ещё не готовы"
                            )
                            reason = str(exc) or "источник данных не ответил вовремя"
                            detail = f"{prefix}. {pair.label}: {reason}"
                            failures.append(detail)
                        finally:
                            self.loading.pop(key, None)
                            visited.add(pair)
                            self.coverage[key] = (
                                round_number,
                                len(visited),
                                len(pairs),
                            )
                    if candidates:
                        selected = max(candidates, key=lambda c: c.quality)
                        # Another asset may have needed a slow history request.
                        # Never send a plan from the earlier, now stale snapshot.
                        pending = (
                            selected
                            if options.mode == "manual"
                            else await self.inspect(
                                selected.snapshot.pair, options.expiration
                            )
                        )
                        if (
                            options.mode == "auto"
                            and pending.direction != selected.direction
                        ):
                            pending = None
                            raise MarketDataError(
                                "Направление изменилось при сравнении пар; повторяю поиск"
                            )
                        await self._attempt(key, options, pending)
                        token = (
                            key,
                            str(pending.snapshot.pair),
                            options.expiration.value,
                            pending.direction,
                        )
                        if options.mode == "auto":
                            self.cooldowns[token] = self.clock() + max(
                                self.policy.cooldown_seconds, options.expiration.value
                            )
                        pending = None
                    else:
                        self.statuses[key] = (
                            "\n\n".join(failures)
                            if failures
                            else "Повторный сигнал временно заблокирован"
                        )
                        if once and not self.progress.get(key):
                            await self._notify(key, self.statuses[key])
                    if once and self.statuses[key].startswith("Вход зафиксирован"):
                        self.statuses[key] = (
                            "Анализ завершён. Можно запросить следующий сигнал."
                        )
                    await self._show_progress(key, options, active=not once, force=True)
                    logger.info(
                        "Search scan: round=%s coverage=%s/%s checks=%s status=%s",
                        round_number,
                        len(visited),
                        len(pairs),
                        self.checks.get(key, 0),
                        self.statuses[key],
                    )
                    if options.mode == "auto" and (
                        not waiting_for_data
                        or self.clock() - group_started >= self.policy.warmup_seconds
                    ):
                        group = []
                    cycles += 1
                    if once:
                        return
                except (MarketDataError, TimeoutError) as exc:
                    self.statuses[key] = "Данные не готовы: " + (
                        str(exc) or "источник не ответил вовремя"
                    )
                    await self._show_progress(key, options, active=not once, force=True)
                    if once:
                        if not self.progress.get(key):
                            await self._notify(key, "⏳ " + self.statuses[key])
                        return
                await self.sleep(self.policy.interval)
        except asyncio.CancelledError:
            if pending:
                try:
                    panel = self.progress.get(key)
                    if (
                        panel
                        and panel.entry_time is not None
                        and self.clock() >= panel.entry_time
                    ):
                        self.statuses[key] = (
                            "Поиск остановлен. Ранее выданный сигнал сохранён."
                        )
                        await self._show_progress(
                            key, options, active=False, force=True
                        )
                    else:
                        await asyncio.wait_for(
                            self._notify(
                                key,
                                "⛔ "
                                + pending.snapshot.pair.label
                                + ": запланированный вход отменён — поиск остановлен или настройки изменены",
                                active=False,
                            ),
                            timeout=2,
                        )
                    await self._event(
                        key, "cancelled", options, pending, reason="stopped"
                    )
                except Exception:
                    logger.warning("Could not deliver search cancellation")
            raise
        except Exception:
            logger.exception("Search stopped after an unexpected error")
            self.statuses[key] = (
                "Поиск остановлен из-за ошибки; незавершённый план отменён. Откройте логи Docker"
            )
            try:
                await self._notify(key, self.statuses[key], active=False)
            except Exception:
                logger.warning("Could not deliver search failure notification")
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if self.statuses.get(key, "").startswith("Подготовка"):
                self.statuses[key] = "Поиск остановлен"

    def _time(self, ts):
        return datetime.fromtimestamp(ts, self.tz).strftime("%d.%m %H:%M:%S")

    def _summary(self, c, entry=None, entry_price=None):
        r = c.result
        notes = [f"• [{n.timeframe.value}] {n.text}" for n in r.pros[:2]]
        if r.cons:
            notes.append(f"⚠ [{r.cons[0].timeframe.value}] {r.cons[0].text}")
        return (
            f"💱 {r.pair.label} · {'ВВЕРХ' if c.direction == 'CALL' else 'ВНИЗ'}\n"
            f"⌛ Экспирация: {r.expiration.label}\n"
            + (
                f"🕒 Вход: {self._time(entry)} ({self.policy.timezone})\n"
                if entry is not None
                else ""
            )
            + (
                f"📍 Ориентир входа: {c.snapshot.price:.5f}\n"
                if entry_price is None
                else f"📍 Уровень входа (по потоку): {entry_price:.5f}\n"
            )
            + (
                ""
                if c.manual
                else f"Качество отбора: {c.quality:.1f} балла (не вероятность выигрыша)\n"
            )
            + "\n".join("• " + reason for reason in c.reasons)
            + ("\n" + "\n".join(notes) if notes else "")
            + f"\nЗагрузка данных: {c.loaded_ms / 1000:.1f} с · Расчёт: {c.calculated_ms:.0f} мс"
        )

    async def _cancel(self, key, options, c, reason):
        panel = self.progress.get(key)
        if panel and panel.entry_time is not None and self.clock() >= panel.entry_time:
            self.statuses[key] = (
                (
                    "Сигнал сохранён. Не удалось зафиксировать котировку в момент входа: "
                    if panel.final_issued
                    else "Подтверждение не завершилось до указанного времени входа: "
                )
                + reason
                + ". Результат этой попытки не включён в статистику."
            )
            await self._event(key, "entry_unverified", options, c, reason=reason)
            await self._show_progress(key, options, active=False, force=True)
            return
        self.statuses[key] = "Вход отменён: " + reason
        await self._event(key, "cancelled", options, c, reason=reason)
        try:
            await asyncio.wait_for(
                self._notify(key, "⛔ " + self.statuses[key]), timeout=2
            )
        except (TimeoutError, TelegramAPIError):
            logger.warning(
                "Cancellation could not be delivered; plan remains cancelled"
            )

    async def _attempt(self, key, options, c):
        if options.mode == "manual":
            c = replace(c, manual=True)
        entry = math_ceil(self.clock() + self.policy.lead_seconds)
        lease = self.provider.pin(
            c.snapshot.pair, entry + options.expiration.value + 25
        )
        entered = False
        try:
            entered = await self._execute(key, options, c, entry)
        except (MarketDataError, AnalysisError, TimeoutError, TelegramAPIError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, (MarketDataError, AnalysisError))
                else "ошибка или задержка доставки/котировок"
            )
            await self._cancel(key, options, c, reason)
        finally:
            if not entered:
                self.provider.release(lease)

    async def _execute(self, key, options, c, entry):
        expiration = options.expiration
        panel = self.progress.get(key)
        if panel:
            panel.entry_time, panel.final_issued = entry, False
        self.statuses[key] = "Подготовка входа " + self._time(entry)
        await self._event(
            key, "prepared", options, c, entry_at=entry, quality=c.quality
        )
        rtt = await self._notify(
            key,
            (
                "📊 РУЧНОЙ АНАЛИЗ · предварительно\n"
                if options.mode == "manual"
                else "🟡 ПРЕДВАРИТЕЛЬНО\n"
            )
            + self._summary(c, entry)
            + "\n"
            + (
                "Ожидайте итогового сигнала до указанного времени входа.\n"
                if options.mode == "manual"
                else "Ожидайте подтверждения. При отмене или опоздании вход пропускается.\n"
            )
            + (
                "Секундный режим экспериментальный: задержка Telegram и ручного входа критична."
                if expiration.is_short
                else ""
            ),
        )
        panel = self.progress.get(key)
        if panel:
            panel.entry_time = entry
            panel.final_issued = False
        budget = min(2.0, expiration.value / 10)
        if rtt > budget or self.clock() > entry - self.policy.confirm_before:
            await self._cancel(
                key, options, c, "доставка уведомления заняла слишком долго"
            )
            return
        await self.sleep(max(0, entry - self.policy.confirm_before - self.clock()))
        try:
            fresh = await asyncio.wait_for(
                self.inspect(
                    c.snapshot.pair, expiration, manual=options.mode == "manual"
                ),
                timeout=max(0.001, entry - 1 - self.clock()),
            )
        except (MarketDataError, AnalysisError, TimeoutError) as exc:
            await self._cancel(
                key,
                options,
                c,
                str(exc) or "Не удалось обновить котировку до подтверждения входа",
            )
            return
        atr = c.result.primary.indicators.atr
        if options.mode == "auto" and (
            fresh.direction != c.direction
            or abs(fresh.snapshot.price - c.snapshot.price)
            > atr * self.policy.max_drift_atr
        ):
            await self._cancel(key, options, c, "направление или цена изменились")
            return
        if (
            self.clock() - fresh.snapshot.tick_time > quote_age_limit(expiration)
            or self.clock() > entry - 1
        ):
            await self._cancel(key, options, c, "проверка завершилась слишком поздно")
            return

        def confirmed_text(entry_price=None):
            return (
                (
                    "✅ РУЧНОЙ СИГНАЛ\n"
                    if options.mode == "manual"
                    else "✅ ПЛАН ПОДТВЕРЖДЁН\n"
                )
                + self._summary(fresh, entry, entry_price)
                + (
                    "\nНаправление обновлено после повторного анализа."
                    if options.mode == "manual" and fresh.direction != c.direction
                    else ""
                )
                + f"\nРасчётное завершение: {self._time(entry + expiration.value)}"
                + "\nПри пропуске времени не догоняйте движение."
            )

        rtt = await self._notify(key, confirmed_text())
        panel = self.progress.get(key)
        if panel:
            panel.final_issued = True
        if rtt > budget or self.clock() > entry - 0.5:
            await self._cancel(key, options, c, "подтверждение задержалось")
            return
        await self.sleep(max(0, entry - self.clock()))
        # Capture a bounded-age tick at the scheduled time. Do not fabricate an
        # entry from a later price if the feed or event loop missed the deadline.
        price = None
        while self.clock() <= entry + age_limit(expiration):
            price = await self.provider.price_at(
                c.snapshot.pair,
                datetime.fromtimestamp(entry, UTC),
                max_age=age_limit(expiration),
            )
            if price is not None:
                break
            await self.sleep(0.05)
        if price is None or (
            options.mode == "auto"
            and abs(price - fresh.snapshot.price) > atr * self.policy.max_drift_atr
        ):
            await self._cancel(
                key, options, c, "нет достоверной цены в момент входа или цена ушла"
            )
            return
        record = SignalRecord(
            datetime.fromtimestamp(entry, UTC),
            c.snapshot.pair,
            fresh.result.primary.timeframe,
            expiration,
            fresh.result.primary.confirmation.direction,
            fresh.direction,
            None,
            False,
            price,
            fresh.result.primary.indicators,
            "Модельный вход по подтверждённому плану",
            MANUAL_VERSION if options.mode == "manual" else VERSION,
            {
                "quality": fresh.quality,
                "raw_logit": fresh.result.logit,
                "mode": options.mode,
                "market": "otc" if c.snapshot.pair.is_otc else "regular",
                "tick_age": fresh.snapshot.age,
                "calculated_ms": fresh.calculated_ms,
                "confirmation_api_seconds": rtt,
                "reference_price": fresh.snapshot.price,
                "analysis_quote_max_age": quote_age_limit(expiration),
                "loaded_ms": fresh.loaded_ms,
            },
        )
        signal_id = await self.repo.save_signal(record)
        await self._event(
            key, "entered", options, c, signal_id=signal_id, entry_at=entry, price=price
        )
        panel = self.progress.get(key)
        if panel:
            async with self.panel_locks.setdefault(key, asyncio.Lock()):
                if self.progress.get(key) is panel:
                    panel.body = confirmed_text(price)
        self.statuses[key] = "Вход зафиксирован; продолжаю поиск после паузы"
        return True


def math_ceil(value):
    import math

    return math.ceil(value)
