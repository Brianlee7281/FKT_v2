"""Step 1.3 — ML Prior Parameter a via XGBoost.

Trains a Poisson-regression XGBoost model to predict per-team expected goals,
then converts predictions to initial baseline intensity a.

Input:  historical_matches + features (Tier 1-4)
Output: XGBoost model, feature_mask.json, median_values.json

Reference: phase1.md → Step 1.3
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xgboost as xgb

from src.calibration.features.tier1_team import (
    TIER1_FEATURES,
    build_team_features,
)
from src.calibration.features.tier2_player import (
    TIER2_FEATURES,
    build_player_features,
)
from src.calibration.features.tier3_odds import (
    TIER3_FEATURES,
    build_odds_features,
)
from src.calibration.features.tier4_context import (
    TIER4_FEATURES,
    build_context_features,
)


# All feature names in canonical order
ALL_FEATURES = TIER1_FEATURES + TIER2_FEATURES + TIER3_FEATURES + TIER4_FEATURES

# Cumulative importance threshold for feature selection
IMPORTANCE_THRESHOLD = 0.95


@dataclass
class MLPriorArtifacts:
    """Output artifacts from training the ML prior."""
    model: xgb.Booster
    feature_names: list[str]        # All features used in training
    feature_mask: list[str]         # Selected features (top 95% importance)
    median_values: dict[str, float] # Median per feature (for imputation)


@dataclass
class MatchFeatureRow:
    """A single training row: features + target for one team in one match."""
    features: dict[str, float]
    target_goals: int
    match_id: str = ""
    team: str = ""  # "home" or "away"


# ---------------------------------------------------------------------------
# Feature assembly
# ---------------------------------------------------------------------------

def assemble_features(
    team_stats: list[dict],
    player_ids: list[str] | None = None,
    player_history: dict[str, list[dict]] | None = None,
    bookmakers: list[dict] | None = None,
    is_home: bool = True,
    match_date: str = "",
    team_prev_date: str | None = None,
    opp_prev_date: str | None = None,
    h2h_matches: list[dict] | None = None,
) -> dict[str, float]:
    """Assemble all tiers of features into a single dict.

    Missing tiers gracefully produce zeros.
    """
    feats: dict[str, float] = {}

    # Tier 1
    feats.update(build_team_features(team_stats))

    # Tier 2
    if player_ids and player_history:
        feats.update(build_player_features(player_ids, player_history))
    else:
        feats.update({f: 0.0 for f in TIER2_FEATURES})

    # Tier 3
    feats.update(build_odds_features(bookmakers or []))

    # Tier 4
    feats.update(build_context_features(
        is_home=is_home,
        match_date=match_date,
        team_prev_date=team_prev_date,
        opp_prev_date=opp_prev_date,
        h2h_matches=h2h_matches,
    ))

    return feats


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_ml_prior(
    rows: list[MatchFeatureRow],
    xgb_params: dict[str, Any] | None = None,
    num_boost_round: int = 200,
) -> MLPriorArtifacts:
    """Train XGBoost Poisson model on historical match features.

    Args:
        rows: Training data — one row per team per match.
        xgb_params: XGBoost parameters override.
        num_boost_round: Number of boosting rounds.

    Returns:
        MLPriorArtifacts with trained model and metadata.
    """
    if not rows:
        raise ValueError("No training rows provided")

    feature_names = ALL_FEATURES

    # Build feature matrix
    X = np.zeros((len(rows), len(feature_names)))
    y = np.zeros(len(rows))

    for i, row in enumerate(rows):
        for j, fname in enumerate(feature_names):
            X[i, j] = row.features.get(fname, 0.0)
        y[i] = row.target_goals

    # Compute medians for imputation
    median_values = {}
    for j, fname in enumerate(feature_names):
        col = X[:, j]
        non_zero = col[col != 0.0]
        median_values[fname] = float(np.median(non_zero)) if len(non_zero) > 0 else 0.0

    # Impute zeros with medians
    for j, fname in enumerate(feature_names):
        mask = X[:, j] == 0.0
        X[mask, j] = median_values[fname]

    # XGBoost DMatrix
    dtrain = xgb.DMatrix(X, label=y, feature_names=feature_names)

    params = {
        "objective": "count:poisson",
        "max_depth": 5,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "reg_alpha": 0.1,
        "reg_lambda": 1.0,
        "seed": 42,
    }
    if xgb_params:
        params.update(xgb_params)

    model = xgb.train(
        params,
        dtrain,
        num_boost_round=num_boost_round,
        verbose_eval=False,
    )

    # Feature selection by gain importance
    feature_mask = _select_features(model, feature_names, IMPORTANCE_THRESHOLD)

    return MLPriorArtifacts(
        model=model,
        feature_names=feature_names,
        feature_mask=feature_mask,
        median_values=median_values,
    )


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_expected_goals(
    artifacts: MLPriorArtifacts,
    features: dict[str, float],
) -> float:
    """Predict expected goals for one team in one match.

    Args:
        artifacts: Trained model artifacts.
        features: Feature dict for one team.

    Returns:
        Expected goals μ (positive float).
    """
    X = np.zeros((1, len(artifacts.feature_names)))
    for j, fname in enumerate(artifacts.feature_names):
        val = features.get(fname, 0.0)
        if val == 0.0:
            val = artifacts.median_values.get(fname, 0.0)
        X[0, j] = val

    dmat = xgb.DMatrix(X, feature_names=artifacts.feature_names)
    pred = artifacts.model.predict(dmat)
    return float(pred[0])


def convert_to_initial_a(mu: float, T_m: float = 90.0) -> float:
    """Convert expected goals to initial baseline intensity.

    a = ln(μ / T_m)

    Under constant-intensity assumption; corrected in Step 1.4.
    """
    if mu <= 0 or T_m <= 0:
        return -3.0  # Safe lower bound
    return math.log(mu / T_m)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_artifacts(artifacts: MLPriorArtifacts, output_dir: str) -> None:
    """Save model and metadata to disk."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    artifacts.model.save_model(str(out / "ml_prior.xgb"))

    with open(out / "feature_mask.json", "w") as f:
        json.dump(artifacts.feature_mask, f, indent=2)

    with open(out / "median_values.json", "w") as f:
        json.dump(artifacts.median_values, f, indent=2)

    with open(out / "feature_names.json", "w") as f:
        json.dump(artifacts.feature_names, f, indent=2)


