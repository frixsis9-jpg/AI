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
        # The main menu still opens after all in-memory state was lost.
        methods = await click("menu:pair")
        assert await state.get_state() == SignalSetup.choosing_pair.state
        assert any(isinstance(m, EditMessageText) and "Выберите валютную пару" in m.text for m in methods)
        assert any(isinstance(m, AnswerCallbackQuery) for m in methods)

        # A button in another old menu also works while a submenu is active.
        await click("menu:expiration")
        assert await state.get_state() == SignalSetup.choosing_expiration.state

        # A submenu itself may have been sent before the restart.
        await state.clear()
        await click("pair:EURUSD")
        assert (await state.get_data())["pair"] == "EURUSD"
        assert await state.get_state() == SignalSetup.menu.state
        provider.warm_up.assert_called_once_with(CurrencyPair("EURUSD"))

        for expiration in [next(iter(Expiration.selectable())).name, "AUTO"]:
            await state.clear()
            await click("exp:" + expiration)
            assert (await state.get_data())["expiration"] == expiration
            assert await state.get_state() == SignalSetup.menu.state

        # A signal request with lost settings explains what to choose rather
        # than silently disappearing or starting an analysis with defaults.
        await state.clear()
        methods = await click("menu:signal")
        assert any(isinstance(m, AnswerCallbackQuery) and m.show_alert and "Сначала выберите" in m.text for m in methods)
        provider.get_candles.assert_not_called()
    finally:
        await storage.close()
        await bot.session.close()
