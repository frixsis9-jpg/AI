"""Telegram bot with persistent menu choices and conditional entry search."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from pydantic import ValidationError

from bot.handlers import selection, signal, start, stats
from market_data.models import MarketDataError
from market_data.pocket_option import PocketOptionProvider
from market_data.session_monitor import SessionMonitor, run_session_monitor_loop
from scanner.service import SearchManager, SearchPolicy
from storage.fsm import SQLiteStorage
from storage.repository import SignalRepository
from storage.resolver import run_resolver_loop

logger = logging.getLogger(__name__)


def _load_settings():
    """Imports config.settings, turning a bad .env (e.g. a malformed
    POCKET_OPTION_SSID — see market_data/ssid.py) into one clear, specific
    message instead of a raw pydantic traceback.
    """
    try:
        from config import settings
    except ValidationError as exc:
        lines = ["❌ Ошибка конфигурации (.env):", ""]
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            message = str(error["msg"]).removeprefix("Value error, ")
            lines.append(f"  {field}: {message}")
        lines.append("")
        lines.append("Исправьте .env и запустите бота заново.")
        print("\n".join(lines), file=sys.stderr)
        raise SystemExit(1) from exc
    return settings


async def main() -> None:
    settings = _load_settings()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # TELEGRAM_PROXY_URL routes ONLY this bot's own Telegram connection
    # through a proxy (e.g. Cloudflare WARP running in Proxy Mode as a
    # background Windows service) — see config.Settings.telegram_proxy_url
    # and README, "Работа без ручного впн". Everything else (Pocket Option,
    # TwelveData) is unaffected. Left unset, aiogram connects directly, same
    # as before.
    session = (
        AiohttpSession(proxy=settings.telegram_proxy_url)
        if settings.telegram_proxy_url
        else None
    )
    bot = Bot(token=settings.telegram_bot_token, session=session)
    if settings.telegram_proxy_url:
        logger.info("Telegram proxy enabled")
    storage = SQLiteStorage(settings.database_path)
    await storage.init()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(start.router)
    dispatcher.include_router(selection.router)
    dispatcher.include_router(signal.router)
    dispatcher.include_router(stats.router)

    provider = PocketOptionProvider(
        ssid=settings.pocket_option_ssid, max_feeds=settings.search_max_feeds
    )

    # One real connection attempt before we ever start polling Telegram: a
    # bad/expired SSID (well-formed but no longer valid — market_data/ssid.py
    # only catches *format* problems) should fail loudly and immediately with
    # a clear message, not silently start the bot and then confuse every user
    # whose first "Получить сигнал" press mysteriously errors out.
    try:
        await provider.verify_connection(require_quotes=True)
    except MarketDataError as exc:
        logger.error("Pocket Option connection check failed: %s", exc)
        print(
            "❌ Не удалось подключиться к Pocket Option с текущим POCKET_OPTION_SSID.\n"
            f"{exc}\n\n"
            "Обновите POCKET_OPTION_SSID в .env (см. README, раздел «Как получить "
            "SSID») и запустите бота заново. Можно также проверить подключение "
            "отдельно: python scripts/check_pocket_option_connection.py",
            file=sys.stderr,
        )
        await provider.aclose()
        await storage.close()
        await bot.session.close()
        raise SystemExit(1) from exc

    repo = SignalRepository(db_path=settings.database_path)
    await repo.init()
    search = SearchManager(
        provider,
        bot,
        repo,
        SearchPolicy(
            interval=settings.search_interval_seconds,
            lead_seconds=settings.search_lead_seconds,
            timezone=settings.search_timezone,
        ),
    )

    # Lets a hidden/background launch (see start_bot.vbs) confirm itself
    # without any visible console: if TELEGRAM_ADMIN_CHAT_ID is set, the
    # user gets this message in Telegram the moment the bot is actually up
    # and polling — the same signal a console window would have given them,
    # just delivered where they'll actually see it. Never fatal: a failure
    # here (e.g. proxy misconfigured) is logged, not raised, since the bot
    # itself may still work fine.
    if settings.telegram_admin_chat_id is not None:
        try:
            await bot.send_message(
                settings.telegram_admin_chat_id,
                "✅ PocketSignalBot запущен и готов к работе.",
            )
        except Exception:
            logger.exception(
                "Could not send startup notification to TELEGRAM_ADMIN_CHAT_ID"
            )

    # Outcomes are looked up by exact tick at the expiration instant, so the
    # interval only controls how soon /stats updates — but the fallback path
    # (no tick history) needs a prompt check, hence the 3-second ceiling.
    resolver_task = asyncio.create_task(
        run_resolver_loop(
            repo,
            provider,
            interval_seconds=min(settings.resolver_interval_seconds, 1.0),
        )
    )

    # Runs alongside the bot for the whole time it's up (see
    # market_data/session_monitor.py for why this exists and why it doesn't
    # alert on every single blip). Works even with telegram_admin_chat_id
    # unset — it just won't have anywhere to send the alert, and only logs.
    if settings.telegram_admin_chat_id is None:
        logger.warning(
            "TELEGRAM_ADMIN_CHAT_ID not set — Pocket Option outages will only be "
            "logged, not sent to you in Telegram. See README, section "
            "'24/7 без присмотра: что делать, когда SSID протухнет'."
        )
    session_monitor = SessionMonitor(
        provider,
        bot=bot,
        admin_chat_id=settings.telegram_admin_chat_id,
        failure_threshold=settings.pocket_option_health_check_failure_threshold,
    )
    session_monitor_task = asyncio.create_task(
        run_session_monitor_loop(
            session_monitor,
            interval_seconds=settings.pocket_option_health_check_interval_seconds,
        )
    )

    try:
        # `provider=` and `repo=` are passed through aiogram's
        # dependency-injection mechanism: any handler that declares a
        # matching parameter name receives these instances automatically.
        await dispatcher.start_polling(
            bot, provider=provider, repo=repo, search=search, close_bot_session=False
        )
    finally:
        await search.close()
        resolver_task.cancel()
        session_monitor_task.cancel()
        for task in (resolver_task, session_monitor_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await repo.aclose()
        await storage.close()
        await provider.aclose()
        await bot.session.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows defaults to ProactorEventLoop, which works fine for aiogram
        # but logs harmless "unclosed transport"/ConnectionResetError noise
        # from aiohttp's SSL transports at shutdown. Switching to the
        # selector loop (we don't need Proactor's subprocess support here)
        # is the fix aiohttp's own docs recommend for exactly this case.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