def load_artifacts(input_dir: str) -> MLPriorArtifacts:
    """Load model and metadata from disk."""
    inp = Path(input_dir)

    model = xgb.Booster()
    model.load_model(str(inp / "ml_prior.xgb"))

    with open(inp / "feature_mask.json") as f:
        feature_mask = json.load(f)

    with open(inp / "median_values.json") as f:
        median_values = json.load(f)

    with open(inp / "feature_names.json") as f:
        feature_names = json.load(f)

    return MLPriorArtifacts(
        model=model,
        feature_names=feature_names,
        feature_mask=feature_mask,
        median_values=median_values,
    )


# ---------------------------------------------------------------------------
# Feature selection
# ---------------------------------------------------------------------------

def _select_features(
    model: xgb.Booster,
    feature_names: list[str],
    threshold: float,
) -> list[str]:
    """Select features with top cumulative importance (gain-based).

    Returns feature names that collectively account for `threshold`
    fraction of total gain importance.
    """
    importance = model.get_score(importance_type="gain")
    if not importance:
        return list(feature_names)

    # Sort by importance descending
    sorted_feats = sorted(importance.items(), key=lambda x: x[1], reverse=True)
    total_gain = sum(v for _, v in sorted_feats)

    if total_gain == 0:
        return list(feature_names)

    selected = []
    cumulative = 0.0
    for fname, gain in sorted_feats:
        selected.append(fname)
        cumulative += gain / total_gain
        if cumulative >= threshold:
            break

    return selected
