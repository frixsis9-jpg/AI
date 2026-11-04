"""Self-identifying callbacks survive restarts; any setting change stops search."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.keyboards import (
    expiration_keyboard,
    main_menu_keyboard,
    market_keyboard,
    pair_keyboard,
    settings_keyboard,
    watchlist_keyboard,
    workspace_keyboard,
)
from bot.rendering import parse_selection, render_home_text, render_menu_text
from bot.states import SignalSetup
from bot.utils import safe_edit_text
from domain import CurrencyPair, Expiration
from market_data.provider import MarketDataProvider
from scanner.service import ALLOWLIST, SearchOptions

router = Router(name="selection")


async def stop_search(callback, search):
    if search is not None:
        await search.stop(callback.message.chat.id, callback.from_user.id)


async def _show_main_menu(callback, state):
    await state.set_state(SignalSetup.menu)
    await safe_edit_text(
        callback.message,
        render_menu_text(await state.get_data()),
        workspace_keyboard(await state.get_data()),
    )


@router.callback_query(F.data == "menu:pair")
async def open_pair_menu(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    search=None,
):
    await callback.answer()
    await stop_search(callback, search)
    pairs = await provider.list_available_pairs()
    await state.set_state(SignalSetup.choosing_pair)
    await safe_edit_text(
        callback.message, "Выберите валютную пару:", pair_keyboard(pairs)
    )


@router.callback_query(F.data.startswith("pair:"))
async def choose_pair(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider | None = None,
    search=None,
):
    await stop_search(callback, search)
    pair = CurrencyPair(callback.data.split(":", 1)[1])
    await state.update_data(pair=str(pair), pair_mode="manual")
    if provider is not None:
        provider.warm_up(pair)
    await callback.answer("Ручной выбор: " + pair.label)
    await state.set_state(SignalSetup.choosing_expiration)
    await safe_edit_text(
        callback.message,
        f"Пара: {pair.label}. Теперь выберите экспирацию:",
        expiration_keyboard(),
    )


@router.callback_query(F.data == "menu:manual")
async def manual_mode(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    search=None,
):
    await stop_search(callback, search)
    await state.update_data(pair_mode="manual")
    await callback.answer()
    await state.set_state(SignalSetup.choosing_pair)
    await safe_edit_text(
        callback.message,
        "Шаг 1 из 2. Выберите валютную пару:",
        pair_keyboard(await provider.list_available_pairs()),
    )


@router.callback_query(F.data == "menu:home")
async def home(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await callback.answer()
    await safe_edit_text(callback.message, render_home_text(), main_menu_keyboard())


@router.callback_query(F.data == "menu:settings")
async def settings(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await callback.answer()
    await safe_edit_text(
        callback.message,
        "Настройки автопоиска. Выберите рынок или ограничьте список пар.",
        settings_keyboard(),
    )


@router.callback_query(F.data == "mode:auto")
async def auto_pair(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await state.update_data(pair_mode="auto")
    await callback.answer("Автовыбор пары. Экспирация остаётся вашей")
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "menu:expiration")
async def open_expiration_menu(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await state.set_state(SignalSetup.choosing_expiration)
    await callback.answer()
    await safe_edit_text(
        callback.message, "Выберите фиксированную экспирацию:", expiration_keyboard()
    )


@router.callback_query(F.data.startswith("exp:"))
async def choose_expiration(callback: CallbackQuery, state: FSMContext, search=None):
    try:
        expiration = Expiration[callback.data.split(":", 1)[1]]
        if expiration not in Expiration.selectable():
            raise KeyError
    except KeyError:
        await callback.answer(
            "Выберите фиксированную экспирацию в меню", show_alert=True
        )
        return
    await stop_search(callback, search)
    await state.update_data(expiration=expiration.name)
    await callback.answer("Экспирация: " + expiration.label)
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "menu:market")
async def open_market(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await callback.answer()
    await safe_edit_text(
        callback.message, "Рынок автоматического поиска:", market_keyboard()
    )


@router.callback_query(F.data.startswith("market:"))
async def choose_market(callback: CallbackQuery, state: FSMContext, search=None):
    market = callback.data.split(":", 1)[1]
    if market not in ("regular", "otc", "both"):
        await callback.answer("Неизвестный рынок")
        return
    await stop_search(callback, search)
    await state.update_data(market=market)
    await callback.answer()
    await _show_main_menu(callback, state)


async def show_watchlist(callback, state, provider):
    pairs = [
        p
        for p in await provider.list_available_pairs()
        if str(p).removesuffix("_otc") in ALLOWLIST
    ]
    data = await state.get_data()
    await safe_edit_text(
        callback.message,
        "Отметьте пары. Пустой список означает все доступные основные пары.",
        watchlist_keyboard(pairs, data.get("watchlist", [])),
    )


@router.callback_query(F.data == "menu:watchlist")
async def open_watchlist(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    search=None,
):
    await stop_search(callback, search)
    await callback.answer()
    await show_watchlist(callback, state, provider)


@router.callback_query(F.data.startswith("watch:"))
async def choose_watchlist(
    callback: CallbackQuery,
    state: FSMContext,
    provider: MarketDataProvider,
    search=None,
):
    await stop_search(callback, search)
    symbol = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("watchlist", []))
    if symbol == "all":
        selected.clear()
    elif symbol.removesuffix("_otc") in ALLOWLIST:
        selected.symmetric_difference_update([symbol])
    else:
        await callback.answer("Пара не входит в основной список")
        return
    await state.update_data(watchlist=sorted(selected))
    await callback.answer()
    await show_watchlist(callback, state, provider)


def options_from_data(data):
    pair, expiration = parse_selection(data)
    if expiration is None:
        raise ValueError("Сначала выберите экспирацию")
    options = SearchOptions(
        expiration,
        data.get("pair_mode", "manual"),
        pair,
        data.get("market", "both"),
        tuple(data.get("watchlist", [])),
    )
    options.validate()
    return options


async def begin_search(callback, state, search, once=False):
    if search is None:
        await callback.answer(
            "Поиск недоступен: перезапустите обновлённый бот", show_alert=True
        )
        return
    try:
        options = options_from_data(await state.get_data())
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Проверяю выбранную пару" if once else "Запускаю автопоиск")
    data = await state.get_data()
    text = (
        f"⏳ Проверяю {options.pair.label}, {options.expiration.label}. Загружаю свечи…"
        if options.mode == "manual"
        else f"🔎 Автопоиск запущен. Экспирация: {options.expiration.label}."
    )
    await safe_edit_text(callback.message, text, workspace_keyboard(data, active=True))
    await search.start(
        callback.message.chat.id,
        callback.from_user.id,
        options,
        once=once,
        progress=(
            callback.message.message_id,
            workspace_keyboard(data, active=True),
            workspace_keyboard(data),
        ),
    )


@router.callback_query(F.data == "search:start")
async def start_search(callback: CallbackQuery, state: FSMContext, search=None):
    await begin_search(callback, state, search)


@router.callback_query(F.data == "search:stop")
async def end_search(callback: CallbackQuery, state: FSMContext, search=None):
    await stop_search(callback, search)
    await callback.answer("Поиск остановлен")
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "search:status")
async def search_status(callback: CallbackQuery, state: FSMContext, search=None):
    await callback.answer()
    text = (
        search.status(callback.message.chat.id, callback.from_user.id)
        if search
        else "Поиск остановлен"
    )
    await safe_edit_text(
        callback.message,
        text + "\n\n" + render_menu_text(await state.get_data()),
        workspace_keyboard(
            await state.get_data(),
            active=search.active(callback.message.chat.id, callback.from_user.id)
            if search
            else False,
        ),
    )


@router.callback_query(F.data == "menu:back")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "menu:new")
async def new_signal(callback: CallbackQuery, state: FSMContext, search=None):
    # Retain chosen duration/pair; switching settings is explicit in the menu.
    await stop_search(callback, search)
    await callback.answer()
    await _show_main_menu(callback, state)


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()
