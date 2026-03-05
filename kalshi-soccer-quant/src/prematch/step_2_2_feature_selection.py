"""Step 2.2: Feature Selection — Apply Phase 1 Feature Mask.

Extracts only validated features from PreMatchData using the
feature_mask.json saved in Phase 1 Step 1.3.

Input:  PreMatchData + feature_mask.json + median_values.json
Output: X_match (np.ndarray) — feature vector with identical
        dimensionality and order as Phase 1 training.

Reference: phase2.md -> Step 2.2
"""

from __future__ import annotations

import numpy as np

from src.common.types import PreMatchData


def build_full_feature_vector(pre_match: PreMatchData) -> dict[str, float]:
    """Build full (unpruned) feature dict from PreMatchData.

    Mirrors the feature assembly in Phase 1 Step 1.3 but sources
    from the PreMatchData structure populated in Step 2.1.

    Feature names use home_/away_ prefixes for team-specific tiers.
    """
    full_vec: dict[str, float] = {}

    # Tier 1: team rolling stats (home/away prefixed)
    for prefix, rolling in [("home_", pre_match.home_team_rolling),
                            ("away_", pre_match.away_team_rolling)]:
        for k, v in rolling.items():
            full_vec[prefix + k] = float(v)

    # Tier 2: player aggregates (home/away prefixed)
    for prefix, agg in [("home_", pre_match.home_player_agg),
                        ("away_", pre_match.away_player_agg)]:
        for k, v in agg.items():
            full_vec[prefix + k] = float(v)

    # Tier 3: odds (not team-specific)
    for k, v in pre_match.odds_features.items():
        if not str(k).startswith("_"):  # Exclude internal fields
            full_vec[k] = float(v)

    # Tier 4: context
    full_vec["is_home"] = 1.0  # Home perspective
    full_vec["rest_days"] = float(pre_match.home_rest_days)
    full_vec["opp_rest_days"] = float(pre_match.away_rest_days)
    full_vec["h2h_goal_diff"] = float(pre_match.h2h_goal_diff)

    return full_vec


def apply_feature_mask(
    pre_match: PreMatchData,
    feature_mask: list[str],
    median_values: dict[str, float],
) -> np.ndarray:
    """Extract only features listed in Phase 1 feature_mask.json.

    Missing values are replaced with medians from Phase 1 training data.
    Because feature names share the same Goalserve schema as Phase 1,
    no manual mapping layer is required.

    Args:
        pre_match: PreMatchData from Step 2.1.
        feature_mask: Ordered list of selected feature names from Phase 1.
        median_values: Median per feature from Phase 1 training (for imputation).

    Returns:
        X_match — 1D array of selected features in mask order.
    """
    full_vec = build_full_feature_vector(pre_match)

    selected = []
    for feat_name in feature_mask:
        val = full_vec.get(feat_name)
        if val is not None and not np.isnan(val):
            selected.append(val)
        else:
            selected.append(median_values.get(feat_name, 0.0))

    return np.array(selected, dtype=np.float64)


def apply_feature_mask_both_teams(
    pre_match: PreMatchData,
    feature_mask: list[str],
    median_values: dict[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Build feature vectors for both home and away teams.

    The home vector uses the standard PreMatchData.
    The away vector swaps home/away perspectives.

    Returns:
        (X_home, X_away) tuple of 1D arrays.
    """
    X_home = apply_feature_mask(pre_match, feature_mask, median_values)

    # Build away perspective by swapping home/away in PreMatchData
    away_view = PreMatchData(
        home_starting_11=pre_match.away_starting_11,
        away_starting_11=pre_match.home_starting_11,
        home_formation=pre_match.away_formation,
        away_formation=pre_match.home_formation,
        home_player_agg=pre_match.away_player_agg,
        away_player_agg=pre_match.home_player_agg,
        home_team_rolling=pre_match.away_team_rolling,
        away_team_rolling=pre_match.home_team_rolling,
        odds_features=_flip_odds_perspective(pre_match.odds_features),
        home_rest_days=pre_match.away_rest_days,
        away_rest_days=pre_match.home_rest_days,
        h2h_goal_diff=-pre_match.h2h_goal_diff,
        match_id=pre_match.match_id,
        kickoff_time=pre_match.kickoff_time,
    )

    X_away = apply_feature_mask(away_view, feature_mask, median_values)

    return X_home, X_away


def _flip_odds_perspective(odds: dict) -> dict:
    """Swap home/away in odds features for away-team perspective."""
    flipped = dict(odds)
    swaps = [
        ("pinnacle_home_prob", "pinnacle_away_prob"),
        ("market_avg_home_prob", "market_avg_away_prob"),
    ]
    for a, b in swaps:
        if a in flipped and b in flipped:
            flipped[a], flipped[b] = flipped[b], flipped[a]
    return flipped
