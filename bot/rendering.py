"""Turns FSM-stored selection data into domain values and user-facing text.

FSM storage only holds JSON-serialisable data. `expiration` is kept as its
enum `.name` string. `pair` is kept as its raw Pocket Option symbol (e.g.
"EURUSD", "EURUSD_otc") — CurrencyPair is a plain `str` subclass (see
domain.py), so that value round-trips through `CurrencyPair(...)` directly,
with no lookup table needed the way the old fixed-enum `.name` did.

There is no timeframe selection here — the bot picks a trio of timeframes
automatically from the chosen expiration (see
analysis.multi_timeframe.timeframe_trio_for and bot/handlers/signal.py), so
it's not part of the user's selection at all.
"""
from __future__ import annotations

from domain import CurrencyPair, Expiration

Selection = tuple[CurrencyPair | None, Expiration | None]

# Stored in FSM instead of an Expiration name when the user lets the analyst
# desk pick the expiration (see analyst/ and bot/handlers/analyst_flow.py).
AUTO = "AUTO"


def is_auto(data: dict) -> bool:
    return data.get("expiration") == AUTO


def parse_selection(data: dict) -> Selection:
    pair_symbol = data.get("pair")
    expiration_name = data.get("expiration")
    pair = CurrencyPair(pair_symbol) if pair_symbol else None
    expiration = Expiration[expiration_name] if expiration_name and expiration_name != AUTO else None
    if expiration is not None and expiration.is_short:
        expiration = None  # chosen before sub-minute expirations were removed
    return pair, expiration


def render_menu_text(data: dict) -> str:
    pair, expiration = parse_selection(data)
    pair_text = pair.label if pair else "не выбрана"
    expiration_text = (
        "🧠 авто — аналитик подберёт" if is_auto(data) else expiration.label if expiration else "не выбрана"
    )
    return (
        "Настройка сигнала для Pocket Option:\n\n"
        f"💱 Валюта: {pair_text}\n"
        f"⌛ Экспирация: {expiration_text}\n\n"
        "Бот анализирует 3 таймфрейма по 120 свечей в реальном времени "
        "(ликвидность, ретесты уровней, структура, имбалансы, индикаторы) и "
        "всегда выдаёт направление ВВЕРХ/ВНИЗ с вероятностью успеха в % и "
        "причинами за и против.\n"
        "Выберите, что изменить, либо запросите сигнал, когда всё задано.\n"
        "Команда /stats — статистика по сохранённым сигналам."
    )
