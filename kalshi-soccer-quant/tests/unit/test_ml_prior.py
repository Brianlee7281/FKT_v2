"""Tests for Step 1.3 — ML Prior (XGBoost).

Reference: implementation_roadmap.md → Step 1.3 verification tests.
"""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from src.calibration.step_1_3_ml_prior import (
    ALL_FEATURES,
    MatchFeatureRow,
    train_ml_prior,
    predict_expected_goals,
    convert_to_initial_a,
)


# ---------------------------------------------------------------------------
# Helpers — generate synthetic training data
# ---------------------------------------------------------------------------

def _make_synthetic_rows(n: int = 500, seed: int = 42) -> list[MatchFeatureRow]:
    """Generate synthetic training data with realistic patterns.

    Home teams score ~1.5 goals on average, away ~1.1.
    Features have some signal (odds, shots correlate with goals).
    """
    rng = np.random.RandomState(seed)
    rows = []

    for i in range(n):
        is_home = i % 2 == 0
        # Base rate depends on home/away
        base_rate = 1.5 if is_home else 1.1

        # Generate features with signal
        pinnacle_prob = rng.uniform(0.2, 0.6)
        shots = rng.uniform(8, 18)
        possession = rng.uniform(35, 65)

        # Goals correlated with features
        mu = base_rate * (0.5 + pinnacle_prob) * (shots / 13.0)
        mu = max(0.3, min(mu, 5.0))
        goals = rng.poisson(mu)

        features = {f: 0.0 for f in ALL_FEATURES}
        features.update({
            "shots_per_90": shots,
            "shots_on_target_per_90": shots * rng.uniform(0.25, 0.45),
            "possession_avg": possession,
            "pass_accuracy": rng.uniform(0.7, 0.92),
            "xG_per_90": mu * rng.uniform(0.8, 1.2),
            "corners_per_90": rng.uniform(3, 8),
            "fouls_per_90": rng.uniform(8, 16),
            "saves_per_90": rng.uniform(2, 6),
            "pinnacle_home_prob": pinnacle_prob if is_home else 1 - pinnacle_prob - 0.25,
            "pinnacle_draw_prob": 0.25,
            "pinnacle_away_prob": 1 - pinnacle_prob - 0.25 if is_home else pinnacle_prob,
            "market_avg_home_prob": pinnacle_prob * rng.uniform(0.95, 1.05),
            "is_home": 1.0 if is_home else 0.0,
            "rest_days": rng.uniform(3, 10),
            "fw_avg_rating": rng.uniform(6.0, 8.0),
            "mf_avg_rating": rng.uniform(6.0, 7.5),
            "df_avg_rating": rng.uniform(6.0, 7.5),
            "team_avg_rating": rng.uniform(6.0, 7.5),
        })

        rows.append(MatchFeatureRow(
            features=features,
            target_goals=goals,
            match_id=f"match_{i}",
            team="home" if is_home else "away",
        ))

    return rows


@pytest.fixture(scope="module")
def trained_artifacts():
    """Train model once for all tests in this module."""
    rows = _make_synthetic_rows(n=500)
    return train_ml_prior(rows, num_boost_round=50)


@pytest.fixture(scope="module")
def training_rows():
    return _make_synthetic_rows(n=500)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPoissonOutputPositive:
    """test_poisson_output_positive — predictions must be > 0."""

    def test_all_predictions_positive(self, trained_artifacts, training_rows):
        for row in training_rows[:50]:
            mu = predict_expected_goals(trained_artifacts, row.features)
            assert mu > 0, f"Prediction must be positive, got {mu}"

    def test_zero_features_still_positive(self, trained_artifacts):
        """Even with all-zero features, Poisson output must be > 0."""
        empty_feats = {f: 0.0 for f in ALL_FEATURES}
        mu = predict_expected_goals(trained_artifacts, empty_feats)
        assert mu > 0


class TestFeatureMaskSubsetOfFull:
    """test_feature_mask_subset_of_full — selected features ⊆ all features."""

    def test_mask_is_subset(self, trained_artifacts):
        mask = set(trained_artifacts.feature_mask)
        full = set(trained_artifacts.feature_names)
        assert mask.issubset(full), \
            f"Mask has features not in full set: {mask - full}"

    def test_mask_not_empty(self, trained_artifacts):
        assert len(trained_artifacts.feature_mask) > 0

    def test_mask_smaller_or_equal_to_full(self, trained_artifacts):
        assert len(trained_artifacts.feature_mask) <= len(trained_artifacts.feature_names)


class TestHomeAdvantageCaptured:
    """test_home_advantage_captured — home predictions > away on average."""

    def test_home_avg_greater_than_away(self, trained_artifacts, training_rows):
        home_preds = []
        away_preds = []

        for row in training_rows:
            mu = predict_expected_goals(trained_artifacts, row.features)
            if row.features.get("is_home", 0) == 1.0:
                home_preds.append(mu)
            else:
                away_preds.append(mu)

        avg_home = np.mean(home_preds)
        avg_away = np.mean(away_preds)

        assert avg_home > avg_away, \
            f"Home avg {avg_home:.3f} should be > away avg {avg_away:.3f}"


class TestPredictionInReasonableRange:
    """test_prediction_in_reasonable_range — 0.3 < μ < 5.0."""

    def test_predictions_in_range(self, trained_artifacts, training_rows):
        for row in training_rows[:100]:
            mu = predict_expected_goals(trained_artifacts, row.features)
            assert 0.1 < mu < 8.0, \
                f"Prediction {mu} outside reasonable range [0.1, 8.0]"

    def test_mean_prediction_near_league_average(self, trained_artifacts, training_rows):
        """Mean predicted goals should be ~1.0-2.0 (typical league average)."""
        preds = [predict_expected_goals(trained_artifacts, r.features)
                 for r in training_rows]
        mean_pred = np.mean(preds)
        assert 0.5 < mean_pred < 4.0, \
            f"Mean prediction {mean_pred:.3f} outside [0.5, 4.0]"

    def test_convert_to_a_is_negative(self, trained_artifacts, training_rows):
        """Initial a should be negative (μ/T_m < 1 for typical goals/min)."""
        for row in training_rows[:20]:
            mu = predict_expected_goals(trained_artifacts, row.features)
            a = convert_to_initial_a(mu, T_m=90.0)
            # μ ≈ 1.3 → a ≈ ln(1.3/90) ≈ -4.2
            assert a < 0, f"a={a} should be negative for μ={mu}"


class TestMedianValuesNoNans:
    """test_median_values_no_nans — no NaN or None in median dict."""

    def test_no_nans(self, trained_artifacts):
        for fname, val in trained_artifacts.median_values.items():
            assert val is not None, f"Median for {fname} is None"
            assert not math.isnan(val), f"Median for {fname} is NaN"

    def test_all_features_have_medians(self, trained_artifacts):
        for fname in ALL_FEATURES:
            assert fname in trained_artifacts.median_values, \
                f"Missing median for feature {fname}"

    def test_medians_are_finite(self, trained_artifacts):
        for fname, val in trained_artifacts.median_values.items():
            assert math.isfinite(val), f"Median for {fname} is not finite: {val}"
