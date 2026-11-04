"""Human-readable persistent menu choices. Search tasks are never restored."""

from domain import CurrencyPair, Expiration

AUTO = "AUTO"  # old callback compatibility; new search requires a fixed expiry


def is_auto(data):
    return data.get("expiration") == AUTO


def parse_selection(data):
    pair = CurrencyPair(data["pair"]) if data.get("pair") else None
    try:
        expiration = Expiration[data.get("expiration", "")]
    except KeyError:
        expiration = None
    if expiration not in Expiration.selectable():
        expiration = None
    return pair, expiration


def render_menu_text(data):
    pair, expiration = parse_selection(data)
    mode = (
        "🔎 Автовыбор пары"
        if data.get("pair_mode") == "auto"
        else "💱 Ручной выбор пары"
    )
    market = {"both": "Обычные + OTC", "regular": "Обычные", "otc": "OTC"}.get(
        data.get("market", "both"), "Обычные + OTC"
    )
    pair_text = (
        "автопоиск"
        if data.get("pair_mode") == "auto"
        else pair.label
        if pair
        else "не выбрана"
    )
    watch = data.get("watchlist", [])
    return (
        "Настройка поиска входа:\n\n"
        f"Режим: {mode}\nПара: {pair_text}\n"
        f"Экспирация: {expiration.label if expiration else 'выберите время'}\n"
        f"Рынок автопоиска: {market}\nСписок автопоиска: {str(len(watch)) + ' пар' if watch else 'все доступные основные пары'}\n\n"
        "Выберите экспирацию и пару либо автовыбор. Нажмите «Начать поиск».\n"
        "При слабых условиях бот ждёт. План входа предварительный до подтверждения.\n"
        "5–30 секунд — экспериментальный режим; время ручного входа критично.\n"
        "После перезапуска настройки сохраняются, поиск запускается кнопкой.\n/stats — модельные результаты, а не ваши реальные сделки."
    )
