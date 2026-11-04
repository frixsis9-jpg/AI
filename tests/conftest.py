import math
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from analysis.entry import Candidate
from analysis.multi_timeframe import analyze_multi_timeframe, timeframe_trio_for
from domain import CurrencyPair, Expiration
from market_data.models import Candle
from market_data.snapshot import MarketSnapshot


@pytest.fixture
def snapshot_factory():
    def make(exp=Expiration.MIN_3, now=None, pair="EURUSD", direction=1):
        now = time.time() if now is None else now
        tick_time = now - 0.1
        candles = {}
        for tf in timeframe_trio_for(exp):
            end = int(tick_time) // tf.seconds * tf.seconds
            rows = []
            for i in range(120):
                price = 1.1 + 0.0001 * math.sin(i * 0.4)
                rows.append(
                    Candle(
                        datetime.fromtimestamp(
                            end - (120 - i) * tf.seconds, tz=timezone.utc
                        ),
                        price,
                        price + 0.0003,
                        price - 0.0003,
                        price + 0.00003,
                    )
                )
            rows.append(
                Candle(
                    datetime.fromtimestamp(end, tz=timezone.utc),
                    1.1,
                    1.1003,
                    1.0997,
                    1.1001,
                    is_closed=False,
                )
            )
            candles[tf] = rows
        ticks = tuple(
            (now - 30 + i * 0.1, 1.1001 - direction * (30 - i * 0.1) * 0.000001)
            for i in range(300)
        )
        return MarketSnapshot(
            CurrencyPair(pair), now, tick_time, 0.1, 1.1001, candles, ticks
        )

    return make


@pytest.fixture
def candidate_factory(snapshot_factory):
    def make(
        exp=Expiration.MIN_3, now=None, pair="EURUSD", direction="CALL", quality=80
    ):
        snap = snapshot_factory(exp, now, pair, 1 if direction == "CALL" else -1)
        result = analyze_multi_timeframe(snap.pair, exp, snap.candles, snap.price)
        primaries = [
            replace(
                r,
                levels=[],
                indicators=replace(r.indicators, atr_ratio=1.0),
                confirmation=replace(r.confirmation, direction=direction),
            )
            for r in result.per_timeframe
        ]
        result = replace(
            result,
            per_timeframe=primaries,
            final_direction=direction,
            agreement=3,
            logit=0.5 if direction == "CALL" else -0.5,
            probability=62,
        )
        return Candidate(snap, result, quality, ("test",))

    return make
