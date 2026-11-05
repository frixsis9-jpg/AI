"""Immutable, timestamped inputs shared by manual analysis and the scanner."""

import math
from dataclasses import dataclass

from domain import CurrencyPair, Timeframe
from market_data.models import Candle, MarketDataError


@dataclass(frozen=True)
class MarketSnapshot:
    pair: CurrencyPair
    captured_at: float  # UTC Unix time; never corrected to hide network latency
    tick_time: float
    received_age: float
    price: float
    candles: dict[Timeframe, list[Candle]]
    ticks: tuple[tuple[float, float], ...]

    @property
    def age(self) -> float:
        return max(self.received_age, self.captured_at - self.tick_time)

    def validate(self, timeframes, *, max_age: float, count: int = 120) -> None:
        if (
            not all(
                math.isfinite(v)
                for v in (
                    self.price,
                    self.captured_at,
                    self.tick_time,
                    self.received_age,
                )
            )
            or self.received_age < 0
        ):
            raise MarketDataError("Недопустимые временные метки или цена")
        if self.price <= 0:
            raise MarketDataError("Недопустимая цена")
        if self.tick_time > self.captured_at + 0.5:
            raise MarketDataError(
                "Время котировки в будущем. Проверьте синхронизацию часов ПК"
            )
        if self.age > max_age:
            raise MarketDataError(
                f"Котировка устарела: {self.age:.3f} с; допустимо {max_age:.3f} с"
            )
        for tf in timeframes:
            closed = [c for c in self.candles.get(tf, []) if c.is_closed][-count:]
            if len(closed) < count:
                raise MarketDataError(
                    f"{tf.value}: история догружается ({len(closed)}/{count} свечей)"
                )
            if any(
                (b.time - a.time).total_seconds() != tf.seconds
                for a, b in zip(closed, closed[1:])
            ):
                raise MarketDataError(f"{tf.value}: пропуски или повторы в истории")
            expected_end = int(self.tick_time) // tf.seconds * tf.seconds
            if int(closed[-1].time.timestamp()) + tf.seconds != expected_end:
                raise MarketDataError(
                    f"{tf.value}: нет последней полной закрытой свечи"
                )
            for c in closed:
                if (
                    not all(
                        math.isfinite(v) and v > 0
                        for v in (c.open, c.high, c.low, c.close)
                    )
                    or not c.low
                    <= min(c.open, c.close)
                    <= max(c.open, c.close)
                    <= c.high
                ):
                    raise MarketDataError(f"{tf.value}: повреждённые OHLC")
