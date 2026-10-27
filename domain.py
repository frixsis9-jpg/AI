"""Shared domain vocabulary: currency pairs, analysis timeframes and expirations.

Kept in one place (independent of both `bot/` and `market_data/`) so neither
layer has to import the other just to agree on what "5M" or "EUR/USD" means.
"""
from __future__ import annotations

from enum import Enum


class CurrencyPair(str):
    """A tradable pair as currently offered by Pocket Option — regular or OTC.

    This used to be a fixed `Enum` with exactly 7 hardcoded regular-forex
    members. It no longer is: OTC pairs open and close over time (that's the
    whole point of "все OTC катировки, которые сейчас активны" — a fixed
    curated list would drift out of date), so the selectable list is now
    built live from Pocket Option's own `active_assets()` — see
    `market_data.pocket_option.PocketOptionProvider.list_available_pairs()`.

    A plain `str` subclass on purpose: the value itself *is* the Pocket
    Option / BinaryOptionsToolsV2 broker symbol (e.g. "EURUSD", "EURUSD_otc"
    for its OTC twin), so it round-trips unchanged through Telegram
    callback_data, aiogram FSM storage (JSON) and SQLite — exactly like a
    plain string always did, no separate encode/decode step needed anywhere
    that used to just store `.value`/`.name`.

    `CurrencyPair.EUR_USD` and friends below are kept as ready-made constants
    for the 7 pairs the bot has always supported (used as the offline
    fallback list and in scripts/tests) — but nothing stops constructing any
    other pair directly, e.g. `CurrencyPair("EURUSD_otc")`.
    """

    __slots__ = ()

    @property
    def is_otc(self) -> bool:
        return self.endswith("_otc")

    @property
    def label(self) -> str:
        """Human-friendly label for buttons/messages, e.g. "EUR/USD" or,
        for an OTC pair, "EUR/USD (OTC)".

        Falls back to the raw symbol unchanged for anything that isn't a
        plain 6-letter currency code (Pocket Option's active_assets() also
        lists commodities/indices/crypto/stocks under the same call, and
        this bot's analysis was only ever built and tested for forex-style
        pairs — see `market_data.pocket_option._looks_like_currency_pair`,
        which is what keeps those out of the selectable list in the first
        place) rather than guessing a split point that could be wrong.
        """
        base_symbol = self.removesuffix("_otc")
        if len(base_symbol) == 6 and base_symbol.isalpha():
            base = f"{base_symbol[:3]}/{base_symbol[3:]}".upper()
        else:
            base = base_symbol
        return f"{base} (OTC)" if self.is_otc else base

    @property
    def pocket_option_symbol(self) -> str:
        """Ticker format Pocket Option / BinaryOptionsToolsV2 expects — this
        pair's value already IS that ticker, so this just hands it back as a
        plain `str` (some call sites want that instead of the CurrencyPair
        wrapper itself).
        """
        return str(self)


# Ready-made constants for the 7 plain (non-OTC) forex pairs this bot has
# supported since the very first version. Used as: (a) the offline fallback
# list `PocketOptionProvider.list_available_pairs()` falls back to when it
# can't reach Pocket Option at all (see market_data/pocket_option.py), so
# the 💱 menu is never simply empty; (b) TwelveDataProvider's fixed pair set
# (that API has no "what's tradable right now" endpoint to query live); and
# (c) a stable, known-good pair for scripts/tests to default to.
CurrencyPair.EUR_USD = CurrencyPair("EURUSD")
CurrencyPair.GBP_USD = CurrencyPair("GBPUSD")
CurrencyPair.USD_JPY = CurrencyPair("USDJPY")
CurrencyPair.AUD_USD = CurrencyPair("AUDUSD")
CurrencyPair.USD_CHF = CurrencyPair("USDCHF")
CurrencyPair.USD_CAD = CurrencyPair("USDCAD")
CurrencyPair.NZD_USD = CurrencyPair("NZDUSD")

REGULAR_PAIRS: tuple[CurrencyPair, ...] = (
    CurrencyPair.EUR_USD,
    CurrencyPair.GBP_USD,
    CurrencyPair.USD_JPY,
    CurrencyPair.AUD_USD,
    CurrencyPair.USD_CHF,
    CurrencyPair.USD_CAD,
    CurrencyPair.NZD_USD,
)


