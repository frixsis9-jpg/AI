"""Inline keyboard builders for the selection flow."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from domain import CurrencyPair, Expiration


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, action in [
        ("💱 Выбрать пару вручную", "menu:pair"),
        ("🔎 Автовыбор пары", "mode:auto"),
        ("⌛ Выбрать экспирацию", "menu:expiration"),
        ("🌐 Рынок автопоиска", "menu:market"),
        ("☑ Список пар автопоиска", "menu:watchlist"),
        ("▶ Начать поиск", "search:start"),
        ("⏹ Остановить поиск", "search:stop"),
        ("📊 Проверить сейчас", "menu:signal"),
        ("ℹ Статус поиска", "search:status"),
    ]:
        builder.button(text=text, callback_data=action)
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
    """Fixed expiry selected by the user, including experimental seconds."""
    builder = InlineKeyboardBuilder()
    for expiration in Expiration.selectable():
        builder.button(text=expiration.label, callback_data=f"exp:{expiration.name}")
    builder.adjust(4)
    builder.row(_back_button())
    return builder.as_markup()


def new_signal_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Новый сигнал", callback_data="menu:new")
    return builder.as_markup()


def _back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")


def market_keyboard():
    builder = InlineKeyboardBuilder()
    for name, label in [
        ("regular", "Обычные пары"),
        ("otc", "Только OTC"),
        ("both", "Оба рынка"),
    ]:
        builder.button(text=label, callback_data="market:" + name)
    builder.adjust(1)
    builder.row(_back_button())
    return builder.as_markup()


def watchlist_keyboard(pairs, selected):
    builder = InlineKeyboardBuilder()
    for pair in pairs:
        builder.button(
            text=("✅ " if str(pair) in selected else "▫ ") + pair.label,
            callback_data="watch:" + str(pair),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="Все основные пары", callback_data="watch:all")
    )
    builder.row(_back_button())
    return builder.as_markup()
