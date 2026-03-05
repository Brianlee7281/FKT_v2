"""Step 2.1: Pre-Match Context Data Collection.

Collects all data needed for pre-match initialization:
  - Lineups + formation from Goalserve Live Game Stats
  - Player rolling stats (last 5 matches per starter)
  - Team rolling stats (last 5 matches)
  - Pregame odds (20+ bookmakers)
  - Context features (home/away, rest days, H2H)

Input:  match_id
Output: PreMatchData

Reference: phase2.md -> Step 2.1
"""

from __future__ import annotations

import json
from typing import Any

from src.calibration.features.tier1_team import build_team_features
from src.calibration.features.tier2_player import build_player_features
from src.calibration.features.tier3_odds import build_odds_features
from src.calibration.features.tier4_context import build_context_features
from src.common.db_client import DBClient
from src.common.logging import get_logger
from src.common.types import PreMatchData
from src.goalserve.client import GoalserveClient
from src.goalserve.parsers import ensure_list

log = get_logger(__name__)


async def collect_prematch_data(
    match_id: str,
    league_id: str,
    gs_client: GoalserveClient,
    db: DBClient,
) -> PreMatchData:
    """Collect all pre-match data for a given match.

    Should be called ~60 minutes before kickoff when lineups are available.

    Args:
        match_id: Goalserve match ID.
        league_id: Goalserve league ID (for odds lookup).
        gs_client: Goalserve API client.
        db: Database client.

    Returns:
        Fully populated PreMatchData instance.
    """
    # 2.1.1: Fetch lineups + formation
    lineups = await _fetch_lineups(match_id, gs_client)
    home_starting_11 = lineups["home_ids"]
    away_starting_11 = lineups["away_ids"]

    log.info(
        "lineups_fetched",
        match_id=match_id,
        home_count=len(home_starting_11),
        away_count=len(away_starting_11),
    )

    # 2.1.2 + 2.1.3: Player rolling stats -> position-weighted aggregation
    home_player_history = await _fetch_player_histories(home_starting_11, db)
    away_player_history = await _fetch_player_histories(away_starting_11, db)

    home_player_agg = build_player_features(home_starting_11, home_player_history)
    away_player_agg = build_player_features(away_starting_11, away_player_history)

    # 2.1.4: Team rolling stats
    home_team_stats = await _fetch_team_rolling_stats(
        match_id, "localteam", db
    )
    away_team_stats = await _fetch_team_rolling_stats(
        match_id, "visitorteam", db
    )

    home_team_rolling = build_team_features(home_team_stats)
    away_team_rolling = build_team_features(away_team_stats)

    # 2.1.5: Odds features
    odds_features = await _fetch_odds_features(match_id, league_id, gs_client)

    # 2.1.6: Context features
    match_meta = await _fetch_match_metadata(match_id, db)
    home_context = build_context_features(
        is_home=True,
        match_date=match_meta.get("date", ""),
        team_prev_date=match_meta.get("home_prev_date"),
        opp_prev_date=match_meta.get("away_prev_date"),
        h2h_matches=match_meta.get("h2h_matches"),
    )
    away_context = build_context_features(
        is_home=False,
        match_date=match_meta.get("date", ""),
        team_prev_date=match_meta.get("away_prev_date"),
        opp_prev_date=match_meta.get("home_prev_date"),
        h2h_matches=match_meta.get("h2h_matches"),
    )

    return PreMatchData(
        home_starting_11=home_starting_11,
        away_starting_11=away_starting_11,
        home_formation=lineups["home_formation"],
        away_formation=lineups["away_formation"],
        home_player_agg=home_player_agg,
        away_player_agg=away_player_agg,
        home_team_rolling=home_team_rolling,
        away_team_rolling=away_team_rolling,
        odds_features=odds_features,
        home_rest_days=int(home_context["rest_days"]),
        away_rest_days=int(away_context["rest_days"]),
        h2h_goal_diff=home_context["h2h_goal_diff"],
        match_id=match_id,
        kickoff_time=match_meta.get("kickoff_time", ""),
    )


