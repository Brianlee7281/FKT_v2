"""Tests for Step 1.5 — Validation.

Reference: implementation_roadmap.md → Step 1.5 verification.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from src.calibration.step_1_4_nll import TrainingResult
from src.calibration.step_1_5_validation import (
    FoldResult,
    GoNoGoReport,
    SignValidationResult,
    brier_score,
    calibration_bins,
    calibration_max_deviation,
    delta_brier_score,
    delta_likelihood_ratio_test,
    evaluate_go_no_go,
    log_loss,
    poisson_btts,
    poisson_match_winner_probs,
    poisson_over_under,
    save_production_params,
    simulate_pnl,
    validate_b_half_ratio,
    validate_gamma_signs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_training_result(
    gamma_H: tuple[float, float] = (-0.3, 0.2),
    gamma_A: tuple[float, float] = (0.2, -0.3),
    delta_H: tuple = (0.1, 0.05, -0.05, -0.1),
    delta_A: tuple = (-0.1, -0.05, 0.05, 0.1),
    b: tuple = (0.0, 0.0, 0.05, 0.0, 0.05, 0.1),
) -> TrainingResult:
    return TrainingResult(
        b=np.array(b),
        gamma_H=np.array(gamma_H),
        gamma_A=np.array(gamma_A),
        delta_H=np.array(delta_H),
        delta_A=np.array(delta_A),
        a_H=np.array([-3.5, -3.4]),
        a_A=np.array([-3.6, -3.5]),
        final_loss=1000.0,
        loss_history=[1200.0, 1100.0, 1000.0],
    )


# ---------------------------------------------------------------------------
# Brier Score
# ---------------------------------------------------------------------------

class TestBrierScore:
    def test_perfect_prediction(self):
        preds = np.array([1.0, 0.0, 1.0])
        outcomes = np.array([1.0, 0.0, 1.0])
        assert brier_score(preds, outcomes) == pytest.approx(0.0)

    def test_worst_prediction(self):
        preds = np.array([0.0, 1.0])
        outcomes = np.array([1.0, 0.0])
        assert brier_score(preds, outcomes) == pytest.approx(1.0)

    def test_delta_bs_negative_when_model_better(self):
        outcomes = np.array([1, 0, 1, 0, 1])
        model = np.array([0.9, 0.1, 0.8, 0.2, 0.9])
        market = np.array([0.6, 0.4, 0.6, 0.4, 0.6])
        assert delta_brier_score(model, market, outcomes) < 0


class TestLogLoss:
    def test_perfect_prediction_near_zero(self):
        preds = np.array([0.999, 0.001])
        outcomes = np.array([1.0, 0.0])
        assert log_loss(preds, outcomes) < 0.01

    def test_bad_prediction_high(self):
        preds = np.array([0.1, 0.9])
        outcomes = np.array([1.0, 0.0])
        assert log_loss(preds, outcomes) > 1.0


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

class TestCalibration:
    def test_perfect_calibration_zero_deviation(self):
        # Predictions exactly match frequencies
        rng = np.random.RandomState(42)
        n = 10000
        preds = rng.uniform(0, 1, n)
        outcomes = (rng.uniform(0, 1, n) < preds).astype(float)
        dev = calibration_max_deviation(preds, outcomes, n_bins=10)
        assert dev < 0.05  # Should be close to 0 with enough data

    def test_bins_have_correct_structure(self):
        preds = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
        outcomes = np.array([0, 0, 1, 1, 1])
        bins = calibration_bins(preds, outcomes, n_bins=5)
        for b in bins:
            assert "bin_center" in b
            assert "mean_predicted" in b
            assert "mean_observed" in b
            assert "count" in b


# ---------------------------------------------------------------------------
# Multi-market Poisson
# ---------------------------------------------------------------------------

class TestPoissonMarkets:
    def test_match_winner_probs_sum_to_one(self):
        probs = poisson_match_winner_probs(1.5, 1.2)
        total = probs["home"] + probs["draw"] + probs["away"]
        assert total == pytest.approx(1.0, abs=0.001)

    def test_home_favored_with_higher_mu(self):
        probs = poisson_match_winner_probs(2.5, 0.8)
        assert probs["home"] > probs["away"]

    def test_over_25_reasonable(self):
        # μ_total=2.7 → P(over 2.5) ≈ 0.5
        p = poisson_over_under(1.5, 1.2, threshold=2.5)
        assert 0.3 < p < 0.7

    def test_btts_increases_with_higher_mu(self):
        p_low = poisson_btts(0.5, 0.5)
        p_high = poisson_btts(2.0, 2.0)
        assert p_high > p_low

    def test_btts_in_valid_range(self):
        p = poisson_btts(1.3, 1.1)
        assert 0.0 < p < 1.0


# ---------------------------------------------------------------------------
# Gamma / delta sign validation
# ---------------------------------------------------------------------------

class TestGammaSignValidation:
    def test_correct_signs_all_pass(self):
        result = _make_training_result(
            gamma_H=(-0.3, 0.2),
            gamma_A=(0.2, -0.3),
        )
        sv = validate_gamma_signs(result)
        assert sv.all_gamma_correct

    def test_wrong_gamma_H_1_fails(self):
        result = _make_training_result(gamma_H=(0.1, 0.2))  # γ^H_1 > 0 wrong
        sv = validate_gamma_signs(result)
        assert not sv.gamma_H_1_correct
        assert not sv.all_gamma_correct


# ---------------------------------------------------------------------------
# Delta LRT
# ---------------------------------------------------------------------------

class TestDeltaLRT:
    def test_significant_when_large_improvement(self):
        # Big improvement in NLL
        result = delta_likelihood_ratio_test(
            nll_with_delta=900.0,
            nll_without_delta=950.0,
            df=8,
        )
        assert result["significant"]
        assert result["p_value"] < 0.05

    def test_not_significant_when_similar(self):
        result = delta_likelihood_ratio_test(
            nll_with_delta=999.0,
            nll_without_delta=1000.0,
            df=8,
        )
        assert not result["significant"]

    def test_lr_statistic_non_negative(self):
        result = delta_likelihood_ratio_test(950.0, 900.0, df=8)
        assert result["lr_statistic"] >= 0


# ---------------------------------------------------------------------------
# b half-ratio
# ---------------------------------------------------------------------------

class TestBHalfRatio:
    def test_equal_b_gives_half_ratio_near_50(self):
        b = np.zeros(6)
        result = validate_b_half_ratio(b)
        assert result["model_h1_ratio"] == pytest.approx(0.5)

    def test_within_tolerance(self):
        b = np.array([0.0, 0.0, 0.05, 0.0, 0.05, 0.1])
        result = validate_b_half_ratio(b, empirical_h1_ratio=0.48)
        assert result["within_tolerance"]

    def test_outside_tolerance(self):
        b = np.array([0.5, 0.5, 0.5, -0.5, -0.5, -0.5])
        result = validate_b_half_ratio(b, empirical_h1_ratio=0.5, tolerance=0.05)
        assert not result["within_tolerance"]


# ---------------------------------------------------------------------------
# Simulation P&L
# ---------------------------------------------------------------------------

class TestSimulatePnl:
    def test_no_edge_no_trades(self):
        model = np.array([0.5, 0.5, 0.5])
        market = np.array([0.5, 0.5, 0.5])
        outcomes = np.array([1, 0, 1])
        result = simulate_pnl(model, market, outcomes, theta_entry=0.02)
        assert result["total_trades"] == 0

    def test_returns_expected_keys(self):
        model = np.array([0.7, 0.3, 0.8])
        market = np.array([0.5, 0.5, 0.5])
        outcomes = np.array([1, 0, 1])
        result = simulate_pnl(model, market, outcomes)
        assert "total_pnl" in result
        assert "max_drawdown_pct" in result
        assert "total_trades" in result

    def test_max_drawdown_non_negative(self):
        rng = np.random.RandomState(42)
        model = rng.uniform(0.3, 0.9, 100)
        market = rng.uniform(0.3, 0.7, 100)
        outcomes = (rng.random(100) < 0.5).astype(float)
        result = simulate_pnl(model, market, outcomes)
        assert result["max_drawdown_pct"] >= 0


# ---------------------------------------------------------------------------
# Go/No-Go
# ---------------------------------------------------------------------------

class TestGoNoGo:
    def test_all_pass(self):
        folds = [
            FoldResult(
                fold_idx=0,
                train_seasons=["2020", "2021", "2022"],
                val_seasons=["2023"],
                brier_score_model=0.20,
                brier_score_pinnacle=0.22,
                delta_bs=-0.02,
                calibration_max_dev=0.03,
                sim_pnl={"total_pnl": 100.0, "max_drawdown_pct": 10.0},
            ),
        ]
        result = _make_training_result()
        report = evaluate_go_no_go(
            folds, result,
            nll_with_delta=900.0,
            nll_without_delta=950.0,
            empirical_h1_ratio=0.49,
        )
        assert report.overall_pass

    def test_gamma_failure_blocks(self):
        folds = [FoldResult(fold_idx=0, train_seasons=[], val_seasons=[])]
        # Wrong gamma sign
        result = _make_training_result(gamma_H=(0.3, 0.2))
        report = evaluate_go_no_go(folds, result)
        assert not report.gamma_signs_pass
        assert not report.overall_pass

    def test_drawdown_failure_blocks(self):
        folds = [
            FoldResult(
                fold_idx=0,
                train_seasons=[], val_seasons=[],
                sim_pnl={"total_pnl": -500.0, "max_drawdown_pct": 25.0},
            ),
        ]
        result = _make_training_result()
        report = evaluate_go_no_go(folds, result)
        assert not report.max_drawdown_pass
        assert not report.overall_pass


# ---------------------------------------------------------------------------
# Production params save
# ---------------------------------------------------------------------------

class TestSaveProductionParams:
    def test_creates_directory_and_files(self):
        result = _make_training_result()
        Q = np.eye(4)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = save_production_params(
                result, Q,
                feature_mask=["feat1", "feat2"],
                median_values={"feat1": 1.0, "feat2": 2.0},
                output_base=tmpdir,
            )

            from pathlib import Path
            p = Path(out_dir)
            assert p.exists()
            assert (p / "params.json").exists()
            assert (p / "Q.npy").exists()
            assert (p / "feature_mask.json").exists()
            assert (p / "median_values.json").exists()

            # Check symlink
            prod = Path(tmpdir) / "production"
            assert prod.is_symlink()

    def test_params_json_has_correct_keys(self):
        result = _make_training_result()
        Q = np.eye(4)

        with tempfile.TemporaryDirectory() as tmpdir:
            import json
            out_dir = save_production_params(result, Q, output_base=tmpdir)

            with open(Path(out_dir) / "params.json") as f:
                params = json.load(f)

            assert "b" in params
            assert "gamma_H" in params
            assert "gamma_A" in params
            assert "delta_H" in params
            assert "delta_A" in params
            assert len(params["b"]) == 6
            assert len(params["gamma_H"]) == 4  # Expanded
            assert len(params["delta_H"]) == 4

    def test_Q_roundtrip(self):
        result = _make_training_result()
        Q = np.array([[-.04, .02, .02, 0],
                       [0, -.02, 0, .02],
                       [0, 0, -.02, .02],
                       [0, 0, 0, 0]])

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = save_production_params(result, Q, output_base=tmpdir)
            Q_loaded = np.load(Path(out_dir) / "Q.npy")
            np.testing.assert_array_almost_equal(Q, Q_loaded)
