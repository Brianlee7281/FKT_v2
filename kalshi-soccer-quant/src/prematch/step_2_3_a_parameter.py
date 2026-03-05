"""Step 2.3: Back-Solving Baseline Intensity Parameter a.

Converts XGBoost expected-goals predictions into the initial
baseline intensity parameter a for the live trading engine.

  a_H = ln(mu_hat_H) - ln(C_time)
  a_A = ln(mu_hat_A) - ln(C_time)

where C_time = sum_i exp(b_i) * delta_t_i integrates the
piecewise-constant time profile over the full expected match duration.

Input:  X_home, X_away (from Step 2.2), XGBoost model, Phase 1 params (b, alpha)
Output: a_H, a_A, C_time

Reference: phase2.md -> Step 2.3
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import xgboost as xgb

from src.calibration.step_1_3_ml_prior import MLPriorArtifacts


@dataclass
class AParameterResult:
    """Output of Step 2.3 back-solving."""
    a_H: float          # Home baseline intensity
    a_A: float          # Away baseline intensity
    mu_H: float         # Home expected goals (XGBoost prediction)
    mu_A: float         # Away expected goals (XGBoost prediction)
    C_time: float       # Time normalization constant


# Reasonable bounds for a parameter (safety clamp)
A_MIN = -5.0
A_MAX = 0.0


def compute_C_time(
    b: np.ndarray,
    E_alpha_1: float = 3.0,
    E_alpha_2: float = 5.0,
) -> float:
    """Compute the time normalization constant C_time.

    C_time = sum_i exp(b_i) * delta_t_i

    where delta_t_i are the durations of each time bin:
      bin 0: 15 min           (1H 0-15)
      bin 1: 15 min           (1H 15-30)
      bin 2: 15 + E[alpha_1]  (1H 30-45 + stoppage)
      bin 3: 15 min           (2H 0-15)
      bin 4: 15 min           (2H 15-30)
      bin 5: 15 + E[alpha_2]  (2H 30-45 + stoppage)

    Args:
        b: Array of 6 time-bin coefficients from Phase 1 Step 1.4.
        E_alpha_1: Expected first-half stoppage time (minutes).
        E_alpha_2: Expected second-half stoppage time (minutes).

    Returns:
        C_time (positive float).
    """
    if len(b) != 6:
        raise ValueError(f"Expected 6 time bins, got {len(b)}")

    delta_t = np.array([
        15.0,                   # bin 0
        15.0,                   # bin 1
        15.0 + E_alpha_1,       # bin 2
        15.0,                   # bin 3
        15.0,                   # bin 4
        15.0 + E_alpha_2,       # bin 5
    ])

    return float(np.sum(np.exp(b) * delta_t))


def predict_expected_goals(
    X_match: np.ndarray,
    artifacts: MLPriorArtifacts,
) -> float:
    """Predict expected goals for one team using the XGBoost Poisson model.

    Args:
        X_match: Feature vector from Step 2.2 (1D array, mask-selected features).
        artifacts: Phase 1 ML prior artifacts (model + metadata).

    Returns:
        mu_hat — expected goals (positive float).
    """
    # Expand to full feature set (model expects all features)
    X_full = np.zeros((1, len(artifacts.feature_names)))
    for j, fname in enumerate(artifacts.feature_names):
        # Find this feature in the mask
        if fname in artifacts.feature_mask:
            mask_idx = artifacts.feature_mask.index(fname)
            if mask_idx < len(X_match):
                X_full[0, j] = X_match[mask_idx]
            else:
                X_full[0, j] = artifacts.median_values.get(fname, 0.0)
        else:
            X_full[0, j] = artifacts.median_values.get(fname, 0.0)

    dmat = xgb.DMatrix(X_full, feature_names=artifacts.feature_names)
    pred = artifacts.model.predict(dmat)
    return float(pred[0])


def back_solve_a(
    mu_hat: float,
    C_time: float,
) -> float:
    """Back-solve for baseline intensity parameter a.

    a = ln(mu_hat) - ln(C_time)

    Args:
        mu_hat: Expected goals from XGBoost prediction.
        C_time: Time normalization constant.

    Returns:
        a parameter (clamped to [A_MIN, A_MAX]).
    """
    if mu_hat <= 0 or C_time <= 0:
        return A_MIN

    a = math.log(mu_hat) - math.log(C_time)
    return max(A_MIN, min(A_MAX, a))


def compute_a_parameters(
    X_home: np.ndarray,
    X_away: np.ndarray,
    artifacts: MLPriorArtifacts,
    b: np.ndarray,
    E_alpha_1: float = 3.0,
    E_alpha_2: float = 5.0,
) -> AParameterResult:
    """Full Step 2.3 pipeline: predict goals and back-solve a.

    Args:
        X_home: Home feature vector from Step 2.2.
        X_away: Away feature vector from Step 2.2.
        artifacts: Phase 1 ML prior artifacts.
        b: 6-element array of time-bin coefficients from Phase 1.
        E_alpha_1: Expected first-half stoppage time.
        E_alpha_2: Expected second-half stoppage time.

    Returns:
        AParameterResult with a_H, a_A, mu_H, mu_A, C_time.
    """
    C_time = compute_C_time(b, E_alpha_1, E_alpha_2)

    mu_H = predict_expected_goals(X_home, artifacts)
    mu_A = predict_expected_goals(X_away, artifacts)

    a_H = back_solve_a(mu_H, C_time)
    a_A = back_solve_a(mu_A, C_time)

    return AParameterResult(
        a_H=a_H,
        a_A=a_A,
        mu_H=mu_H,
        mu_A=mu_A,
        C_time=C_time,
    )
