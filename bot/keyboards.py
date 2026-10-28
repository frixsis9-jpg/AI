"""Inline keyboard builders for the selection flow."""
from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from domain import CurrencyPair, Expiration


def main_menu_keyboard() -> InlineKeyboardMarkup:
    # No "выбрать таймфрейм" step anymore — the bot analyzes several
    # timeframes automatically per request (see analysis.multi_timeframe),
    # so there's one fewer choice/step between opening the bot and a signal.
    builder = InlineKeyboardBuilder()
    builder.button(text="💱 Выбрать валюту", callback_data="menu:pair")
    builder.button(text="⌛ Выбрать экспирацию", callback_data="menu:expiration")
    builder.button(text="📊 Получить сигнал", callback_data="menu:signal")
    builder.adjust(1)
    return builder.as_markup()


def pair_keyboard(pairs: Sequence[CurrencyPair]) -> InlineKeyboardMarkup:
    """`pairs` is whatever the market data provider's `list_available_pairs()`
    returned for THIS request — a live list that can include OTC pairs and
    changes over time, not a fixed compile-time set (see domain.CurrencyPair).
    Regular and OTC pairs are shown as two separate labeled groups so it's
    obvious which is which.
    """
    regular = [p for p in pairs if not p.is_otc]
    otc = [p for p in pairs if p.is_otc]

    builder = InlineKeyboardBuilder()
    sizes: list[int] = []

    if regular:
        builder.button(text="— Обычные —", callback_data="noop")
        for pair in regular:
            builder.button(text=pair.label, callback_data=f"pair:{pair}")
        sizes += [1] + [2] * -(-len(regular) // 2)  # header row, then 2 per row

    if otc:
        builder.button(text="— OTC (круглосуточно) —", callback_data="noop")
        for pair in otc:
            builder.button(text=pair.label, callback_data=f"pair:{pair}")
        sizes += [1] + [2] * -(-len(otc) // 2)

    if sizes:
        builder.adjust(*sizes)
    builder.row(_back_button())
    return builder.as_markup()


def expiration_keyboard() -> InlineKeyboardMarkup:
    """1 мин .. 1 ч (sub-minute expirations are no longer offered, see
    Expiration.selectable) — laid out 4 per row to keep it compact."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🧠 Авто — аналитик подберёт", callback_data="exp:AUTO")
    for expiration in Expiration.selectable():
        builder.button(text=expiration.label, callback_data=f"exp:{expiration.name}")
    builder.adjust(1, 4, 4)
    builder.row(_back_button())
    return builder.as_markup()


def new_signal_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Новый сигнал", callback_data="menu:new")
    return builder.as_markup()


def _back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")
