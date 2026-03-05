"""Tests for Step 2.2 (Feature Selection) and Step 2.3 (a Parameter).

Verifies feature mask application, median imputation, C_time computation,
and the back-solving formula a = ln(mu) - ln(C_time).
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.common.types import PreMatchData
from src.prematch.step_2_2_feature_selection import (
    apply_feature_mask,
    apply_feature_mask_both_teams,
    build_full_feature_vector,
)
from src.prematch.step_2_3_a_parameter import (
    A_MAX,
    A_MIN,
    AParameterResult,
    back_solve_a,
    compute_C_time,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prematch_data() -> PreMatchData:
    """Create a minimal PreMatchData for testing."""
    return PreMatchData(
        home_starting_11=["101", "102"],
        away_starting_11=["201", "202"],
        home_formation="4-3-3",
        away_formation="4-4-2",
        home_player_agg={
            "fw_avg_rating": 7.2,
            "fw_goals_p90": 0.5,
            "fw_key_passes_p90": 1.0,
            "mf_avg_rating": 6.8,
            "mf_key_passes_p90": 2.0,
            "mf_pass_accuracy": 0.82,
            "df_avg_rating": 6.5,
            "df_tackles_p90": 3.0,
            "df_interceptions_p90": 2.5,
            "gk_save_rate": 0.72,
            "team_avg_rating": 6.9,
        },
        away_player_agg={
            "fw_avg_rating": 7.0,
            "fw_goals_p90": 0.4,
            "fw_key_passes_p90": 0.8,
            "mf_avg_rating": 6.6,
            "mf_key_passes_p90": 1.5,
            "mf_pass_accuracy": 0.78,
            "df_avg_rating": 6.4,
            "df_tackles_p90": 2.8,
            "df_interceptions_p90": 2.2,
            "gk_save_rate": 0.70,
            "team_avg_rating": 6.7,
        },
        home_team_rolling={
            "xG_per_90": 1.8,
            "xGA_per_90": 1.1,
            "shots_per_90": 14.0,
            "shots_on_target_per_90": 5.0,
            "shots_insidebox_ratio": 0.6,
            "possession_avg": 55.0,
            "pass_accuracy": 0.85,
            "corners_per_90": 6.0,
            "fouls_per_90": 10.0,
            "saves_per_90": 3.0,
        },
        away_team_rolling={
            "xG_per_90": 1.5,
            "xGA_per_90": 1.3,
            "shots_per_90": 12.0,
            "shots_on_target_per_90": 4.0,
            "shots_insidebox_ratio": 0.5,
            "possession_avg": 45.0,
            "pass_accuracy": 0.80,
            "corners_per_90": 5.0,
            "fouls_per_90": 12.0,
            "saves_per_90": 4.0,
        },
        odds_features={
            "pinnacle_home_prob": 0.45,
            "pinnacle_draw_prob": 0.28,
            "pinnacle_away_prob": 0.27,
            "market_avg_home_prob": 0.44,
            "market_avg_draw_prob": 0.29,
            "market_avg_away_prob": 0.27,
            "bookmaker_odds_std": 0.02,
            "n_bookmakers": 15.0,
        },
        home_rest_days=5,
        away_rest_days=3,
        h2h_goal_diff=0.4,
        match_id="12345",
        kickoff_time="15:00",
    )


# ---------------------------------------------------------------------------
# Step 2.2: Feature Selection Tests
# ---------------------------------------------------------------------------

class TestBuildFullFeatureVector:
    def test_contains_all_tier_prefixes(self):
        pm = _make_prematch_data()
        vec = build_full_feature_vector(pm)

        # Tier 1 home/away
        assert "home_xG_per_90" in vec
        assert "away_xG_per_90" in vec

        # Tier 2 home/away
        assert "home_fw_avg_rating" in vec
        assert "away_fw_avg_rating" in vec

        # Tier 3 (no prefix)
        assert "pinnacle_home_prob" in vec

        # Tier 4
        assert "is_home" in vec
        assert "rest_days" in vec
        assert "opp_rest_days" in vec
        assert "h2h_goal_diff" in vec

    def test_is_home_always_one(self):
        pm = _make_prematch_data()
        vec = build_full_feature_vector(pm)
        assert vec["is_home"] == 1.0

    def test_no_internal_odds_fields(self):
        pm = _make_prematch_data()
        pm.odds_features["_pinnacle_raw"] = (0.45, 0.28, 0.27)
        vec = build_full_feature_vector(pm)
        assert "_pinnacle_raw" not in vec


class TestApplyFeatureMask:
    def test_returns_correct_length(self):
        pm = _make_prematch_data()
        mask = ["home_xG_per_90", "pinnacle_home_prob", "rest_days"]
        medians = {"home_xG_per_90": 1.5, "pinnacle_home_prob": 0.4, "rest_days": 4.0}

        result = apply_feature_mask(pm, mask, medians)
        assert len(result) == 3

    def test_values_match_prematch_data(self):
        pm = _make_prematch_data()
        mask = ["home_xG_per_90", "pinnacle_home_prob"]
        medians = {"home_xG_per_90": 1.5, "pinnacle_home_prob": 0.4}

        result = apply_feature_mask(pm, mask, medians)
        assert result[0] == pytest.approx(1.8)  # home_xG_per_90
        assert result[1] == pytest.approx(0.45)  # pinnacle_home_prob

    def test_missing_feature_uses_median(self):
        pm = _make_prematch_data()
        mask = ["home_xG_per_90", "nonexistent_feature"]
        medians = {"home_xG_per_90": 1.5, "nonexistent_feature": 99.0}

        result = apply_feature_mask(pm, mask, medians)
        assert result[1] == pytest.approx(99.0)

    def test_preserves_mask_order(self):
        pm = _make_prematch_data()
        mask = ["pinnacle_home_prob", "home_xG_per_90", "rest_days"]
        medians = {k: 0.0 for k in mask}

        result = apply_feature_mask(pm, mask, medians)
        assert result[0] == pytest.approx(0.45)  # pinnacle first
        assert result[1] == pytest.approx(1.8)    # xG second
        assert result[2] == pytest.approx(5.0)    # rest_days third

    def test_empty_mask_returns_empty(self):
        pm = _make_prematch_data()
        result = apply_feature_mask(pm, [], {})
        assert len(result) == 0


class TestApplyFeatureMaskBothTeams:
    def test_home_away_swap_xg(self):
        pm = _make_prematch_data()
        mask = ["home_xG_per_90", "away_xG_per_90"]
        medians = {k: 0.0 for k in mask}

        X_home, X_away = apply_feature_mask_both_teams(pm, mask, medians)

        # Home perspective: home=1.8, away=1.5
        assert X_home[0] == pytest.approx(1.8)
        assert X_home[1] == pytest.approx(1.5)

        # Away perspective: home=1.5 (was away), away=1.8 (was home)
        assert X_away[0] == pytest.approx(1.5)
        assert X_away[1] == pytest.approx(1.8)

    def test_odds_swap(self):
        pm = _make_prematch_data()
        mask = ["pinnacle_home_prob", "pinnacle_away_prob"]
        medians = {k: 0.0 for k in mask}

        X_home, X_away = apply_feature_mask_both_teams(pm, mask, medians)

        assert X_home[0] == pytest.approx(0.45)  # home prob for home
        assert X_away[0] == pytest.approx(0.27)  # away prob becomes "home" for away

    def test_h2h_negated_for_away(self):
        pm = _make_prematch_data()
        mask = ["h2h_goal_diff"]
        medians = {"h2h_goal_diff": 0.0}

        X_home, X_away = apply_feature_mask_both_teams(pm, mask, medians)

        assert X_home[0] == pytest.approx(0.4)
        assert X_away[0] == pytest.approx(-0.4)


# ---------------------------------------------------------------------------
# Step 2.3: a Parameter Tests
# ---------------------------------------------------------------------------

class TestComputeCTime:
    def test_uniform_b_zero(self):
        """When all b=0, C_time = sum of delta_t = T_exp."""
        b = np.zeros(6)
        C_time = compute_C_time(b, E_alpha_1=3.0, E_alpha_2=5.0)
        T_exp = 90.0 + 3.0 + 5.0  # 98 minutes
        assert C_time == pytest.approx(T_exp)

    def test_positive_b_increases_C_time(self):
        """Positive b values should increase C_time beyond T_exp."""
        b = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
        C_time = compute_C_time(b, E_alpha_1=3.0, E_alpha_2=5.0)
        T_exp = 98.0
        assert C_time > T_exp

    def test_expected_calculation(self):
        """Verify exact calculation with known values."""
        b = np.array([0.0, 0.05, 0.1, 0.0, -0.05, 0.15])
        E_a1, E_a2 = 3.0, 5.0
        dt = np.array([15.0, 15.0, 18.0, 15.0, 15.0, 20.0])

        expected = np.sum(np.exp(b) * dt)
        result = compute_C_time(b, E_a1, E_a2)
        assert result == pytest.approx(expected)

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError, match="Expected 6"):
            compute_C_time(np.zeros(5))


class TestBackSolveA:
    def test_basic_back_solve(self):
        """a = ln(mu) - ln(C_time) for reasonable values."""
        mu = 1.5
        C_time = 98.0
        a = back_solve_a(mu, C_time)
        expected = math.log(1.5) - math.log(98.0)
        assert a == pytest.approx(expected)

    def test_round_trip_with_C_time(self):
        """Verify mu = exp(a) * C_time round-trips correctly."""
        b = np.zeros(6)
        C_time = compute_C_time(b, 3.0, 5.0)  # 98.0
        mu_original = 1.8

        a = back_solve_a(mu_original, C_time)
        mu_recovered = math.exp(a) * C_time
        assert mu_recovered == pytest.approx(mu_original)

    def test_clamped_to_bounds(self):
        """Very low mu should be clamped to A_MIN."""
        a = back_solve_a(0.001, 98.0)
        assert a >= A_MIN

        a = back_solve_a(1000.0, 1.0)
        assert a <= A_MAX

    def test_zero_mu_returns_a_min(self):
        assert back_solve_a(0.0, 98.0) == A_MIN

    def test_zero_C_time_returns_a_min(self):
        assert back_solve_a(1.5, 0.0) == A_MIN

    def test_a_in_reasonable_range(self):
        """For typical EPL matches (mu ~1.0-2.0), a should be roughly -4 to -2."""
        C_time = 98.0
        for mu in [0.8, 1.0, 1.5, 2.0, 2.5]:
            a = back_solve_a(mu, C_time)
            assert A_MIN <= a <= A_MAX, f"a={a} out of range for mu={mu}"

    def test_higher_mu_gives_higher_a(self):
        """More goals expected -> higher baseline intensity."""
        C_time = 98.0
        a_low = back_solve_a(1.0, C_time)
        a_high = back_solve_a(2.0, C_time)
        assert a_high > a_low
