"""Small aiogram helpers shared across handlers."""
from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def safe_edit_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """`Message.edit_text`, ignoring Telegram's "message is not modified" error.

    That error fires when the user re-selects a value that produces the exact
    same text/markup already shown — harmless, but noisy if left uncaught.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
