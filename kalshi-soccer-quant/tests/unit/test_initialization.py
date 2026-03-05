"""Tests for Step 2.5: Live Engine Initialization.

Verifies parameter loading, matrix exponential precomputation,
Q_off normalization, and full model assembly.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest
import scipy.linalg

from src.common.types import SanityResult
from src.prematch.step_2_3_a_parameter import AParameterResult
from src.prematch.step_2_5_initialization import (
    LiveModelInstance,
    initialize_model,
    load_phase1_params,
    normalize_Q_off,
    precompute_P_fine_grid,
    precompute_P_grid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_params_dir() -> str:
    """Create a temporary directory with Phase 1 production params."""
    tmpdir = tempfile.mkdtemp()

    # params.json
    params = {
        "b": [0.0, 0.05, 0.1, 0.0, -0.05, 0.15],
        "gamma_H": [0.0, -0.15, 0.10, -0.05],
        "gamma_A": [0.0, 0.10, -0.15, -0.05],
        "delta_H": [-0.3, -0.1, 0.0, 0.1, 0.3],
        "delta_A": [0.3, 0.1, 0.0, -0.1, -0.3],
    }
    with open(Path(tmpdir) / "params.json", "w") as f:
        json.dump(params, f)

    # Q.npy — simple 4x4 rate matrix
    Q = np.array([
        [-0.02, 0.01, 0.01, 0.00],
        [0.00, -0.01, 0.00, 0.01],
        [0.00, 0.00, -0.01, 0.01],
        [0.00, 0.00, 0.00, 0.00],
    ])
    np.save(str(Path(tmpdir) / "Q.npy"), Q)

    # validation_report.json
    report = {"delta_lrt_pass": True, "overall_pass": True}
    with open(Path(tmpdir) / "validation_report.json", "w") as f:
        json.dump(report, f)

    return tmpdir


def _make_a_result() -> AParameterResult:
    return AParameterResult(
        a_H=-3.2,
        a_A=-3.5,
        mu_H=1.5,
        mu_A=1.2,
        C_time=98.0,
    )


def _make_sanity_result() -> SanityResult:
    return SanityResult(
        verdict="GO",
        delta_match_winner=0.05,
        delta_over_under=0.03,
    )


# ---------------------------------------------------------------------------
# Parameter loading tests
# ---------------------------------------------------------------------------

class TestLoadPhase1Params:
    def test_loads_all_keys(self):
        params_dir = _make_params_dir()
        params = load_phase1_params(params_dir)

        assert "b" in params
        assert "gamma_H" in params
        assert "gamma_A" in params
        assert "delta_H" in params
        assert "delta_A" in params
        assert "Q" in params
        assert "delta_significant" in params

    def test_correct_shapes(self):
        params_dir = _make_params_dir()
        params = load_phase1_params(params_dir)

        assert params["b"].shape == (6,)
        assert params["gamma_H"].shape == (4,)
        assert params["gamma_A"].shape == (4,)
        assert params["delta_H"].shape == (5,)
        assert params["delta_A"].shape == (5,)
        assert params["Q"].shape == (4, 4)

    def test_delta_significant_from_report(self):
        params_dir = _make_params_dir()
        params = load_phase1_params(params_dir)
        assert params["delta_significant"] is True

    def test_delta_significant_false_without_report(self):
        params_dir = _make_params_dir()
        (Path(params_dir) / "validation_report.json").unlink()
        params = load_phase1_params(params_dir)
        assert params["delta_significant"] is False


# ---------------------------------------------------------------------------
# Matrix exponential tests
# ---------------------------------------------------------------------------

class TestPrecomputePGrid:
    def test_grid_size(self):
        Q = np.zeros((4, 4))
        P_grid = precompute_P_grid(Q)
        assert len(P_grid) == 101  # 0..100

    def test_P_0_is_identity(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        P_grid = precompute_P_grid(Q)
        np.testing.assert_allclose(P_grid[0], np.eye(4), atol=1e-10)

    def test_P_values_match_scipy(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        P_grid = precompute_P_grid(Q)
        expected = scipy.linalg.expm(Q * 45)
        np.testing.assert_allclose(P_grid[45], expected, atol=1e-12)

    def test_rows_sum_to_one(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        P_grid = precompute_P_grid(Q)
        for dt in [0, 10, 45, 90, 100]:
            row_sums = P_grid[dt].sum(axis=1)
            np.testing.assert_allclose(row_sums, np.ones(4), atol=1e-10)


class TestPrecomputePFineGrid:
    def test_fine_grid_size(self):
        Q = np.zeros((4, 4))
        P_fine = precompute_P_fine_grid(Q)
        assert len(P_fine) == 31  # 0..30 (10-sec steps)

    def test_fine_grid_0_is_identity(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        P_fine = precompute_P_fine_grid(Q)
        np.testing.assert_allclose(P_fine[0], np.eye(4), atol=1e-10)

    def test_fine_grid_30_equals_5_minutes(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        P_fine = precompute_P_fine_grid(Q)
        expected = scipy.linalg.expm(Q * 5.0)
        np.testing.assert_allclose(P_fine[30], expected, atol=1e-12)


# ---------------------------------------------------------------------------
# Q_off normalization tests
# ---------------------------------------------------------------------------

class TestNormalizeQOff:
    def test_rows_sum_to_one_or_zero(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],  # Absorbing state
        ])
        Q_off = normalize_Q_off(Q)

        # Non-absorbing rows sum to 1
        assert Q_off[0].sum() == pytest.approx(1.0)
        assert Q_off[1].sum() == pytest.approx(1.0)
        assert Q_off[2].sum() == pytest.approx(1.0)

        # Absorbing state row sums to 0
        assert Q_off[3].sum() == pytest.approx(0.0)

    def test_diagonal_is_zero(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        Q_off = normalize_Q_off(Q)
        for i in range(4):
            assert Q_off[i, i] == 0.0

    def test_correct_probabilities(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        Q_off = normalize_Q_off(Q)

        # State 0: equal probability to states 1 and 2
        assert Q_off[0, 1] == pytest.approx(0.5)
        assert Q_off[0, 2] == pytest.approx(0.5)
        assert Q_off[0, 3] == pytest.approx(0.0)

        # State 1: all mass to state 3
        assert Q_off[1, 3] == pytest.approx(1.0)

    def test_all_non_negative(self):
        Q = np.array([
            [-0.02, 0.01, 0.01, 0.00],
            [0.00, -0.01, 0.00, 0.01],
            [0.00, 0.00, -0.01, 0.01],
            [0.00, 0.00, 0.00, 0.00],
        ])
        Q_off = normalize_Q_off(Q)
        assert np.all(Q_off >= 0)


# ---------------------------------------------------------------------------
# Full initialization tests
# ---------------------------------------------------------------------------

class TestInitializeModel:
    def test_creates_live_model_instance(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert isinstance(model, LiveModelInstance)

    def test_initial_state_correct(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert model.current_time == 0.0
        assert model.current_state == 0
        assert model.current_score == (0, 0)
        assert model.delta_S == 0
        assert model.engine_phase == "WAITING_FOR_KICKOFF"
        assert model.event_state == "IDLE"

    def test_a_parameters_from_step_23(self):
        params_dir = _make_params_dir()
        a_result = _make_a_result()
        model = initialize_model(
            params_dir=params_dir,
            a_result=a_result,
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert model.a_H == a_result.a_H
        assert model.a_A == a_result.a_A
        assert model.C_time == a_result.C_time

    def test_sanity_result_propagated(self):
        params_dir = _make_params_dir()
        sanity = _make_sanity_result()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=sanity,
            match_id="12345",
        )
        assert model.sanity_verdict == "GO"
        assert model.delta_match_winner == sanity.delta_match_winner

    def test_P_grid_populated(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert len(model.P_grid) == 101
        assert len(model.P_fine_grid) == 31

    def test_Q_off_normalized_populated(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert model.Q_off_normalized.shape == (4, 4)
        # Non-absorbing rows sum to 1
        assert model.Q_off_normalized[0].sum() == pytest.approx(1.0)

    def test_delta_significant_flag(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert model.delta_significant is True

    def test_T_exp_uses_stoppage(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
            E_alpha_1=4.0,
            E_alpha_2=6.0,
        )
        assert model.T_exp == pytest.approx(100.0)

    def test_match_id_set(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="99999",
        )
        assert model.match_id == "99999"

    def test_connectivity_initially_false(self):
        params_dir = _make_params_dir()
        model = initialize_model(
            params_dir=params_dir,
            a_result=_make_a_result(),
            sanity_result=_make_sanity_result(),
            match_id="12345",
        )
        assert model.live_score_ready is False
        assert model.live_odds_healthy is False
        assert model.kalshi_healthy is False