class Timeframe(str, Enum):
    """Analysis timeframe — the candle interval indicators are computed on.

    S15 (15-second native candles) was added for the "fast" single-timeframe
    signal mode (see bot/handlers/signal.py) — by request, that mode analyzes
    only this one timeframe instead of the M1/M5/M15/etc trio, so signals
    come back as fast as possible for very short expirations.
    """

    S15 = "15S"
    M1 = "1M"
    M5 = "5M"
    M15 = "15M"
    M30 = "30M"
    H1 = "1H"

    @property
    def seconds(self) -> int:
        return {
            Timeframe.S15: 15,
            Timeframe.M1: 60,
            Timeframe.M5: 300,
            Timeframe.M15: 900,
            Timeframe.M30: 1800,
            Timeframe.H1: 3600,
        }[self]

    @property
    def twelvedata_interval(self) -> str:
        """Maps our short code to the `interval` value Twelve Data's API expects.

        Kept for the (currently unused-by-default) TwelveDataProvider fallback.
        Twelve Data has no sub-1-minute interval, so S15 maps to "1min" as the
        closest available approximation — irrelevant in practice since S15 is
        only ever fetched from PocketOptionProvider, which has real 15s bars.
        """
        return {
            Timeframe.S15: "1min",
            Timeframe.M1: "1min",
            Timeframe.M5: "5min",
            Timeframe.M15: "15min",
            Timeframe.M30: "30min",
            Timeframe.H1: "1h",
        }[self]

    @property
    def pocket_option_base(self) -> PocketOptionBase:
        """How to build this timeframe from Pocket Option's native candle feed.

        The Pocket Option / BinaryOptionsToolsV2 feed only serves candles at a
        fixed set of native periods — 1, 5, 15, 30, 60 and 300 seconds. 15S
        maps directly onto the native 15-second period; there is nothing
        native above 5 minutes, so 15M/30M/1H are built client-side by
        grouping consecutive 300s (5-minute) candles.
        """
        return _POCKET_OPTION_BASE[self]

    @property
    def higher_timeframe(self) -> Timeframe:
        """The next timeframe up, used later for higher-timeframe trend context."""
        order = {
            Timeframe.S15: Timeframe.M1,
            Timeframe.M1: Timeframe.M15,
            Timeframe.M5: Timeframe.M30,
            Timeframe.M15: Timeframe.H1,
            Timeframe.M30: Timeframe.H1,
            Timeframe.H1: Timeframe.H1,
        }
        return order[self]


class PocketOptionBase:
    """Native Pocket Option candle period + how many of them make up one bar
    of a given analysis Timeframe.
    """

    __slots__ = ("group_size", "period_seconds")

    def __init__(self, period_seconds: int, group_size: int) -> None:
        self.period_seconds = period_seconds
        self.group_size = group_size


_POCKET_OPTION_BASE: dict[Timeframe, PocketOptionBase] = {
    Timeframe.S15: PocketOptionBase(period_seconds=15, group_size=1),
    Timeframe.M1: PocketOptionBase(period_seconds=60, group_size=1),
    Timeframe.M5: PocketOptionBase(period_seconds=300, group_size=1),
    Timeframe.M15: PocketOptionBase(period_seconds=300, group_size=3),
    Timeframe.M30: PocketOptionBase(period_seconds=300, group_size=6),
    Timeframe.H1: PocketOptionBase(period_seconds=300, group_size=12),
}


class Expiration(int, Enum):
    """Option expiration time in seconds — independent of the analysis timeframe.

    Range 5 seconds to 1 hour, matching what Pocket Option itself offers.
    Anything under 1 minute is treated as an experimental regime by the
    signal-generation logic (stricter confidence threshold) once that layer
    is built — the interface still exposes all of them.
    """

    SEC_5 = 5
    SEC_10 = 10
    SEC_15 = 15
    SEC_20 = 20
    SEC_30 = 30
    MIN_1 = 60
    MIN_2 = 120
    MIN_3 = 180
    MIN_5 = 300
    MIN_10 = 600
    MIN_15 = 900
    MIN_30 = 1800
    HOUR_1 = 3600

    @classmethod
    def selectable(cls) -> list[Expiration]:
        """Expirations offered in the ⌛ menu. Sub-minute ones (5-30 s) are
        kept in the enum only so old stored signals still load; they're
        not offered any more — at that horizon price is dominated by tick
        noise, which no amount of analysis can read (see README)."""
        return [e for e in cls if not e.is_short]

    @property
    def is_short(self) -> bool:
        """True for sub-1-minute expirations (legacy, no longer selectable)."""
        return self.value < 60

    @property
    def label(self) -> str:
        if self.value < 60:
            return f"{self.value} сек"
        if self.value < 3600:
            minutes = self.value // 60
            return f"{minutes} мин"
        hours = self.value // 3600
        return f"{hours} ч"
