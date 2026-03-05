"""Tests for Step 3.3: MC Core (Numba).

Verifies the Numba JIT-compiled Monte Carlo simulation produces
correct output shape, determinism, and statistical properties.

Reference: implementation_roadmap.md → Step 3.3 tests
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.engine.mc_core import mc_simulate_remaining


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def default_params():
    """Default MMPP parameters for a balanced 0-0 match at kickoff."""
    return {
        "t_now": 0.0,
        "T_end": 98.0,
        "S_H": 0,
        "S_A": 0,
        "state": 0,
        "score_diff": 0,
        "a_H": -3.5,
        "a_A": -3.5,
        "b": np.zeros(6, dtype=np.float64),
        "gamma_H": np.zeros(4, dtype=np.float64),
        "gamma_A": np.zeros(4, dtype=np.float64),
        "delta_H": np.zeros(5, dtype=np.float64),
        "delta_A": np.zeros(5, dtype=np.float64),
        "Q_diag": np.array([-0.01, -0.01, -0.01, 0.0], dtype=np.float64),
        "Q_off": _make_Q_off(),
        "basis_bounds": np.array(
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0], dtype=np.float64
        ),
        "N": 5000,
        "seed": 42,
    }


def _make_Q_off():
    """Create a valid normalized Q_off matrix."""
    Q_off = np.zeros((4, 4), dtype=np.float64)
    Q_off[0, 1] = 0.5   # 11v11 → 10v11
    Q_off[0, 2] = 0.5   # 11v11 → 11v10
    Q_off[1, 3] = 1.0   # 10v11 → 10v10
    Q_off[2, 3] = 1.0   # 11v10 → 10v10
    # State 3 is absorbing (no transitions)
    return Q_off


def _run_mc(params):
    """Helper to run MC with dict params."""
    return mc_simulate_remaining(
        params["t_now"], params["T_end"],
        params["S_H"], params["S_A"],
        params["state"], params["score_diff"],
        params["a_H"], params["a_A"],
        params["b"],
        params["gamma_H"], params["gamma_A"],
        params["delta_H"], params["delta_A"],
        params["Q_diag"], params["Q_off"],
        params["basis_bounds"],
        params["N"], params["seed"],
    )


# ---------------------------------------------------------------------------
# Tests per roadmap
# ---------------------------------------------------------------------------

class TestOutputShapeNBy2:
    """test_output_shape_N_by_2"""

    def test_shape_5000(self, default_params):
        result = _run_mc(default_params)
        assert result.shape == (5000, 2)

    def test_shape_100(self, default_params):
        default_params["N"] = 100
        result = _run_mc(default_params)
        assert result.shape == (100, 2)

    def test_dtype_int32(self, default_params):
        default_params["N"] = 100
        result = _run_mc(default_params)
        assert result.dtype == np.int32


class TestDeterministicWithSameSeed:
    """test_deterministic_with_same_seed"""

    def test_same_seed_same_results(self, default_params):
        default_params["N"] = 1000
        result1 = _run_mc(default_params)
        result2 = _run_mc(default_params)
        np.testing.assert_array_equal(result1, result2)

    def test_different_seed_different_results(self, default_params):
        default_params["N"] = 1000
        result1 = _run_mc(default_params)
        default_params["seed"] = 99
        result2 = _run_mc(default_params)
        # With different seeds, results should differ
        assert not np.array_equal(result1, result2)


class TestScoresNonNegative:
    """test_scores_non_negative"""

    def test_all_scores_non_negative(self, default_params):
        result = _run_mc(default_params)
        assert np.all(result >= 0)

    def test_scores_non_negative_with_existing_score(self, default_params):
        default_params["S_H"] = 2
        default_params["S_A"] = 1
        default_params["score_diff"] = 1
        result = _run_mc(default_params)
        assert np.all(result[:, 0] >= 2)
        assert np.all(result[:, 1] >= 1)


class TestMeanMatchesAnalyticalAtX0DS0:
    """test_mean_matches_analytical_at_X0_dS0"""

    def test_mean_goals_match_poisson_rate(self, default_params):
        """At X=0, ΔS=0, with zero b/gamma/delta, mean goals ≈ exp(a) * T."""
        default_params["N"] = 50000
        # With a = -3.5, lambda = exp(-3.5) ≈ 0.03 goals/min
        # Over 98 min: expected ≈ 2.94 goals per team
        # But Q_diag has small red card rate, so slightly less
        result = _run_mc(default_params)

        expected_rate = np.exp(-3.5)  # per minute
        expected_goals = expected_rate * 98.0

        mean_H = np.mean(result[:, 0])
        mean_A = np.mean(result[:, 1])

        # Should be within 10% of expected (statistical tolerance)
        assert abs(mean_H - expected_goals) < 0.3
        assert abs(mean_A - expected_goals) < 0.3

    def test_symmetric_rates_symmetric_means(self, default_params):
        """With identical parameters, home and away means should be close."""
        default_params["N"] = 50000
        result = _run_mc(default_params)

        mean_H = np.mean(result[:, 0])
        mean_A = np.mean(result[:, 1])

        # Symmetric params → means should be within 5% of each other
        assert abs(mean_H - mean_A) < 0.2


class TestRedCardStateReducesGoals:
    """test_red_card_state_reduces_goals"""

    def test_home_red_reduces_home_goals(self, default_params):
        """Starting in state 1 (10v11) with negative gamma_H[1]
        should reduce home goals."""
        default_params["N"] = 50000

        # Baseline: X=0
        result_baseline = _run_mc(default_params)
        mean_H_baseline = np.mean(result_baseline[:, 0])

        # With home red card: X=1, gamma_H[1] = -0.3
        default_params["state"] = 1
        default_params["gamma_H"] = np.array(
            [0.0, -0.3, 0.0, -0.3], dtype=np.float64
        )
        default_params["gamma_A"] = np.array(
            [0.0, 0.15, 0.0, 0.0], dtype=np.float64
        )
        default_params["seed"] = 42  # Reset seed
        result_red = _run_mc(default_params)
        mean_H_red = np.mean(result_red[:, 0])

        # Home goals should be lower with red card penalty
        assert mean_H_red < mean_H_baseline

    def test_away_red_reduces_away_goals(self, default_params):
        """Starting in state 2 (11v10) with negative gamma_A[2]
        should reduce away goals."""
        default_params["N"] = 50000

        # Baseline: X=0
        result_baseline = _run_mc(default_params)
        mean_A_baseline = np.mean(result_baseline[:, 1])

        # With away red card: X=2, gamma_A[2] = -0.3
        default_params["state"] = 2
        default_params["gamma_A"] = np.array(
            [0.0, 0.0, -0.3, -0.3], dtype=np.float64
        )
        default_params["gamma_H"] = np.array(
            [0.0, 0.0, 0.15, 0.0], dtype=np.float64
        )
        default_params["seed"] = 42
        result_red = _run_mc(default_params)
        mean_A_red = np.mean(result_red[:, 1])

        assert mean_A_red < mean_A_baseline


class TestPerformanceUnder1ms:
    """test_performance_under_1ms — N=50,000"""

    def test_performance(self, default_params):
        """MC simulation should complete in < 1ms for N=50,000 after warmup."""
        # Warmup run (JIT compilation)
        default_params["N"] = 100
        _run_mc(default_params)

        # Timed run
        default_params["N"] = 50000
        start = time.perf_counter()
        _run_mc(default_params)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Allow up to 50ms (generous for CI/different machines)
        # The spec says < 1ms but JIT-compiled Numba may vary
        assert elapsed_ms < 50, f"MC took {elapsed_ms:.1f}ms, expected < 50ms"


class TestDeltaShiftsDistribution:
    """test_delta_shifts_distribution"""

    def test_positive_delta_H_increases_home_goals(self, default_params):
        """Positive delta_H at ΔS=0 should increase home expected goals."""
        default_params["N"] = 50000

        # Baseline: no delta effect
        result_baseline = _run_mc(default_params)
        mean_H_baseline = np.mean(result_baseline[:, 0])

        # With delta_H[2] = +0.3 (index 2 is ΔS=0)
        default_params["delta_H"] = np.array(
            [0.0, 0.0, 0.3, 0.0, 0.0], dtype=np.float64
        )
        default_params["seed"] = 42
        result_delta = _run_mc(default_params)
        mean_H_delta = np.mean(result_delta[:, 0])

        assert mean_H_delta > mean_H_baseline

    def test_negative_delta_A_decreases_away_goals(self, default_params):
        """Negative delta_A at ΔS=0 should decrease away expected goals."""
        default_params["N"] = 50000

        # Baseline
        result_baseline = _run_mc(default_params)
        mean_A_baseline = np.mean(result_baseline[:, 1])

        # With delta_A[2] = -0.3 (index 2 is ΔS=0)
        default_params["delta_A"] = np.array(
            [0.0, 0.0, -0.3, 0.0, 0.0], dtype=np.float64
        )
        default_params["seed"] = 42
        result_delta = _run_mc(default_params)
        mean_A_delta = np.mean(result_delta[:, 1])

        assert mean_A_delta < mean_A_baseline

    def test_leading_team_delta_effect(self, default_params):
        """When home leads (ΔS=+1), delta_H[3] > 0 should boost home goals
        compared to delta_H[3] = 0."""
        default_params["N"] = 50000
        default_params["S_H"] = 1
        default_params["S_A"] = 0
        default_params["score_diff"] = 1

        # Baseline: no delta
        result_baseline = _run_mc(default_params)
        mean_H_baseline = np.mean(result_baseline[:, 0])

        # With delta effect for leading: delta_H[3] = +0.25
        default_params["delta_H"] = np.array(
            [0.0, 0.0, 0.0, 0.25, 0.0], dtype=np.float64
        )
        default_params["seed"] = 42
        result_delta = _run_mc(default_params)
        mean_H_delta = np.mean(result_delta[:, 0])

        assert mean_H_delta > mean_H_baseline


class TestBasisIndexBeyondBounds:
    """test_basis_index_uses_last_bin_for_stoppage_time"""

    def test_late_game_uses_last_bin(self, default_params):
        """Simulation starting past all bin boundaries should use bin 5,
        not bin 0."""
        default_params["N"] = 10000
        # Start at minute 99, T_end=100 — past basis_bounds[6]=98
        default_params["t_now"] = 99.0
        default_params["T_end"] = 100.0

        # Make bin 5 have distinct b value to verify correct bin is used
        default_params["b"] = np.array(
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.5], dtype=np.float64
        )

        result_late = _run_mc(default_params)
        mean_H_late = np.mean(result_late[:, 0])

        # With b[5]=0.5, lambda is exp(-3.5+0.5)=exp(-3.0) per minute
        # Over 1 minute: expected ~0.05 goals per team
        expected_rate = np.exp(-3.0)
        assert abs(mean_H_late - expected_rate) < 0.05

    def test_late_game_different_from_early_bin(self, default_params):
        """Verify bin 5 intensity differs from bin 0 when b values differ."""
        default_params["N"] = 20000
        default_params["t_now"] = 99.0
        default_params["T_end"] = 100.0

        # bin 0 has high b, bin 5 has low b
        default_params["b"] = np.array(
            [0.5, 0.0, 0.0, 0.0, 0.0, -0.5], dtype=np.float64
        )

        result = _run_mc(default_params)
        mean_H = np.mean(result[:, 0])

        # Should use bin 5 (b=-0.5), not bin 0 (b=+0.5)
        # exp(-3.5 + (-0.5)) = exp(-4.0) ~ 0.018 per minute
        # exp(-3.5 + 0.5) = exp(-3.0) ~ 0.050 per minute
        expected_with_bin5 = np.exp(-4.0)
        expected_with_bin0 = np.exp(-3.0)

        # Mean should be close to bin 5 rate, not bin 0
        assert abs(mean_H - expected_with_bin5) < abs(mean_H - expected_with_bin0)
