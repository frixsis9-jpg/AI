"""Handlers for the 💱/⌛ selection sub-menus and ◀️ Назад / 🔄 Новый сигнал.

No timeframe sub-menu here — see bot/states.py and bot/rendering.py for why.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.keyboards import expiration_keyboard, main_menu_keyboard, pair_keyboard
from bot.rendering import AUTO, render_menu_text
from bot.states import SignalSetup
from bot.utils import safe_edit_text
from domain import CurrencyPair, Expiration
from market_data.provider import MarketDataProvider

router = Router(name="selection")


async def _show_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SignalSetup.menu)
    data = await state.get_data()
    await safe_edit_text(callback.message, render_menu_text(data), main_menu_keyboard())


# --- opening a sub-menu -------------------------------------------------

@router.callback_query(SignalSetup.menu, F.data == "menu:pair")
async def open_pair_menu(
    callback: CallbackQuery, state: FSMContext, provider: MarketDataProvider
) -> None:
    # Live list (regular + OTC), not a fixed compile-time set — see
    # domain.CurrencyPair and PocketOptionProvider.list_available_pairs().
    pairs = await provider.list_available_pairs()
    await state.set_state(SignalSetup.choosing_pair)
    await safe_edit_text(callback.message, "Выберите валютную пару:", pair_keyboard(pairs))
    await callback.answer()


@router.callback_query(SignalSetup.menu, F.data == "menu:expiration")
async def open_expiration_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SignalSetup.choosing_expiration)
    await safe_edit_text(callback.message, "Выберите время экспирации:", expiration_keyboard())
    await callback.answer()


# --- making a choice inside a sub-menu ----------------------------------

@router.callback_query(SignalSetup.choosing_pair, F.data.startswith("pair:"))
async def choose_pair(
    callback: CallbackQuery, state: FSMContext, provider: MarketDataProvider | None = None
) -> None:
    pair = CurrencyPair(callback.data.split(":", 1)[1])
    await state.update_data(pair=str(pair))
    # Start the pair's live feed right away (history backfill + tick
    # subscription), so it's already warm by the time "📊 Получить сигнал"
    # is pressed — see market_data/live_feed.py.
    warm_up = getattr(provider, "warm_up", None)
    if warm_up is not None:
        warm_up(pair)
    await _show_main_menu(callback, state)
    await callback.answer(f"Валюта: {pair.label}")


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    """The non-clickable "— Обычные —" / "— OTC —" group headers in
    pair_keyboard() still need some callback answer, or Telegram leaves the
    button showing a spinner until it times out.
    """
    await callback.answer()


@router.callback_query(SignalSetup.choosing_expiration, F.data.startswith("exp:"))
async def choose_expiration(callback: CallbackQuery, state: FSMContext) -> None:
    name = callback.data.split(":", 1)[1]
    if name == AUTO:
        await state.update_data(expiration=AUTO)
        await _show_main_menu(callback, state)
        await callback.answer("Экспирацию подберёт аналитик")
        return
    expiration = Expiration[name]
    if expiration.is_short:
        # a stale keyboard from before sub-minute expirations were removed
        await callback.answer("Экспирации меньше 1 минуты больше не поддерживаются", show_alert=True)
        return
    await state.update_data(expiration=expiration.name)
    await _show_main_menu(callback, state)
    await callback.answer(f"Экспирация: {expiration.label}")


# --- navigation ----------------------------------------------------------

@router.callback_query(F.data == "menu:back")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "menu:new")
async def new_signal(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pair=None, expiration=None)
    await _show_main_menu(callback, state)
    await callback.answer()
