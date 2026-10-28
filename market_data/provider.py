"""Abstract interface every market data source must implement.

The rest of the bot (analysis, handlers) only ever talks to this interface,
never to a concrete provider directly. That is what lets us swap Twelve Data
for OANDA, add a second (e.g. OTC) source later, or mock data in tests
without touching any other layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from domain import REGULAR_PAIRS, CurrencyPair, Timeframe
from market_data.models import Candle


class MarketDataProvider(ABC):
    """Fetches recent OHLC candles for a given pair and timeframe."""

    @abstractmethod
    async def get_candles(
        self,
        pair: CurrencyPair,
        timeframe: Timeframe,
        output_size: int = 150,
    ) -> list[Candle]:
        """Return up to `output_size` most recent candles, oldest first.

        Raises:
            market_data.models.MarketDataError: if data cannot be retrieved
                or the response cannot be parsed into candles.
        """
        raise NotImplementedError

    async def list_available_pairs(self) -> list[CurrencyPair]:
        """Which pairs to offer the user for selection right now.

        Default: the fixed baseline of 7 regular forex pairs this bot has
        always supported (`domain.REGULAR_PAIRS`) — overridden by providers
        that can report what's actually tradable live, OTC pairs included
        (see `market_data.pocket_option.PocketOptionProvider`, the only one
        that currently does).
        """
        return list(REGULAR_PAIRS)

    async def aclose(self) -> None:
        """Release any underlying resources (HTTP client, sockets, ...).

        Default no-op — override in providers that own a client.
        """
        return
