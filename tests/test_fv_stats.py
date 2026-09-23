from __future__ import annotations

import numpy as np

from pmcore import stats
from strategies.ref_mm.research import fv

M = 1_000_000


def test_fv_worked_example() -> None:
    t = 1000 * M
    ps = fv.PrintSeries.from_arrays(
        np.array([t - 90 * M, t - 30 * M], np.int64),
        np.array([4700, 4800], np.int64),
        np.array([-1, 1], np.int64),
    )
    out = fv.fv_x2_at(ps, np.array([t], np.int64))
    assert out[0] == 9500 and fv.fv_ticks_decimal_str(9500) == "0.47500"
    # sell too old (11 minutes): buy only
    ps2 = fv.PrintSeries.from_arrays(
        np.array([t - 660 * M, t - 30 * M], np.int64),
        np.array([4700, 4800], np.int64),
        np.array([-1, 1], np.int64),
    )
    assert fv.fv_x2_at(ps2, np.array([t], np.int64))[0] == 9600
    # nothing before t - L_ref
    assert fv.fv_x2_at(ps, np.array([t - 100 * M], np.int64))[0] == fv.NO_FV


def test_fv_lag_excludes_prints_within_2s() -> None:
    t = 1000 * M
    ps = fv.PrintSeries.from_arrays(
        np.array([t - 1 * M], np.int64), np.array([5000], np.int64), np.array([1], np.int64)
    )
    assert fv.fv_x2_at(ps, np.array([t], np.int64))[0] == fv.NO_FV
    assert fv.fv_x2_at(ps, np.array([t + 2 * M], np.int64))[0] == 10000


def test_fv_unsorted_input_is_sorted() -> None:
    t = 1000 * M
    ps = fv.PrintSeries.from_arrays(
        np.array([t - 30 * M, t - 90 * M], np.int64),
        np.array([4800, 4600], np.int64),
        np.array([1, 1], np.int64),
    )
    assert fv.fv_x2_at(ps, np.array([t], np.int64))[0] == 9600  # latest buy is 4800


def test_bootstrap_worked_example() -> None:
    values = np.array([2.0, -1.0, 4.0])
    weights = np.array([100.0, 50.0, 50.0])
    blocks = np.array([1, 2, 3])
    r = stats.block_bootstrap_mean(values, weights, blocks, n_resamples=2000)
    assert abs(r.mean - 1.75) < 1e-9
    assert r.ci_low <= r.mean <= r.ci_high and r.n_blocks == 3


def test_bootstrap_ci_tightens_with_blocks() -> None:
    rng = np.random.default_rng(0)
    v = rng.normal(1.0, 5.0, size=2000)
    w = np.ones_like(v)
    r_few = stats.block_bootstrap_mean(v, w, np.repeat(np.arange(20), 100), n_resamples=1000)
    r_many = stats.block_bootstrap_mean(v, w, np.arange(2000), n_resamples=1000)
    assert (r_many.ci_high - r_many.ci_low) < (r_few.ci_high - r_few.ci_low) * 1.5


def test_mde_example() -> None:
    assert abs(stats.minimum_detectable_effect(8.0, 400) - 1.12) < 0.01
    assert abs(stats._z(0.975) - 1.95996) < 1e-4
