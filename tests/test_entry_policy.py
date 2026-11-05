from dataclasses import replace

import pytest

from analysis.entry import age_limit, quote_age_limit, evaluate, evaluate_manual
from analysis.support_resistance import Level
from domain import Expiration
from market_data.models import MarketDataError


@pytest.mark.parametrize("seconds", [5, 10, 15, 30, 60, 180, 300, 900, 3600])
def test_profile_age_limits_shrink_for_short_expirations(seconds):
    assert 0 < age_limit(Expiration(seconds)) <= 5


@pytest.mark.parametrize("seconds", [5, 10, 15, 30, 60, 180, 300, 900, 3600])
def test_shared_policy_accepts_valid_supported_profiles(
    seconds, candidate_factory, monkeypatch
):
    exp = Expiration(seconds)
    c = candidate_factory(exp)
    monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *a: c.result)
    accepted = evaluate(c.snapshot, exp)
    assert accepted.direction == "CALL"
    assert accepted.result.expiration == exp


def test_weak_model_and_disagreement_are_rejected(candidate_factory, monkeypatch):
    c = candidate_factory()
    for result in [replace(c.result, logit=0.01), replace(c.result, agreement=1)]:
        monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *a: result)
        with pytest.raises(MarketDataError):
            evaluate(c.snapshot, c.result.expiration)
        manual = evaluate_manual(c.snapshot, c.result.expiration)
        assert manual.direction == result.final_direction
        assert manual.manual
        assert any("⚠" in reason for reason in manual.reasons)


def test_ticks_and_current_price_are_gates_not_extra_probability_votes(
    candidate_factory, monkeypatch
):
    c = candidate_factory(Expiration.SEC_10)
    monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *a: c.result)
    result = evaluate(c.snapshot, c.result.expiration)
    assert result.result.logit == c.result.logit
    with pytest.raises(MarketDataError, match="тиков"):
        evaluate(replace(c.snapshot, ticks=c.snapshot.ticks[-2:]), c.result.expiration)
    falling = tuple((t, 2 - p) for t, p in c.snapshot.ticks)
    with pytest.raises(MarketDataError, match="не подтверждают"):
        evaluate(replace(c.snapshot, ticks=falling), c.result.expiration)


def test_current_opposing_level_and_overextension_block_entry(
    candidate_factory, monkeypatch
):
    c = candidate_factory()
    atr = c.result.primary.indicators.atr
    primary = replace(
        c.result.primary, levels=[Level(c.snapshot.price + 0.1 * atr, "resistance", 3)]
    )
    model = replace(c.result, per_timeframe=[primary] + c.result.per_timeframe[1:])
    monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *a: model)
    with pytest.raises(MarketDataError, match="уровень"):
        evaluate(c.snapshot, c.result.expiration)
    monkeypatch.setattr("analysis.entry.analyze_multi_timeframe", lambda *a: c.result)
    with pytest.raises(MarketDataError, match="ушла"):
        evaluate(
            replace(c.snapshot, price=c.snapshot.price + 10 * atr), c.result.expiration
        )


def test_existing_closed_candle_analysis_does_not_change_with_forming_candle(
    snapshot_factory,
):
    from analysis.multi_timeframe import analyze_multi_timeframe

    s = snapshot_factory()
    a = analyze_multi_timeframe(s.pair, Expiration.MIN_3, s.candles, s.price)
    changed = {
        tf: rows[:-1] + [replace(rows[-1], high=2, close=2)]
        for tf, rows in s.candles.items()
    }
    b = analyze_multi_timeframe(s.pair, Expiration.MIN_3, changed, 2)
    assert a.logit == b.logit
    assert a.primary.current_price != b.primary.current_price


def test_analysis_tolerates_small_quote_jitter_without_widening_settlement_limits(
    candidate_factory, monkeypatch
):
    c = candidate_factory(Expiration.SEC_15, now=1_800_000_012)
    monkeypatch.setattr(
        "analysis.entry.analyze_multi_timeframe", lambda *args: c.result
    )
    jitter = replace(
        c.snapshot, tick_time=c.snapshot.captured_at - 1.1, received_age=0.05
    )
    assert age_limit(Expiration.SEC_15) == 1
    assert quote_age_limit(Expiration.SEC_15) == 1.25
    assert evaluate_manual(jitter, Expiration.SEC_15).direction == c.direction
    with pytest.raises(MarketDataError, match="устарела"):
        jitter.validate(jitter.candles, max_age=age_limit(Expiration.SEC_15))
    for bad in [
        replace(jitter, tick_time=jitter.captured_at - 2),
        replace(jitter, received_age=3),
    ]:
        with pytest.raises(MarketDataError, match="устарела"):
            evaluate_manual(bad, Expiration.SEC_15)
