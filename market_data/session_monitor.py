"""Background health monitor for the Pocket Option connection.

Why this exists: the underlying WebSocket connection already reconnects on
its own for ordinary network blips (see `PocketOptionProvider`/the library's
own `reconnect_time` config) — that's not what this solves. What it solves
is the case this project's SSID can't avoid: Pocket Option is unofficial and
has no refresh-token flow, so the credential itself (not just the socket)
eventually stops working and *nothing* can silently fix that — a human has
to get a fresh one (see README, "Как получить SSID"). This monitor's only
job is to notice that has happened and say so loudly, instead of the bot
quietly failing every user's next "Получить сигнал" press with no warning.

It deliberately does NOT alert on a single failed probe — an unofficial feed
has occasional blips that recover on their own, and alerting on every one of
those would train the admin to ignore the alerts. It only alerts after
several consecutive failures (a real, sustained outage), and only once per
outage — then again once when it recovers, so the admin isn't left wondering
whether it's still down.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Protocol

from market_data.models import MarketDataError

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL_SECONDS = 300.0  # 5 minutes
DEFAULT_FAILURE_THRESHOLD = 3  # consecutive failed probes before alerting


class SupportsSendMessage(Protocol):
    """The one aiogram.Bot method this module needs — kept narrow so tests
    can inject a fake without pulling in real Telegram networking."""

    async def send_message(self, chat_id: int, text: str) -> object: ...


class SessionMonitor:
    """Tracks consecutive probe failures and raises exactly one alert per
    sustained outage (plus one recovery notice), instead of one per failure.
    """

    def __init__(
        self,
        provider,
        *,
        bot: SupportsSendMessage | None = None,
        admin_chat_id: int | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self._provider = provider
        self._bot = bot
        self._admin_chat_id = admin_chat_id
        self._failure_threshold = failure_threshold
        self.consecutive_failures = 0
        self.alerted = False
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None

    async def check_once(self) -> bool:
        """Runs one lightweight real probe. Returns True on success.

        Delegates to `provider.verify_connection()` — pair-agnostic (uses
        Pocket Option's own active_assets(), not a candle fetch for one
        specific pair), so a failure here is genuinely about the
        connection/session, never about one particular asset's market being
        closed right now (weekends, etc.) — see market_data/pocket_option.py
        for why that distinction matters and used to cause false alarms.
        """
        # require_quotes: Pocket Option keeps sending the asset list even
        # after the SSID stops receiving quotes, so "assets arrived" alone
        # can't tell a dead session from a live one.
        try:
            await self._provider.verify_connection(require_quotes=True)
        except MarketDataError as exc:
            await self._on_failure(exc)
            return False
        else:
            await self._on_success()
            return True

    async def _on_success(self) -> None:
        self.last_success_at = datetime.now(timezone.utc)
        self.last_error = None
        if self.alerted:
            logger.info(
                "Pocket Option connection recovered after %d consecutive failed probe(s)",
                self.consecutive_failures,
            )
            await self._notify_admin(
                "✅ Соединение с Pocket Option восстановилось — сигналы снова доступны."
            )
        self.consecutive_failures = 0
        self.alerted = False

    async def _on_failure(self, exc: MarketDataError) -> None:
        self.consecutive_failures += 1
        self.last_error = str(exc)
        logger.warning(
            "Pocket Option probe failed (%d in a row): %s", self.consecutive_failures, exc
        )
        if self.consecutive_failures >= self._failure_threshold and not self.alerted:
            since = (
                f" Последний успешный запрос: {self.last_success_at:%Y-%m-%d %H:%M:%S} UTC."
                if self.last_success_at is not None
                else " Соединение не работало с самого запуска бота."
            )
            await self._notify_admin(
                "⚠️ Pocket Option, похоже, отключился по-настоящему: "
                f"{self.consecutive_failures} проверок подряд не прошли.{since}\n\n"
                "Вероятная причина — сессия (SSID) истекла (это неизбежно "
                "время от времени для неофициального подключения, у Pocket "
                "Option нет токена для автоматического обновления). "
                "Обновите POCKET_OPTION_SSID в .env (см. README, раздел «Как "
                "получить SSID») и перезапустите бота.\n\n"
                f"Последняя ошибка: {exc}"
            )
            self.alerted = True

    async def _notify_admin(self, text: str) -> None:
        if self._bot is None or self._admin_chat_id is None:
            return
        try:
            await self._bot.send_message(self._admin_chat_id, text)
        except Exception:
            logger.exception("Failed to send Pocket Option session alert to admin chat")


async def run_session_monitor_loop(
    monitor: SessionMonitor,
    interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
) -> None:
    """Long-lived background loop — same shape as storage.resolver's loop.

    Meant to be started with `asyncio.create_task` alongside
    `dispatcher.start_polling()` in main.py and cancelled on shutdown.
    """
    while True:
        try:
            await monitor.check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in Pocket Option session monitor")
        await asyncio.sleep(interval_seconds)
