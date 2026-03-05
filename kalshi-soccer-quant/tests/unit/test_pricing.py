"""Tests for Step 3.4: Hybrid Pricing — True Probability Estimation.

Verifies analytic Poisson/Skellam pricing, MC aggregation,
and hybrid routing logic.

Reference: implementation_roadmap.md → Step 3.4
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import poisson

from src.engine.step_3_4_pricing import (
    PricingResult,
    aggregate_markets,
    analytical_pricing,
    compute_mc_stderr,
    price_analytical,
    price_from_mc,
)


# ---------------------------------------------------------------------------
# Analytic pricing tests
# ---------------------------------------------------------------------------

class TestAnalyticalOverUnder:
    """Analytic over/under pricing matches Poisson CDF."""

    def test_over_25_at_0_0(self):
        """P(over 2.5) at 0-0 with μ_H=1.3, μ_A=1.1."""
        mu_H, mu_A = 1.3, 1.1
        result = analytical_pricing(mu_H, mu_A, (0, 0))

        expected = 1.0 - poisson.cdf(2, mu_H + mu_A)
        assert abs(result["over_25"] - expected) < 1e-10

    def test_over_25_already_3_goals(self):
        """At 2-1 (G=3 > 2), P(over 2.5) = 1.0."""
        result = analytical_pricing(0.5, 0.5, (2, 1))
        assert result["over_25"] == 1.0

    def test_over_15_at_1_0(self):
        """At 1-0 (G=1 = N), need at least 1 more goal."""
        mu_H, mu_A = 0.8, 0.6
        result = analytical_pricing(mu_H, mu_A, (1, 0))

        # Need 0 more remaining goals to be over 1.5?
        # G=1, N=1, remaining_needed = 1-1 = 0
        # P(over) = 1 - P(X=0) where X ~ Poi(1.4)
        expected = 1.0 - poisson.cdf(0, mu_H + mu_A)
        assert abs(result["over_15"] - expected) < 1e-10

    def test_all_over_under_keys_present(self):
        result = analytical_pricing(1.0, 1.0, (0, 0))
        for n in (1, 2, 3, 4, 5):
            assert f"over_{n}5" in result

    def test_over_probabilities_decrease_with_threshold(self):
        """P(over 1.5) > P(over 2.5) > P(over 3.5) > ..."""
        result = analytical_pricing(1.3, 1.1, (0, 0))
        for n in (1, 2, 3, 4):
            assert result[f"over_{n}5"] >= result[f"over_{n+1}5"]


class TestAnalyticalMatchOdds:
    """Analytic match odds from independent Poisson."""

    def test_probabilities_sum_to_one(self):
        result = analytical_pricing(1.5, 1.2, (0, 0))
        total = result["home_win"] + result["draw"] + result["away_win"]
        assert abs(total - 1.0) < 0.01

    def test_stronger_home_favored(self):
        """Higher μ_H should give higher home win probability."""
        result = analytical_pricing(2.0, 0.8, (0, 0))
        assert result["home_win"] > result["away_win"]

    def test_symmetric_gives_equal(self):
        """Equal μ should give equal home/away win probabilities."""
        result = analytical_pricing(1.5, 1.5, (0, 0))
        assert abs(result["home_win"] - result["away_win"]) < 0.01

    def test_existing_score_affects_result(self):
        """At 2-0, home win should be very likely even with low μ."""
        result = analytical_pricing(0.3, 0.3, (2, 0))
        assert result["home_win"] > 0.7

    def test_match_odds_keys_present(self):
        result = analytical_pricing(1.0, 1.0, (0, 0))
        assert "home_win" in result
        assert "draw" in result
        assert "away_win" in result


class TestAnalyticalBTTS:
    """Analytic BTTS pricing."""

    def test_btts_at_0_0(self):
        """BTTS at 0-0 requires both teams to score remaining goals."""
        mu_H, mu_A = 1.5, 1.2
        result = analytical_pricing(mu_H, mu_A, (0, 0))

        p_h_zero = poisson.pmf(0, mu_H)
        p_a_zero = poisson.pmf(0, mu_A)
        expected = (1.0 - p_h_zero) * (1.0 - p_a_zero)

        assert abs(result["btts_yes"] - expected) < 1e-10

    def test_btts_one_team_scored(self):
        """At 1-0, only away needs to score."""
        mu_H, mu_A = 0.5, 0.8
        result = analytical_pricing(mu_H, mu_A, (1, 0))

        p_a_zero = poisson.pmf(0, mu_A)
        expected = 1.0 - p_a_zero
        assert abs(result["btts_yes"] - expected) < 1e-10

    def test_btts_both_scored(self):
        """At 1-1, BTTS is already satisfied."""
        result = analytical_pricing(0.5, 0.5, (1, 1))
        assert result["btts_yes"] == 1.0


# ---------------------------------------------------------------------------
# MC aggregation tests
# ---------------------------------------------------------------------------

class TestAggregateMarkets:
    """MC market aggregation."""

    def test_all_keys_present(self):
        scores = np.array([[1, 0], [2, 1], [0, 0], [1, 1]], dtype=np.int32)
        result = aggregate_markets(scores, (0, 0))

        expected_keys = [
            "over_15", "over_25", "over_35", "over_45", "over_55",
            "home_win", "draw", "away_win", "btts_yes",
        ]
        for key in expected_keys:
            assert key in result

    def test_over_25_count(self):
        """Manual check: total > 2 for each sim."""
        scores = np.array([
            [2, 1],  # total=3 > 2 ✓
            [1, 0],  # total=1 ≤ 2 ✗
            [0, 3],  # total=3 > 2 ✓
            [1, 1],  # total=2 ≤ 2 ✗
        ], dtype=np.int32)

        result = aggregate_markets(scores, (0, 0))
        assert abs(result["over_25"] - 0.5) < 1e-10

    def test_match_odds_count(self):
        scores = np.array([
            [2, 1],  # home win
            [1, 0],  # home win
            [0, 1],  # away win
            [1, 1],  # draw
        ], dtype=np.int32)

        result = aggregate_markets(scores, (0, 0))
        assert abs(result["home_win"] - 0.5) < 1e-10
        assert abs(result["draw"] - 0.25) < 1e-10
        assert abs(result["away_win"] - 0.25) < 1e-10

    def test_btts_count(self):
        scores = np.array([
            [1, 1],  # btts ✓
            [2, 0],  # btts ✗
            [0, 1],  # btts ✗
            [3, 2],  # btts ✓
        ], dtype=np.int32)

        result = aggregate_markets(scores, (0, 0))
        assert abs(result["btts_yes"] - 0.5) < 1e-10

    def test_probabilities_in_range(self):
        np.random.seed(42)
        scores = np.random.randint(0, 5, size=(1000, 2)).astype(np.int32)
        result = aggregate_markets(scores, (0, 0))

        for key, val in result.items():
            assert 0.0 <= val <= 1.0, f"{key}={val} out of range"


class TestMCStdErr:
    """MC standard error computation."""

    def test_stderr_formula(self):
        P_true = {"home_win": 0.5, "draw": 0.25, "away_win": 0.25}
        N = 10000
        se = compute_mc_stderr(P_true, N)

        # Max SE at p=0.5: sqrt(0.25/10000) = 0.005
        expected = np.sqrt(0.5 * 0.5 / N)
        assert abs(se - expected) < 1e-10

    def test_stderr_zero_for_certain(self):
        P_true = {"home_win": 1.0, "draw": 0.0, "away_win": 0.0}
        se = compute_mc_stderr(P_true, 10000)
        assert se == 0.0

    def test_stderr_decreases_with_N(self):
        P_true = {"market": 0.5}
        se_small = compute_mc_stderr(P_true, 1000)
        se_large = compute_mc_stderr(P_true, 100000)
        assert se_large < se_small


# ---------------------------------------------------------------------------
# Hybrid routing tests
# ---------------------------------------------------------------------------

class TestPriceAnalytical:
    """price_analytical returns correct PricingResult."""

    def test_returns_pricing_result(self):
        result = price_analytical(1.3, 1.1, (0, 0))
        assert isinstance(result, PricingResult)
        assert result.pricing_mode == "analytical"
        assert result.sigma_MC == 0.0
        assert len(result.P_true) > 0

    def test_analytical_probabilities_valid(self):
        result = price_analytical(1.5, 1.2, (0, 0))
        total = (result.P_true["home_win"]
                 + result.P_true["draw"]
                 + result.P_true["away_win"])
        assert abs(total - 1.0) < 0.01


class TestPriceFromMC:
    """price_from_mc returns correct PricingResult."""

    def test_returns_mc_mode(self):
        scores = np.random.randint(0, 4, size=(5000, 2)).astype(np.int32)
        result = price_from_mc(scores, (0, 0))
        assert result.pricing_mode == "monte_carlo"
        assert result.sigma_MC > 0

    def test_mc_probabilities_valid(self):
        np.random.seed(42)
        scores = np.random.randint(0, 4, size=(5000, 2)).astype(np.int32)
        result = price_from_mc(scores, (0, 0))
        for val in result.P_true.values():
            assert 0.0 <= val <= 1.0


# ---------------------------------------------------------------------------
# Consistency: analytic vs MC at X=0, ΔS=0
# ---------------------------------------------------------------------------

class TestAnalyticMCConsistency:
    """At X=0, ΔS=0, MC mean should converge to analytic pricing."""

    def test_over_25_convergence(self):
        """MC over 2.5 should match analytic within 2σ."""
        from src.engine.mc_core import mc_simulate_remaining

        mu_H, mu_A = 1.3, 1.1
        a_H = np.log(mu_H / 98.0)
        a_A = np.log(mu_A / 98.0)

        b = np.zeros(6, dtype=np.float64)
        gamma_H = np.zeros(4, dtype=np.float64)
        gamma_A = np.zeros(4, dtype=np.float64)
        delta_H = np.zeros(5, dtype=np.float64)
        delta_A = np.zeros(5, dtype=np.float64)
        Q_diag = np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        Q_off = np.zeros((4, 4), dtype=np.float64)
        bounds = np.array(
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0], dtype=np.float64
        )

        final_scores = mc_simulate_remaining(
            0.0, 98.0, 0, 0, 0, 0,
            a_H, a_A, b, gamma_H, gamma_A,
            delta_H, delta_A, Q_diag, Q_off, bounds,
            100_000, 42,
        )

        mc_result = aggregate_markets(final_scores, (0, 0))
        analytic_result = analytical_pricing(mu_H, mu_A, (0, 0))

        # Within 2% tolerance
        assert abs(mc_result["over_25"] - analytic_result["over_25"]) < 0.02

    def test_match_odds_convergence(self):
        """MC match odds should match analytic within 2%."""
        from src.engine.mc_core import mc_simulate_remaining

        mu_H, mu_A = 1.5, 1.0
        a_H = np.log(mu_H / 98.0)
        a_A = np.log(mu_A / 98.0)

        b = np.zeros(6, dtype=np.float64)
        gamma_H = np.zeros(4, dtype=np.float64)
        gamma_A = np.zeros(4, dtype=np.float64)
        delta_H = np.zeros(5, dtype=np.float64)
        delta_A = np.zeros(5, dtype=np.float64)
        Q_diag = np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        Q_off = np.zeros((4, 4), dtype=np.float64)
        bounds = np.array(
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0], dtype=np.float64
        )

        final_scores = mc_simulate_remaining(
            0.0, 98.0, 0, 0, 0, 0,
            a_H, a_A, b, gamma_H, gamma_A,
            delta_H, delta_A, Q_diag, Q_off, bounds,
            100_000, 42,
        )

        mc_result = aggregate_markets(final_scores, (0, 0))
        analytic_result = analytical_pricing(mu_H, mu_A, (0, 0))

        assert abs(mc_result["home_win"] - analytic_result["home_win"]) < 0.02
        assert abs(mc_result["draw"] - analytic_result["draw"]) < 0.02
        assert abs(mc_result["away_win"] - analytic_result["away_win"]) < 0.02