# ---------------------------------------------------------------------------
# 2.1.1: Lineups
# ---------------------------------------------------------------------------

async def _fetch_lineups(
    match_id: str, gs_client: GoalserveClient
) -> dict[str, Any]:
    """Fetch starting XI and formation from Goalserve Live Game Stats."""
    match_data = await gs_client.get_live_stats(match_id)
    if not match_data:
        log.warning("lineups_unavailable", match_id=match_id)
        return {
            "home_ids": [],
            "away_ids": [],
            "home_formation": "",
            "away_formation": "",
        }

    teams = match_data.get("teams", {})

    home_team = teams.get("localteam", {})
    away_team = teams.get("visitorteam", {})

    home_players = ensure_list(home_team.get("player", []))
    away_players = ensure_list(away_team.get("player", []))

    return {
        "home_ids": [str(p.get("id", "")) for p in home_players if p.get("id")],
        "away_ids": [str(p.get("id", "")) for p in away_players if p.get("id")],
        "home_formation": home_team.get("formation", ""),
        "away_formation": away_team.get("formation", ""),
    }


# ---------------------------------------------------------------------------
# 2.1.2: Player rolling stats from DB
# ---------------------------------------------------------------------------

async def _fetch_player_histories(
    player_ids: list[str], db: DBClient, n_matches: int = 5
) -> dict[str, list[dict]]:
    """Fetch last N match stats for each player from historical_matches.

    Player stats are stored as JSONB in the player_stats column.
    We search across recent matches for each player ID.
    """
    if not player_ids:
        return {}

    result: dict[str, list[dict]] = {}

    # Query recent matches that have player_stats
    rows = await db.fetch(
        """
        SELECT match_id, date, player_stats
        FROM historical_matches
        WHERE player_stats IS NOT NULL
          AND status IN ('FT', 'AET', 'FT_PEN')
        ORDER BY date DESC
        LIMIT 200
        """,
    )

    # Build index: player_id -> list of (date, stats_dict)
    player_matches: dict[str, list[tuple[str, dict]]] = {
        pid: [] for pid in player_ids
    }

    for row in rows:
        ps_raw = row["player_stats"]
        if isinstance(ps_raw, str):
            try:
                ps_data = json.loads(ps_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            ps_data = ps_raw

        if not ps_data:
            continue

        match_date = row["date"]

        # Check both teams' player lists
        for team_key in ("localteam", "visitorteam"):
            team_ps = ps_data.get(team_key, {})
            players = ensure_list(team_ps.get("player", []))
            for p in players:
                pid = str(p.get("id", ""))
                if pid in player_matches:
                    player_matches[pid].append((match_date, p))

    # Take last N matches per player, sorted by date desc
    for pid in player_ids:
        matches = player_matches.get(pid, [])
        matches.sort(key=lambda x: x[0], reverse=True)
        result[pid] = [m[1] for m in matches[:n_matches]]

    return result


# ---------------------------------------------------------------------------
# 2.1.4: Team rolling stats from DB
# ---------------------------------------------------------------------------

async def _fetch_team_rolling_stats(
    match_id: str, team_key: str, db: DBClient, n_matches: int = 5
) -> list[dict]:
    """Fetch last N team-level stats for a team.

    Uses the stats JSONB column from historical_matches.
    Finds the team name from the current match, then queries
    recent matches where that team played.
    """
    # Get team name from current match
    current = await db.fetchrow(
        "SELECT home_team, away_team FROM historical_matches WHERE match_id = $1",
        match_id,
    )
    if not current:
        return []

    team_name = (
        current["home_team"] if team_key == "localteam" else current["away_team"]
    )
    if not team_name:
        return []

    rows = await db.fetch(
        """
        SELECT stats, home_team, away_team
        FROM historical_matches
        WHERE (home_team = $1 OR away_team = $1)
          AND match_id != $2
          AND stats IS NOT NULL
          AND status IN ('FT', 'AET', 'FT_PEN')
        ORDER BY date DESC
        LIMIT $3
        """,
        team_name,
        match_id,
        n_matches,
    )

    result = []
    for row in rows:
        stats_raw = row["stats"]
        if isinstance(stats_raw, str):
            try:
                stats_data = json.loads(stats_raw)
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            stats_data = stats_raw

        if not stats_data:
            continue

        # Determine which side this team was on
        side = "localteam" if row["home_team"] == team_name else "visitorteam"
        team_stats = stats_data.get(side, {})
        if team_stats:
            result.append(team_stats)

    return result


# ---------------------------------------------------------------------------
# 2.1.5: Odds features
# ---------------------------------------------------------------------------

async def _fetch_odds_features(
    match_id: str, league_id: str, gs_client: GoalserveClient
) -> dict[str, float]:
    """Fetch current pregame odds and build Tier 3 features."""
    try:
        odds_matches = await gs_client.get_odds(league_id)
    except Exception:
        log.warning("odds_fetch_failed", match_id=match_id, league_id=league_id)
        return {}

    # Find our match in the odds response
    for m in odds_matches:
        mid = m.get("id", m.get("static_id", ""))
        if str(mid) == str(match_id):
            bookmakers = ensure_list(m.get("bookmakers", m.get("odds", {}).get("type", [])))
            return build_odds_features(bookmakers)

    log.warning("odds_match_not_found", match_id=match_id, league_id=league_id)
    return {}


# ---------------------------------------------------------------------------
# 2.1.6: Match metadata for context features
# ---------------------------------------------------------------------------

async def _fetch_match_metadata(
    match_id: str, db: DBClient
) -> dict[str, Any]:
    """Fetch match date, previous match dates, and H2H history."""
    current = await db.fetchrow(
        """
        SELECT match_id, date, home_team, away_team, kickoff_time
        FROM historical_matches
        WHERE match_id = $1
        """,
        match_id,
    )

    meta: dict[str, Any] = {
        "date": "",
        "kickoff_time": "",
        "home_prev_date": None,
        "away_prev_date": None,
        "h2h_matches": [],
    }

    if not current:
        # Match not yet in DB (new match) — try match_jobs
        job = await db.fetchrow(
            "SELECT kickoff_time FROM match_jobs WHERE match_id = $1",
            match_id,
        )
        if job:
            meta["kickoff_time"] = job["kickoff_time"]
        return meta

    meta["date"] = current["date"]
    meta["kickoff_time"] = current.get("kickoff_time", "")
    home_team = current["home_team"]
    away_team = current["away_team"]

    # Previous match dates
    home_prev = await db.fetchval(
        """
        SELECT date FROM historical_matches
        WHERE (home_team = $1 OR away_team = $1)
          AND match_id != $2
          AND status IN ('FT', 'AET', 'FT_PEN')
        ORDER BY date DESC LIMIT 1
        """,
        home_team,
        match_id,
    )
    if home_prev:
        meta["home_prev_date"] = str(home_prev)

    away_prev = await db.fetchval(
        """
        SELECT date FROM historical_matches
        WHERE (home_team = $1 OR away_team = $1)
          AND match_id != $2
          AND status IN ('FT', 'AET', 'FT_PEN')
        ORDER BY date DESC LIMIT 1
        """,
        away_team,
        match_id,
    )
    if away_prev:
        meta["away_prev_date"] = str(away_prev)

    # H2H history (last 5 matches between these teams)
    h2h_rows = await db.fetch(
        """
        SELECT home_team, away_team, ft_score_h, ft_score_a
        FROM historical_matches
        WHERE ((home_team = $1 AND away_team = $2)
            OR (home_team = $2 AND away_team = $1))
          AND match_id != $3
          AND status IN ('FT', 'AET', 'FT_PEN')
        ORDER BY date DESC
        LIMIT 5
        """,
        home_team,
        away_team,
        match_id,
    )

    h2h_matches = []
    for row in h2h_rows:
        h2h_matches.append({
            "home_goals": row["ft_score_h"],
            "away_goals": row["ft_score_a"],
        })
    meta["h2h_matches"] = h2h_matches

    return meta
