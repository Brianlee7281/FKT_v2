"""Tier 4: Context Features.

Source: Goalserve Fixtures — home/away, rest days, H2H history.

Reference: phase1.md → Step 1.3 → Tier 4
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


TIER4_FEATURES = [
    "is_home",
    "rest_days",
    "opp_rest_days",
    "h2h_goal_diff",
]


def build_context_features(
    is_home: bool,
    match_date: str,
    team_prev_date: str | None = None,
    opp_prev_date: str | None = None,
    h2h_matches: list[dict] | None = None,
) -> dict[str, float]:
    """Build Tier 4 context features.

    Args:
        is_home: Whether this team is the home team.
        match_date: Match date string (various formats accepted).
        team_prev_date: Date of team's previous match.
        opp_prev_date: Date of opponent's previous match.
        h2h_matches: List of recent H2H match dicts with
                      'home_goals' and 'away_goals' keys.

    Returns:
        Dict of feature name → value.
    """
    return {
        "is_home": 1.0 if is_home else 0.0,
        "rest_days": _calc_rest_days(match_date, team_prev_date),
        "opp_rest_days": _calc_rest_days(match_date, opp_prev_date),
        "h2h_goal_diff": _calc_h2h_goal_diff(h2h_matches, is_home),
    }


def _calc_rest_days(match_date: str, prev_date: str | None) -> float:
    """Calculate rest days between matches."""
    if not prev_date:
        return 7.0  # Default assumption

    d1 = _parse_date(match_date)
    d2 = _parse_date(prev_date)
    if d1 and d2:
        diff = abs((d1 - d2).days)
        return float(min(diff, 30))  # Cap at 30 days
    return 7.0


def _calc_h2h_goal_diff(
    h2h_matches: list[dict] | None, is_home: bool
) -> float:
    """Calculate mean goal difference from H2H history.

    Positive = this team scored more on average.
    """
    if not h2h_matches:
        return 0.0

    diffs = []
    for m in h2h_matches[-5:]:  # Last 5 H2H
        hg = _safe_float(m.get("home_goals", m.get("ft_score_h", 0)))
        ag = _safe_float(m.get("away_goals", m.get("ft_score_a", 0)))
        if is_home:
            diffs.append(hg - ag)
        else:
            diffs.append(ag - hg)

    if not diffs:
        return 0.0
    return float(sum(diffs) / len(diffs))


def _parse_date(date_str: str) -> datetime | None:
    """Try multiple date formats."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _safe_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
