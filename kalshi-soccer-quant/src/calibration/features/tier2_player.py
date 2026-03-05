"""Tier 2: Player-Level Aggregated Features.

Source: Goalserve Live Game Stats — player_stats.{team}.player[]
Aggregate recent stats of starting XI by position group.

Reference: phase1.md → Step 1.3 → Tier 2
"""

from __future__ import annotations

from typing import Any

import numpy as np


# Minimum minutes played to include in rolling averages
MIN_MINUTES = 10

# Position group mapping
POSITION_GROUPS = {
    "fw": ["F", "FW", "CF", "ST", "LW", "RW"],
    "mf": ["M", "MF", "CM", "AM", "DM", "LM", "RM", "CAM", "CDM"],
    "df": ["D", "DF", "CB", "LB", "RB", "LWB", "RWB"],
    "gk": ["G", "GK"],
}

TIER2_FEATURES = [
    "fw_avg_rating",
    "fw_goals_p90",
    "fw_key_passes_p90",
    "mf_avg_rating",
    "mf_key_passes_p90",
    "mf_pass_accuracy",
    "df_avg_rating",
    "df_tackles_p90",
    "df_interceptions_p90",
    "gk_save_rate",
    "team_avg_rating",
]


def build_player_features(
    starting_11_ids: list[str],
    player_history: dict[str, list[dict]],
) -> dict[str, float]:
    """Build Tier 2 features from starting XI player histories.

    Args:
        starting_11_ids: List of player IDs in the starting lineup.
        player_history: {player_id: [recent N matches of player_stats]}.

    Returns:
        Dict of feature name → value.
    """
    if not starting_11_ids or not player_history:
        return {f: 0.0 for f in TIER2_FEATURES}

    # Classify players into position groups
    grouped: dict[str, list[str]] = {g: [] for g in POSITION_GROUPS}
    for pid in starting_11_ids:
        history = player_history.get(pid, [])
        if not history:
            continue
        pos = str(history[0].get("pos", history[0].get("position", "")))
        assigned = False
        for group, codes in POSITION_GROUPS.items():
            if pos.upper() in [c.upper() for c in codes]:
                grouped[group].append(pid)
                assigned = True
                break
        if not assigned:
            grouped["mf"].append(pid)  # Default to midfielder

    features: dict[str, float] = {}

    # Forward features
    fw_ratings, fw_goals, fw_kp = _collect_offensive_stats(
        grouped["fw"], player_history
    )
    features["fw_avg_rating"] = _safe_mean(fw_ratings)
    features["fw_goals_p90"] = _safe_sum(fw_goals)
    features["fw_key_passes_p90"] = _safe_sum(fw_kp)

    # Midfielder features
    mf_ratings, mf_kp, mf_pass_acc = _collect_midfield_stats(
        grouped["mf"], player_history
    )
    features["mf_avg_rating"] = _safe_mean(mf_ratings)
    features["mf_key_passes_p90"] = _safe_sum(mf_kp)
    features["mf_pass_accuracy"] = _safe_mean(mf_pass_acc)

    # Defender features
    df_ratings, df_tackles, df_interceptions = _collect_defensive_stats(
        grouped["df"], player_history
    )
    features["df_avg_rating"] = _safe_mean(df_ratings)
    features["df_tackles_p90"] = _safe_sum(df_tackles)
    features["df_interceptions_p90"] = _safe_sum(df_interceptions)

    # GK features
    features["gk_save_rate"] = _calc_gk_save_rate(
        grouped["gk"], player_history
    )

    # Team average rating (minutes-weighted)
    all_ratings = fw_ratings + mf_ratings + df_ratings
    features["team_avg_rating"] = _safe_mean(all_ratings)

    return features


# ---------------------------------------------------------------------------
# Position-group stat collectors
# ---------------------------------------------------------------------------

def _collect_offensive_stats(
    player_ids: list[str], history: dict[str, list[dict]]
) -> tuple[list[float], list[float], list[float]]:
    ratings, goals_p90, kp_p90 = [], [], []
    for pid in player_ids:
        for gs in history.get(pid, []):
            mp = _safe_float(gs.get("minutes_played", 0))
            if mp < MIN_MINUTES:
                continue
            ratings.append(_safe_float(gs.get("rating", 0)))
            goals_p90.append(_safe_float(gs.get("goals", 0)) / mp * 90)
            kp_p90.append(_safe_float(gs.get("keyPasses", gs.get("key_passes", 0))) / mp * 90)
    return ratings, goals_p90, kp_p90


def _collect_midfield_stats(
    player_ids: list[str], history: dict[str, list[dict]]
) -> tuple[list[float], list[float], list[float]]:
    ratings, kp_p90, pass_acc = [], [], []
    for pid in player_ids:
        for gs in history.get(pid, []):
            mp = _safe_float(gs.get("minutes_played", 0))
            if mp < MIN_MINUTES:
                continue
            ratings.append(_safe_float(gs.get("rating", 0)))
            kp_p90.append(_safe_float(gs.get("keyPasses", gs.get("key_passes", 0))) / mp * 90)
            passes_acc = _safe_float(gs.get("passes_accurate", gs.get("passesAccurate", 0)))
            passes_total = _safe_float(gs.get("passes_total", gs.get("passesTotal", 0)))
            if passes_total > 0:
                pass_acc.append(passes_acc / passes_total)
    return ratings, kp_p90, pass_acc


def _collect_defensive_stats(
    player_ids: list[str], history: dict[str, list[dict]]
) -> tuple[list[float], list[float], list[float]]:
    ratings, tackles_p90, interceptions_p90 = [], [], []
    for pid in player_ids:
        for gs in history.get(pid, []):
            mp = _safe_float(gs.get("minutes_played", 0))
            if mp < MIN_MINUTES:
                continue
            ratings.append(_safe_float(gs.get("rating", 0)))
            tackles_p90.append(_safe_float(gs.get("tackles", 0)) / mp * 90)
            interceptions_p90.append(_safe_float(gs.get("interceptions", 0)) / mp * 90)
    return ratings, tackles_p90, interceptions_p90


def _calc_gk_save_rate(
    player_ids: list[str], history: dict[str, list[dict]]
) -> float:
    total_saves = 0.0
    total_conceded = 0.0
    for pid in player_ids:
        for gs in history.get(pid, []):
            mp = _safe_float(gs.get("minutes_played", 0))
            if mp < MIN_MINUTES:
                continue
            total_saves += _safe_float(gs.get("saves", 0))
            total_conceded += _safe_float(gs.get("goals_conceded", 0))
    denom = total_saves + total_conceded
    if denom > 0:
        return total_saves / denom
    return 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return float(np.mean(vals))


def _safe_sum(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return float(np.sum(vals))
