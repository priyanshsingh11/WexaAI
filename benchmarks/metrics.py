"""Latency statistics.

Percentiles are computed by the **nearest-rank** method: the p-th percentile of n sorted
samples is the value at index ceil(p/100 * n) - 1. This is stated explicitly because
percentile definitions genuinely differ -- numpy's default linear interpolation reports a
different p95 than nearest-rank on the same data, and a benchmark that does not say which
one it used cannot be checked. Nearest-rank always returns an actually-observed value,
never an interpolated one that no request experienced.

Every raw sample is also written to results/raw/, so any reviewer preferring a different
definition can recompute from the same data.
"""

from __future__ import annotations

import math
import statistics
from typing import Iterable, Sequence


def percentile(sorted_samples: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty sequence."""
    if not sorted_samples:
        raise ValueError("percentile of empty sample set")
    if p <= 0:
        return sorted_samples[0]
    if p >= 100:
        return sorted_samples[-1]
    rank = math.ceil(p / 100.0 * len(sorted_samples))
    return sorted_samples[max(0, min(len(sorted_samples) - 1, rank - 1))]


def summarize(samples: Iterable[float]) -> dict[str, float | int | None]:
    """Full latency distribution for one workload on one database.

    Reports the whole shape, not just an average: the assignment explicitly asks for
    percentiles rather than means, and on a 0.5 vCPU burstable instance the tail is where
    throttling and queueing actually show up.
    """
    values = sorted(samples)
    if not values:
        return {"n": 0, "min_ms": None, "mean_ms": None, "p50_ms": None, "p90_ms": None,
                "p95_ms": None, "p99_ms": None, "max_ms": None, "stdev_ms": None}
    return {
        "n": len(values),
        "min_ms": round(values[0], 3),
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(percentile(values, 50), 3),
        "p90_ms": round(percentile(values, 90), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "p99_ms": round(percentile(values, 99), 3),
        "max_ms": round(values[-1], 3),
        "stdev_ms": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }
