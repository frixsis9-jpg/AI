"""/start — resets selection and shows the main menu."""
import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards import main_menu_keyboard
from bot.rendering import render_menu_text
from bot.states import SignalSetup

logger = logging.getLogger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # Logged at INFO, not shown to the user: the simplest way to find your
    # own numeric chat_id for TELEGRAM_ADMIN_CHAT_ID (see .env.example) is to
    # message this bot once and read it from these logs, rather than adding
    # a third-party bot to the mix just to look up an ID.
    logger.info("chat_id=%s used /start", message.chat.id)
    await state.set_state(SignalSetup.menu)
    await state.update_data(pair=None, expiration=None)
    data = await state.get_data()
    await message.answer(render_menu_text(data), reply_markup=main_menu_keyboard())
