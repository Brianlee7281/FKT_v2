"""Step 1.1 — Time-Series Event Segmentation and Intervalization.

Converts point events in historical matches into continuous intervals
where lambda is constant. Because intensity depends on (X(t), ΔS(t)),
the interval must be split whenever either variable changes.

Reference: phase1.md → Step 1.1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.common.types import IntervalRecord
from src.goalserve.parsers import ensure_list, parse_minute, _is_true

# Default halftime break duration (minutes)
HALFTIME_BREAK = 15.0


# ---------------------------------------------------------------------------
# Internal event representation
# ---------------------------------------------------------------------------

@dataclass
class _Event:
    """Internal event used for interval splitting."""
    kind: str          # goal, red_card, halftime_start, halftime_end, match_end
    minute: float
    team: str | None   # localteam / visitorteam (None for period boundaries)
    is_owngoal: bool = False
    raw: dict | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_intervals(match_data: dict, match_id: str = "") -> list[IntervalRecord]:
    """Convert a Goalserve match dict into a list of IntervalRecords.

    Args:
        match_data: Raw match dict from Goalserve Fixtures/Results API.
        match_id: Optional override; if empty, extracted from match_data.

    Returns:
        List of IntervalRecords covering the full match timeline.
    """
    if not match_id:
        match_id = str(match_data.get("id", match_data.get("static_id", "")))

    # 1. Extract stoppage time
    matchinfo = match_data.get("matchinfo", {})
    time_info = matchinfo.get("time", {})
    alpha_1 = _safe_float(time_info.get("addedTime_period1"))
    alpha_2 = _safe_float(time_info.get("addedTime_period2"))
    T_m = 90.0 + alpha_1 + alpha_2

    # 2. Collect events
    events = _collect_events(match_data, alpha_1, T_m)

    # 3. Sort by time (stable sort keeps insertion order for ties)
    #    Priority within same minute: red_card before goal (state change first),
    #    halftime/period boundaries last among ties.
    _PRIORITY = {
        "red_card": 0,
        "goal": 1,
        "halftime_start": 2,
        "halftime_end": 3,
        "match_end": 4,
    }
    events.sort(key=lambda e: (e.minute, _PRIORITY.get(e.kind, 9)))

    # 4. Walk through events and produce intervals
    return _split_into_intervals(events, match_id, T_m, alpha_1, alpha_2)


def build_intervals_from_db_row(row: dict) -> list[IntervalRecord]:
    """Build intervals from a historical_matches DB row.

    The DB stores summary, stats, etc. as JSONB columns.
    This reconstructs the match_data dict the parser expects.
    """
    match_data = {
        "id": row.get("match_id", ""),
        "matchinfo": {
            "time": {
                "addedTime_period1": row.get("added_time_1", 0),
                "addedTime_period2": row.get("added_time_2", 0),
            },
        },
        "summary": row.get("summary", {}),
        "localteam": {
            "ht_score": row.get("ht_score_h", 0),
            "ft_score": row.get("ft_score_h", 0),
        },
        "visitorteam": {
            "ht_score": row.get("ht_score_a", 0),
            "ft_score": row.get("ft_score_a", 0),
        },
    }
    return build_intervals(match_data, match_id=row.get("match_id", ""))


# ---------------------------------------------------------------------------
# Event collection
# ---------------------------------------------------------------------------

def _collect_events(match_data: dict, alpha_1: float, T_m: float) -> list[_Event]:
    """Parse goals and red cards from match summary, add period boundaries."""
    events: list[_Event] = []
    summary = match_data.get("summary", {})

    # --- Format A: Detailed summary with per-team goals/redcards ---
    # Structure: summary.localteam.goals.player, summary.visitorteam.goals.player
    for team_key in ("localteam", "visitorteam"):
        team_summary = summary.get(team_key, {})
        if not team_summary:
            continue

        # Goals
        goals_data = team_summary.get("goals", {})
        if goals_data:
            raw_goals = ensure_list(goals_data.get("player", []))
            for g in raw_goals:
                if _is_true(g.get("var_cancelled")):
                    continue

                minute = parse_minute(g.get("minute", 0), g.get("extra_min", ""))
                is_owngoal = _is_true(g.get("owngoal"))

                if is_owngoal:
                    scoring_team = "visitorteam" if team_key == "localteam" else "localteam"
                else:
                    scoring_team = team_key

                events.append(_Event(
                    kind="goal",
                    minute=minute,
                    team=scoring_team,
                    is_owngoal=is_owngoal,
                    raw=g,
                ))

        # Red cards
        redcards_data = team_summary.get("redcards", {})
        if redcards_data:
            raw_cards = ensure_list(redcards_data.get("player", []))
            for r in raw_cards:
                minute = parse_minute(r.get("minute", 0), r.get("extra_min", ""))
                events.append(_Event(
                    kind="red_card",
                    minute=minute,
                    team=team_key,
                    raw=r,
                ))

    # --- Format B: Flat goals list from Goalserve fixtures endpoint ---
    # Structure: summary.goal (list of {team, minute, player, ...})
    # or match_data.goals.goal
    if not any(e.kind == "goal" for e in events):
        # Try flat format from goals field
        goals_src = summary if isinstance(summary, dict) else {}
        flat_goals = goals_src.get("goal", [])
        if not flat_goals:
            goals_field = match_data.get("goals", {})
            if isinstance(goals_field, dict):
                flat_goals = goals_field.get("goal", [])
        flat_goals = ensure_list(flat_goals) if flat_goals else []

        for g in flat_goals:
            minute_str = g.get("minute", "0")
            # Handle "45+2" stoppage time format
            extra = ""
            if isinstance(minute_str, str) and "+" in minute_str:
                parts = minute_str.split("+")
                minute_str = parts[0]
                extra = parts[1] if len(parts) > 1 else ""
            minute = parse_minute(minute_str, extra)

            team = g.get("team", "")
            is_owngoal = "(OG)" in g.get("player", "") or _is_true(g.get("owngoal"))

            if is_owngoal:
                scoring_team = "visitorteam" if team == "localteam" else "localteam"
            else:
                scoring_team = team

            if scoring_team in ("localteam", "visitorteam"):
                events.append(_Event(
                    kind="goal",
                    minute=minute,
                    team=scoring_team,
                    is_owngoal=is_owngoal,
                    raw=g,
                ))

    # Period boundaries
    ht_start = 45.0 + alpha_1
    ht_end = ht_start + HALFTIME_BREAK

    events.append(_Event(kind="halftime_start", minute=ht_start, team=None))
    events.append(_Event(kind="halftime_end", minute=ht_end, team=None))
    events.append(_Event(kind="match_end", minute=T_m + HALFTIME_BREAK, team=None))

    return events


# ---------------------------------------------------------------------------
# Interval splitting
# ---------------------------------------------------------------------------

def _split_into_intervals(
    events: list[_Event],
    match_id: str,
    T_m: float,
    alpha_1: float,
    alpha_2: float,
) -> list[IntervalRecord]:
    """Walk sorted events and produce IntervalRecords.

    State tracking:
        - state_X: Markov state (0=11v11, 1=10v11, 2=11v10, 3=10v10)
        - delta_S: score difference (home - away)
        - in_halftime: whether we're in the halftime break
    """
    intervals: list[IntervalRecord] = []

    state_X = 0
    delta_S = 0
    t_cursor = 0.0
    in_halftime = False

    # Accumulators for goal events within the current interval
    home_goals_in_interval: list[float] = []
    away_goals_in_interval: list[float] = []
    goal_delta_before: list[int] = []
    goal_is_owngoal: list[bool] = []

    def _flush_interval(t_end: float, is_ht: bool = False) -> None:
        """Close the current interval and append to results."""
        nonlocal t_cursor, home_goals_in_interval, away_goals_in_interval
        nonlocal goal_delta_before, goal_is_owngoal

        if t_end <= t_cursor and not is_ht:
            return  # Zero-length interval, skip

        intervals.append(IntervalRecord(
            match_id=match_id,
            t_start=t_cursor,
            t_end=t_end,
            state_X=state_X,
            delta_S=delta_S,
            home_goal_times=list(home_goals_in_interval),
            away_goal_times=list(away_goals_in_interval),
            goal_delta_before=list(goal_delta_before),
            goal_is_owngoal=list(goal_is_owngoal),
            T_m=T_m,
            is_halftime=is_ht,
            alpha_1=alpha_1,
            alpha_2=alpha_2,
        ))

        t_cursor = t_end
        home_goals_in_interval = []
        away_goals_in_interval = []
        goal_delta_before = []
        goal_is_owngoal = []

    for event in events:
        if event.kind == "halftime_start":
            if not in_halftime:
                _flush_interval(event.minute)
                in_halftime = True
                t_cursor = event.minute

        elif event.kind == "halftime_end":
            if in_halftime:
                _flush_interval(event.minute, is_ht=True)
                in_halftime = False

        elif event.kind == "match_end":
            if not in_halftime:
                _flush_interval(event.minute)

        elif in_halftime:
            # Events during halftime are ignored for interval splitting
            continue

        elif event.kind == "goal":
            # Record pre-goal ΔS (causality: use state BEFORE the goal)
            goal_delta_before.append(delta_S)
            goal_is_owngoal.append(event.is_owngoal)

            if event.team == "localteam":
                home_goals_in_interval.append(event.minute)
            else:
                away_goals_in_interval.append(event.minute)

            # Flush interval up to goal time, then update ΔS
            _flush_interval(event.minute)

            # Update score after flushing (so the flushed interval has pre-goal ΔS)
            if event.team == "localteam":
                delta_S += 1
            else:
                delta_S -= 1

        elif event.kind == "red_card":
            # Flush interval up to red card time, then update state
            _flush_interval(event.minute)

            # Transition Markov state
            if event.team == "localteam":
                # Home player sent off
                if state_X == 0:
                    state_X = 1   # 11v11 → 10v11
                elif state_X == 2:
                    state_X = 3   # 11v10 → 10v10
            else:
                # Away player sent off
                if state_X == 0:
                    state_X = 2   # 11v11 → 11v10
                elif state_X == 1:
                    state_X = 3   # 10v11 → 10v10

    return intervals


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def build_all_intervals(matches: list[dict]) -> list[IntervalRecord]:
    """Build intervals for a batch of match dicts.

    Args:
        matches: List of raw Goalserve match dicts.

    Returns:
        Flat list of all IntervalRecords across all matches.
    """
    all_intervals: list[IntervalRecord] = []
    for match in matches:
        try:
            intervals = build_intervals(match)
            all_intervals.extend(intervals)
        except Exception:
            # Log but don't fail the whole batch
            match_id = match.get("id", match.get("static_id", "unknown"))
            import sys
            print(f"Warning: failed to build intervals for match {match_id}",
                  file=sys.stderr)
    return all_intervals


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
