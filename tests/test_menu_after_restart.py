"""Exercise actual callback routing with the empty FSM left by a restart."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.handlers import selection, signal
from bot.states import SignalSetup
from domain import CurrencyPair, Expiration


@pytest.mark.asyncio
async def test_old_keyboards_work_with_empty_state_and_across_submenus():
    session = AsyncMock()
    bot = Bot(token="123456789:" + "a" * 35, session=session)
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(selection.router)
    dispatcher.include_router(signal.router)
    provider = Mock()
    provider.list_available_pairs = AsyncMock(return_value=[CurrencyPair("EURUSD")])
    state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    search = Mock()
    search.start = AsyncMock()
    search.stop = AsyncMock()
    dispatcher["search"] = search
    counter = 0

    async def click(payload):
        nonlocal counter
        counter += 1
        session.reset_mock()
        update = Update(
            update_id=counter,
            callback_query=CallbackQuery(
                id=str(counter),
                from_user=User(id=42, is_bot=False, first_name="Test"),
                chat_instance="local-test",
                data=payload,
                message=Message(
                    message_id=10,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=42, type="private"),
                    text="Menu sent before restart",
                ),
            ),
        )
        await dispatcher.feed_update(bot, update, provider=provider)
        return [call.args[1] for call in session.call_args_list]

    try:
        # The home has two choices. Manual mode walks pair -> expiry -> signal.
        methods = await click("menu:home")
        home = next(m for m in methods if isinstance(m, EditMessageText))
        assert len([b for row in home.reply_markup.inline_keyboard for b in row]) == 2
        methods = await click("menu:manual")
        assert any(
            isinstance(m, EditMessageText) and "Шаг 1" in m.text for m in methods
        )

        # Old callbacks also remain usable after in-memory state was lost.
        methods = await click("menu:pair")
        assert await state.get_state() == SignalSetup.choosing_pair.state
        assert any(
            isinstance(m, EditMessageText) and "Выберите валютную пару" in m.text
            for m in methods
        )
        assert any(isinstance(m, AnswerCallbackQuery) for m in methods)

        # A button in another old menu also works while a submenu is active.
        await click("menu:expiration")
        assert await state.get_state() == SignalSetup.choosing_expiration.state

        # A submenu itself may have been sent before the restart.
        await state.clear()
        await click("pair:EURUSD")
        assert (await state.get_data())["pair"] == "EURUSD"
        assert await state.get_state() == SignalSetup.choosing_expiration.state
        provider.warm_up.assert_called_once_with(CurrencyPair("EURUSD"))

        for expiration in [e.name for e in Expiration.selectable()]:
            await state.clear()
            await click("exp:" + expiration)
            assert (await state.get_data())["expiration"] == expiration
            assert await state.get_state() == SignalSetup.menu.state
            # Manual workspace has only pair, expiry, signal and home.
            methods = await click("menu:back")
            menu = next(m for m in methods if isinstance(m, EditMessageText))
            buttons = [b for row in menu.reply_markup.inline_keyboard for b in row]
            assert len(buttons) == 4
            assert any(b.text == "📊 Получить сигнал" for b in buttons)

        await state.clear()
        methods = await click("exp:AUTO")
        assert "expiration" not in await state.get_data()
        assert any(isinstance(m, AnswerCallbackQuery) and m.show_alert for m in methods)

        await state.update_data(expiration="SEC_10", pair="EURUSD")
        await click("mode:auto")
        assert (await state.get_data())["expiration"] == "SEC_10"
        await click("search:start")
        assert search.start.call_args.args[2].mode == "auto"
        assert search.start.call_args.args[2].expiration.value == 10
        await click("pair:GBPUSD")
        assert (await state.get_data())["pair_mode"] == "manual"
        assert search.stop.await_count > 0

        # A signal request with lost settings explains what to choose rather
        # than silently disappearing or starting an analysis with defaults.
        await state.clear()
        methods = await click("menu:signal")
        assert any(
            isinstance(m, AnswerCallbackQuery)
            and m.show_alert
            and "Сначала выберите" in m.text
            for m in methods
        )
        provider.get_candles.assert_not_called()
    finally:
        await storage.close()
        await bot.session.close()
