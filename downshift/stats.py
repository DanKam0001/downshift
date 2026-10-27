"""Bootstrap confidence intervals for held-out scores, plain Python.

Rows are resampled with replacement (a fixed seed, so the same scores always give the same interval).
For a comparison, the same resampled rows are used for both sides (paired bootstrap), which removes the
row-difficulty variance that both models share.
"""
from __future__ import annotations

import random

SEED = 20261027
N_BOOT = 4000


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _boot_means(values: list[float], n_boot: int, seed: int) -> list[float]:
    n = len(values)
    rng = random.Random(seed)
    out = []
    for _ in range(n_boot):
        out.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    out.sort()
    return out


def bootstrap_ci(scores: list[float], n_boot: int = N_BOOT, seed: int = SEED, level: float = 0.95) -> dict:
    """{"mean", "lo", "hi", "n"} for the mean of per-row scores."""
    if not scores:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "n": 0}
    means = _boot_means(list(scores), n_boot, seed)
    a = (1 - level) / 2
    return {"mean": round(sum(scores) / len(scores), 4), "lo": round(_percentile(means, a), 4),
            "hi": round(_percentile(means, 1 - a), 4), "n": len(scores)}


def paired_diff_ci(a: list[float], b: list[float], n_boot: int = N_BOOT, seed: int = SEED,
                   level: float = 0.95) -> dict:
    """CI for mean(a) - mean(b) where a[i] and b[i] are scores on the same row."""
    if len(a) != len(b):
        raise ValueError("paired scores must be on the same rows")
    diffs = [x - y for x, y in zip(a, b)]
    r = bootstrap_ci(diffs, n_boot, seed, level)
    return {"diff": r["mean"], "lo": r["lo"], "hi": r["hi"], "n": r["n"],
            "rows_better": sum(1 for d in diffs if d > 1e-9), "rows_worse": sum(1 for d in diffs if d < -1e-9)}


def matches(diff: dict, tolerance: float) -> bool:
    """A cheaper option matches the reference when the CI lower bound is above -tolerance."""
    return diff["lo"] > -tolerance + 1e-12


def beats(diff: dict) -> bool:
    return diff["lo"] > 1e-12
