"""Tier 1: Team-Level Rolling Stats.

Source: Goalserve Live Game Stats — stats.{team}
Aggregate rolling averages over each team's last N matches.

Reference: phase1.md → Step 1.3 → Tier 1
"""

from __future__ import annotations

from typing import Any

import numpy as np


# Default rolling window size
DEFAULT_WINDOW = 5

# Feature names produced by this tier
TIER1_FEATURES = [
    "xG_per_90",
    "xGA_per_90",
    "shots_per_90",
    "shots_on_target_per_90",
    "shots_insidebox_ratio",
    "possession_avg",
    "pass_accuracy",
    "corners_per_90",
    "fouls_per_90",
    "saves_per_90",
]


def build_team_features(
    recent_stats: list[dict],
    window: int = DEFAULT_WINDOW,
) -> dict[str, float]:
    """Build Tier 1 features from a team's recent match stats.

    Args:
        recent_stats: List of team stats dicts from Goalserve Live Game Stats,
                      ordered most-recent-first. Each dict has structure like
                      stats.localteam or stats.visitorteam.
        window: Number of recent matches to use.

    Returns:
        Dict of feature name → value.
    """
    stats = recent_stats[:window]
    if not stats:
        return {f: 0.0 for f in TIER1_FEATURES}

    xg_vals = []
    xga_vals = []
    shots_vals = []
    sot_vals = []
    insidebox_ratios = []
    possession_vals = []
    pass_acc_vals = []
    corners_vals = []
    fouls_vals = []
    saves_vals = []

    for s in stats:
        # Goalserve stores xG under "expected_goals.total", not "xg"/"xG"
        xg_vals.append(_extract_stat(s, "expected_goals", "total"))
        # xGA: use opponent's expected_goals if available, else goals_prevented
        xga_val = _extract_stat(s, "goals_prevented", "total")
        xga_vals.append(xga_val)

        shots = _extract_stat(s, "shots", "total")
        shots_vals.append(shots)

        sot = _extract_stat(s, "shots", "ongoal")
        sot_vals.append(sot)

        insidebox = _extract_stat(s, "shots", "insidebox")
        if shots > 0:
            insidebox_ratios.append(insidebox / shots)

        poss_val = _extract_stat(s, "possestiontime", "total")
        # Goalserve stores possession as "50%" — strip the %
        if poss_val == 0.0:
            raw_poss = s.get("possestiontime", {})
            if isinstance(raw_poss, dict):
                raw_str = str(raw_poss.get("total", ""))
                if raw_str.endswith("%"):
                    poss_val = _safe_float(raw_str.rstrip("%"))
        possession_vals.append(poss_val)

        passes_acc = _extract_stat(s, "passes", "accurate")
        passes_total = _extract_stat(s, "passes", "total")
        if passes_total > 0:
            pass_acc_vals.append(passes_acc / passes_total)

        corners_vals.append(_extract_stat(s, "corners", "total"))
        fouls_vals.append(_extract_stat(s, "fouls", "total"))
        saves_vals.append(_extract_stat(s, "saves", "total"))

    n = len(stats)
    return {
        "xG_per_90": _safe_mean(xg_vals),
        "xGA_per_90": _safe_mean(xga_vals),
        "shots_per_90": _safe_mean(shots_vals),
        "shots_on_target_per_90": _safe_mean(sot_vals),
        "shots_insidebox_ratio": _safe_mean(insidebox_ratios),
        "possession_avg": _safe_mean(possession_vals),
        "pass_accuracy": _safe_mean(pass_acc_vals),
        "corners_per_90": _safe_mean(corners_vals),
        "fouls_per_90": _safe_mean(fouls_vals),
        "saves_per_90": _safe_mean(saves_vals),
    }


def _extract_stat(stats: dict, category: str, field: str) -> float:
    """Extract a nested stat like stats.shots.total."""
    cat = stats.get(category, {})
    if isinstance(cat, dict):
        return _safe_float(cat.get(field, 0))
    return _safe_float(cat)


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
