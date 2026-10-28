"""Keeps the shown probability honest by checking it against real outcomes.

The model's probability (analysis.confirmation) is an estimate from priors
or fitted weights. Every settled signal tells us how often signals with a
similar estimate actually won. `calibrate()` blends the two per probability
bucket with Bayesian shrinkage:

    shown = (wins + K · p_model) / (n + K)

With few outcomes (n ≪ K) the model's own number dominates; as outcomes
accumulate, the shown number converges to the real hit rate of that
bucket. So if the model says "68%" but such signals really win 58% of the
time, users see a number approaching 58% — never an inflated promise.
"""
from __future__ import annotations

from dataclasses import dataclass

# (low, high) bounds of probability buckets, %.
BUCKETS: tuple[tuple[float, float], ...] = ((50, 55), (55, 60), (60, 65), (65, 70), (70, 101))

# Prior strength: how many real outcomes it takes to weigh as much as the
# model's own estimate.
PRIOR_STRENGTH = 30.0


@dataclass(frozen=True)
class BucketStats:
    low: float
    high: float
    wins: int
    total: int

    @property
    def winrate(self) -> float | None:
        return 100.0 * self.wins / self.total if self.total else None


@dataclass(frozen=True)
class Calibration:
    probability: float  # shown, %
    model_probability: float  # raw model estimate, %
    bucket: BucketStats | None

    @property
    def adjusted(self) -> bool:
        return self.bucket is not None and self.bucket.total > 0


def bucket_stats(outcomes: list[tuple[float, bool, int]]) -> list[BucketStats]:
    stats = []
    for low, high in BUCKETS:
        rows = [won for p, won, _ in outcomes if low <= p < high]
        stats.append(BucketStats(low, high, sum(rows), len(rows)))
    return stats


def calibrate(model_probability: float, outcomes: list[tuple[float, bool, int]]) -> Calibration:
    bucket = next(
        (b for b in bucket_stats(outcomes) if b.low <= model_probability < b.high),
        None,
    )
    if bucket is None or bucket.total == 0:
        return Calibration(model_probability, model_probability, bucket)
    shown = (100.0 * bucket.wins + PRIOR_STRENGTH * model_probability) / (bucket.total + PRIOR_STRENGTH)
    # never flip below 50: the direction itself is still the model's best side
    shown = round(max(50.0, shown), 1)
    return Calibration(shown, model_probability, bucket)
