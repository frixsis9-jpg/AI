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


def render_home_text():
    return "Выберите режим:\n\n💱 Ручной сигнал — выберите пару и экспирацию, затем нажмите «Получить сигнал».\n🔎 Автопоиск — бот сам ищет пару под вашу экспирацию."


def render_menu_text(data):
    pair, expiration = parse_selection(data)
    exp = expiration.label if expiration else "не выбрана"
    if data.get("pair_mode") == "auto":
        market = {"both": "Обычные + OTC", "regular": "Обычные пары", "otc": "OTC"}.get(
            data.get("market", "both"), "Обычные + OTC"
        )
        count = len(data.get("watchlist", []))
        text = f"🔎 Автопоиск\n\n⌛ Экспирация: {exp}\n🌐 Рынок: {market}\nПары: {str(count) + ' выбранных' if count else 'основной список'}\n\nНажмите «Начать автопоиск». Ход проверки появится здесь."
    else:
        text = f"💱 Ручной сигнал\n\nПара: {pair.label if pair else 'не выбрана'}\n⌛ Экспирация: {exp}\n\nНажмите «Получить сигнал». Бот проверит именно эту пару."
    if expiration and expiration.is_short:
        text += "\n\n5–30 секунд — экспериментальный режим."
    return text
