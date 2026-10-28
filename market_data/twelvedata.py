"""Twelve Data implementation of MarketDataProvider.

API docs: https://twelvedata.com/docs#time-series
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from domain import CurrencyPair, Timeframe
from market_data.models import Candle, MarketDataError
from market_data.provider import MarketDataProvider

_BASE_URL = "https://api.twelvedata.com"
_TIME_SERIES_ENDPOINT = f"{_BASE_URL}/time_series"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class TwelveDataProvider(MarketDataProvider):
    """Fetches forex candles from the Twelve Data REST API."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("Twelve Data API key must not be empty")
        self._api_key = api_key
        # Allow an external client to be injected (tests, connection pooling
        # shared across providers); own one otherwise.
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
        self._owns_client = client is None

    async def get_candles(
        self,
        pair: CurrencyPair,
        timeframe: Timeframe,
        output_size: int = 150,
    ) -> list[Candle]:
        params = {
            "symbol": pair.label,
            "interval": timeframe.twelvedata_interval,
            "outputsize": output_size,
            "apikey": self._api_key,
            "order": "ASC",  # ask for chronological order directly
            "timezone": "UTC",  # pin the timezone so candle times are unambiguous
        }

        try:
            response = await self._client.get(_TIME_SERIES_ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise MarketDataError(
                f"Network error while fetching {pair.label} {timeframe.value} candles: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError(
                f"Twelve Data returned a non-JSON response (HTTP {response.status_code})"
            ) from exc

        if response.status_code != 200 or payload.get("status") == "error":
            message = payload.get("message", "unknown error")
            raise MarketDataError(
                f"Twelve Data error for {pair.label} {timeframe.value}: {message}"
            )

        values = payload.get("values")
        if not values:
            raise MarketDataError(
                f"Twelve Data returned no candles for {pair.label} {timeframe.value}"
            )

        try:
            candles = [self._parse_candle(item) for item in values]
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError(
                f"Could not parse Twelve Data candle payload for {pair.label} {timeframe.value}: {exc}"
            ) from exc

        # Defensive: sort by time even though we asked for ASC order, in case
        # the API's ordering guarantee ever changes.
        candles.sort(key=lambda c: c.time)
        return candles

    @staticmethod
    def _parse_candle(item: dict) -> Candle:
        return Candle(
            time=datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            volume=float(item["volume"]) if item.get("volume") not in (None, "") else None,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
