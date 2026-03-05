"""Transform Goalserve raw JSON into internal types."""

from __future__ import annotations

from typing import Any

from src.common.types import GoalEvent, MatchResult, RedCardEvent
from src.common.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_list(val: Any) -> list:
    """Goalserve sometimes returns a single dict instead of a list."""
    if val is None:
        return []
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list):
        return val
    return []


def parse_minute(minute: str | int, extra_min: str | int = "") -> float:
    """Convert minute + extra_min to a float timestamp.

    Stoppage-time goal: minute=90, extra_min=3 → 93.0
    """
    m = float(minute) if minute not in (None, "") else 0.0
    em = float(extra_min) if extra_min not in (None, "") else 0.0
    return m + em


def resolve_scoring_team(goal_data: dict, recorded_team: str) -> str:
    """Flip scoring team for own goals."""
    if _is_true(goal_data.get("owngoal")):
        return "visitorteam" if recorded_team == "localteam" else "localteam"
    return recorded_team


def _is_true(val: Any) -> bool:
    """Check if a Goalserve boolean-like field is True."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


def _safe_int(val: Any) -> int:
    if val is None or val == "":
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Goal parsing
# ---------------------------------------------------------------------------

def parse_goals(summary: dict, team_key: str) -> list[GoalEvent]:
    """Parse goals from summary.{team}.goals.

    Args:
        summary: The match summary dict.
        team_key: "localteam" or "visitorteam".

    Returns:
        List of GoalEvent, excluding VAR-cancelled goals.
    """
    team_summary = summary.get(team_key, {})
    if not team_summary:
        return []

    goals_data = team_summary.get("goals", {})
    if not goals_data:
        return []

    raw_goals = ensure_list(goals_data.get("player", []))
    events = []

    for g in raw_goals:
        var_cancelled = _is_true(g.get("var_cancelled"))

        scoring_team = resolve_scoring_team(g, team_key)

        event = GoalEvent(
            minute=parse_minute(g.get("minute", 0), g.get("extra_min", "")),
            extra_min=float(g.get("extra_min", 0) or 0),
            player_id=str(g.get("id", "")),
            player_name=g.get("name", ""),
            team=team_key,
            scoring_team=scoring_team,
            is_penalty=_is_true(g.get("penalty")),
            is_owngoal=_is_true(g.get("owngoal")),
            var_cancelled=var_cancelled,
        )
        events.append(event)

    return events


# ---------------------------------------------------------------------------
# Red card parsing
# ---------------------------------------------------------------------------

def parse_red_cards(summary: dict, team_key: str) -> list[RedCardEvent]:
    """Parse red cards from summary.{team}.redcards."""
    team_summary = summary.get(team_key, {})
    if not team_summary:
        return []

    redcards_data = team_summary.get("redcards", {})
    if not redcards_data:
        return []

    raw_cards = ensure_list(redcards_data.get("player", []))
    events = []

    for r in raw_cards:
        event = RedCardEvent(
            minute=parse_minute(r.get("minute", 0), r.get("extra_min", "")),
            extra_min=float(r.get("extra_min", 0) or 0),
            player_id=str(r.get("id", "")),
            player_name=r.get("name", ""),
            team=team_key,
        )
        events.append(event)

    return events


# ---------------------------------------------------------------------------
# Match result parsing
# ---------------------------------------------------------------------------

def parse_match_result(match: dict, league_id: str = "") -> MatchResult:
    """Parse a full match dict into a MatchResult."""
    local = match.get("localteam", {})
    visitor = match.get("visitorteam", {})
    matchinfo = match.get("matchinfo", {})
    time_info = matchinfo.get("time", {})

    return MatchResult(
        match_id=str(match.get("id", match.get("static_id", ""))),
        league_id=league_id or match.get("league_id", ""),
        date=match.get("date", match.get("formatted_date", "")),
        home_team=local.get("name", ""),
        away_team=visitor.get("name", ""),
        ft_score_h=_safe_int(local.get("ft_score")),
        ft_score_a=_safe_int(visitor.get("ft_score")),
        ht_score_h=_safe_int(local.get("ht_score")),
        ht_score_a=_safe_int(visitor.get("ht_score")),
        added_time_1=_safe_int(time_info.get("addedTime_period1")),
        added_time_2=_safe_int(time_info.get("addedTime_period2")),
        status=match.get("status", ""),
        summary=match.get("summary", {}),
        lineups=match.get("teams", {}),
    )


# ---------------------------------------------------------------------------
# Odds parsing
# ---------------------------------------------------------------------------

def parse_odds(bookmakers: list[dict]) -> dict:
    """Parse bookmaker odds into normalized probabilities.

    Args:
        bookmakers: List of bookmaker dicts from Goalserve Pregame Odds.

    Returns:
        Dict with pinnacle probs, market average, and std.
    """
    if not bookmakers:
        return {}

    all_probs = []
    pinnacle_prob = None

    for bm in bookmakers:
        odds = bm.get("odd", bm.get("odds", []))
        if isinstance(odds, dict):
            odds = [odds]

        # Find 1X2 (Match Winner) odds
        home_odds = draw_odds = away_odds = None
        for o in ensure_list(odds):
            name = o.get("name", "").lower()
            value = o.get("value", o.get("odds", ""))
            if name in ("1", "home", "1x2_1"):
                home_odds = _safe_float(value)
            elif name in ("x", "draw", "1x2_x"):
                draw_odds = _safe_float(value)
            elif name in ("2", "away", "1x2_2"):
                away_odds = _safe_float(value)

        if not all([home_odds, draw_odds, away_odds]):
            continue
        if home_odds <= 0 or draw_odds <= 0 or away_odds <= 0:
            continue

        prob = _remove_overround(home_odds, draw_odds, away_odds)
        all_probs.append(prob)

        bm_name = bm.get("name", "").lower()
        if "pinnacle" in bm_name:
            pinnacle_prob = prob

    if not all_probs:
        return {}

    import numpy as np
    avg_probs = tuple(np.mean(all_probs, axis=0))

    if pinnacle_prob is None:
        pinnacle_prob = avg_probs

    return {
        "pinnacle_home_prob": pinnacle_prob[0],
        "pinnacle_draw_prob": pinnacle_prob[1],
        "pinnacle_away_prob": pinnacle_prob[2],
        "market_avg_home_prob": avg_probs[0],
        "market_avg_draw_prob": avg_probs[1],
        "market_avg_away_prob": avg_probs[2],
        "bookmaker_odds_std": float(np.std([p[0] for p in all_probs])),
        "n_bookmakers": len(all_probs),
    }


# ---------------------------------------------------------------------------
# Player stats parsing
# ---------------------------------------------------------------------------

def parse_player_stats(player_stats_data: dict,
                       team_key: str) -> list[dict]:
    """Parse player stats from Live Game Stats response.

    Args:
        player_stats_data: The player_stats section of the response.
        team_key: "localteam" or "visitorteam".

    Returns:
        List of per-player stat dicts.
    """
    team_data = player_stats_data.get(team_key, {})
    if not team_data:
        return []

    players = ensure_list(team_data.get("player", []))
    return players


def parse_team_stats(stats_data: dict, team_key: str) -> dict:
    """Parse team-level stats from Live Game Stats response."""
    team_stats = stats_data.get(team_key, stats_data.get("stats", {}).get(team_key, {}))
    if not team_stats:
        return {}
    return team_stats


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _remove_overround(h: float, d: float, a: float) -> tuple[float, float, float]:
    """Remove bookmaker overround from decimal odds."""
    total = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / total, (1.0 / d) / total, (1.0 / a) / total


def _safe_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
