"""Statistics on already-computed per-contract returns. Floats are allowed here and only here
(CLAUDE.md conventions): inputs are returns in ticks, never money to be settled.

Block bootstrap by event (PREREGISTRATION section 8): each event is one block; resample events
with replacement 10,000 times; the statistic is the weighted mean; the 95% CI is the 2.5th and
97.5th percentiles of the resampled statistics. Seed fixed at 20240101.

Worked example: three events with contract-weighted mean markouts +2, -1, +4 ticks and weights
100, 50, 50 contracts. Pooled mean = (2 * 100 + (-1) * 50 + 4 * 50) / 200 = 1.75 ticks. A
resample drawing events (1, 1, 3) gives (2 * 100 + 2 * 100 + 4 * 50) / 250 = 2.4 ticks.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

SEED = 20240101
N_RESAMPLES = 10_000


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    mean: float
    ci_low: float
    ci_high: float
    n_obs: int
    n_blocks: int
    total_weight: float
    sd: float
    n_resamples: int


def weighted_mean(values: NDArray[np.float64], weights: NDArray[np.float64]) -> float:
    w = float(weights.sum())
    if w <= 0:
        raise ValueError("total weight must be positive")
    return float((values * weights).sum() / w)


def block_bootstrap_mean(
    values: NDArray[np.float64],
    weights: NDArray[np.float64],
    block_ids: NDArray[np.int64],
    n_resamples: int = N_RESAMPLES,
    seed: int = SEED,
) -> BootstrapResult:
    values = np.asarray(values, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    block_ids = np.asarray(block_ids)
    if not (values.shape == weights.shape == block_ids.shape):
        raise ValueError("values, weights, block_ids must have the same shape")
    if values.size == 0:
        raise ValueError("no observations")
    uniq, inv = np.unique(block_ids, return_inverse=True)
    nb = uniq.size
    # Per-block sums so each resample is O(blocks), not O(observations).
    block_wsum = np.bincount(inv, weights=weights, minlength=nb)
    block_vsum = np.bincount(inv, weights=values * weights, minlength=nb)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, nb, size=(n_resamples, nb))
    counts = np.apply_along_axis(lambda row: np.bincount(row, minlength=nb), 1, draws)
    num = counts @ block_vsum
    den = counts @ block_wsum
    stats = num / den
    lo, hi = np.percentile(stats, [2.5, 97.5])
    mean = weighted_mean(values, weights)
    sd = float(np.sqrt(np.average((values - mean) ** 2, weights=weights)))
    return BootstrapResult(
        mean, float(lo), float(hi), int(values.size), int(nb), float(weights.sum()), sd, n_resamples
    )


def minimum_detectable_effect(
    sd: float, n_effective: int, power: float = 0.8, alpha: float = 0.05
) -> float:
    """Two-sided z-test MDE = (z_{1-alpha/2} + z_{power}) * sd / sqrt(n).

    Example: sd 8 ticks, 400 effective observations: (1.96 + 0.84) * 8 / 20 = 1.12 ticks.
    """
    from math import sqrt

    z_alpha = _z(1 - alpha / 2)
    z_pow = _z(power)
    if n_effective <= 0:
        raise ValueError("n_effective must be positive")
    return (z_alpha + z_pow) * sd / sqrt(n_effective)


def _z(p: float) -> float:
    """Inverse standard normal CDF (Acklam's approximation, |error| < 1.2e-9)."""
    from math import log, sqrt

    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = sqrt(-2 * log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = sqrt(-2 * log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )
