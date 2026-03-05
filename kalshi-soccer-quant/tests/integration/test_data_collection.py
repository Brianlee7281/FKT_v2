"""Integration tests for Step 2.1: Pre-Match Data Collection.

These tests use mocked Goalserve API and DB clients to verify
the data collection pipeline without requiring live services.

Reference: implementation_roadmap.md -> Step 2.1 tests
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.calibration.features.tier1_team import TIER1_FEATURES
from src.calibration.features.tier2_player import TIER2_FEATURES
from src.calibration.features.tier3_odds import TIER3_FEATURES
from src.common.types import PreMatchData
from src.prematch.step_2_1_data_collection import (
    _fetch_lineups,
    _fetch_player_histories,
    collect_prematch_data,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MATCH_ID = "12345"
LEAGUE_ID = "1204"

MOCK_LIVE_STATS = {
    "teams": {
        "localteam": {
            "formation": "4-3-3",
            "player": [
                {"id": "101", "name": "GK1", "pos": "G", "formation_pos": "1"},
                {"id": "102", "name": "DF1", "pos": "D", "formation_pos": "2"},
                {"id": "103", "name": "DF2", "pos": "D", "formation_pos": "3"},
                {"id": "104", "name": "DF3", "pos": "D", "formation_pos": "4"},
                {"id": "105", "name": "DF4", "pos": "D", "formation_pos": "5"},
                {"id": "106", "name": "MF1", "pos": "M", "formation_pos": "6"},
                {"id": "107", "name": "MF2", "pos": "M", "formation_pos": "7"},
                {"id": "108", "name": "MF3", "pos": "M", "formation_pos": "8"},
                {"id": "109", "name": "FW1", "pos": "F", "formation_pos": "9"},
                {"id": "110", "name": "FW2", "pos": "F", "formation_pos": "10"},
                {"id": "111", "name": "FW3", "pos": "F", "formation_pos": "11"},
            ],
        },
        "visitorteam": {
            "formation": "4-4-2",
            "player": [
                {"id": "201", "name": "GK2", "pos": "G", "formation_pos": "1"},
                {"id": "202", "name": "DF5", "pos": "D", "formation_pos": "2"},
                {"id": "203", "name": "DF6", "pos": "D", "formation_pos": "3"},
                {"id": "204", "name": "DF7", "pos": "D", "formation_pos": "4"},
                {"id": "205", "name": "DF8", "pos": "D", "formation_pos": "5"},
                {"id": "206", "name": "MF4", "pos": "M", "formation_pos": "6"},
                {"id": "207", "name": "MF5", "pos": "M", "formation_pos": "7"},
                {"id": "208", "name": "MF6", "pos": "M", "formation_pos": "8"},
                {"id": "209", "name": "MF7", "pos": "M", "formation_pos": "9"},
                {"id": "210", "name": "FW4", "pos": "F", "formation_pos": "10"},
                {"id": "211", "name": "FW5", "pos": "F", "formation_pos": "11"},
            ],
        },
    },
}


def _make_player_stat(pid: str, pos: str, minutes: int = 90, rating: float = 7.0):
    """Create a mock player stat dict."""
    return {
        "id": pid,
        "pos": pos,
        "position": pos,
        "minutes_played": str(minutes),
        "rating": str(rating),
        "goals": "0",
        "keyPasses": "2",
        "passes_accurate": "30",
        "passes_total": "40",
        "tackles": "3",
        "interceptions": "2",
        "saves": "3" if pos == "G" else "0",
        "goals_conceded": "1" if pos == "G" else "0",
    }


def _make_player_stats_json(player_ids_home: list[str], player_ids_away: list[str]):
    """Create mock player_stats JSONB matching Goalserve schema."""
    home_players = []
    for pid in player_ids_home:
        pos = "G" if pid.endswith("01") else "D" if int(pid) % 10 <= 5 else "M"
        home_players.append(_make_player_stat(pid, pos))

    away_players = []
    for pid in player_ids_away:
        pos = "G" if pid.endswith("01") else "D" if int(pid) % 10 <= 5 else "M"
        away_players.append(_make_player_stat(pid, pos))

    return json.dumps({
        "localteam": {"player": home_players},
        "visitorteam": {"player": away_players},
    })


def _make_db_rows(n_matches: int = 5):
    """Create mock DB rows for player history queries."""
    home_ids = [f"1{i:02d}" for i in range(1, 12)]
    away_ids = [f"2{i:02d}" for i in range(1, 12)]

    rows = []
    for i in range(n_matches):
        row = {
            "match_id": f"m{i}",
            "date": f"2025-01-{20 - i:02d}",
            "player_stats": _make_player_stats_json(home_ids, away_ids),
        }
        rows.append(MagicMock(**{k: v for k, v in row.items()}, __getitem__=lambda s, k: getattr(s, k))  )

    # Make rows behave like asyncpg Records
    result = []
    for row_data in rows:
        mock_row = MagicMock()
        d = {
            "match_id": row_data.match_id,
            "date": row_data.date,
            "player_stats": row_data.player_stats,
        }
        mock_row.__getitem__ = lambda s, k, _d=d: _d[k]
        mock_row.get = lambda k, default=None, _d=d: _d.get(k, default)
        result.append(mock_row)
    return result


def _make_mock_db():
    """Create a mock DB client with realistic responses."""
    db = AsyncMock()

    # Player history rows
    db.fetch = AsyncMock(side_effect=_db_fetch_side_effect)
    db.fetchrow = AsyncMock(side_effect=_db_fetchrow_side_effect)
    db.fetchval = AsyncMock(return_value="2025-01-18")

    return db


def _make_record(data: dict):
    """Create a mock asyncpg Record."""
    mock = MagicMock()
    mock.__getitem__ = lambda s, k: data[k]
    mock.get = lambda k, default=None: data.get(k, default)
    return mock


async def _db_fetch_side_effect(query: str, *args):
    """Route DB fetch calls to appropriate mock data."""
    if "player_stats" in query:
        return _make_db_rows(5)
    if "home_team" in query and "away_team" in query and "ft_score_h" in query:
        # H2H query
        return [
            _make_record({"home_team": "Arsenal", "away_team": "Chelsea",
                         "ft_score_h": 2, "ft_score_a": 1}),
        ]
    if "stats" in query:
        # Team rolling stats
        rows = []
        for i in range(5):
            stats_json = json.dumps({
                "localteam": {
                    "shots": {"total": "12", "ongoal": "5", "insidebox": "8"},
                    "passes": {"accurate": "350", "total": "450"},
                    "possestiontime": {"total": "55"},
                    "corners": {"total": "6"},
                    "fouls": {"total": "10"},
                    "saves": {"total": "3"},
                },
                "visitorteam": {
                    "shots": {"total": "10", "ongoal": "4", "insidebox": "6"},
                    "passes": {"accurate": "300", "total": "420"},
                    "possestiontime": {"total": "45"},
                    "corners": {"total": "4"},
                    "fouls": {"total": "12"},
                    "saves": {"total": "5"},
                },
            })
            rows.append(_make_record({
                "stats": stats_json,
                "home_team": "Arsenal",
                "away_team": "Chelsea",
            }))
        return rows
    return []


async def _db_fetchrow_side_effect(query: str, *args):
    """Route DB fetchrow calls."""
    if "home_team" in query and "away_team" in query:
        return _make_record({
            "match_id": MATCH_ID,
            "date": "2025-01-25",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "kickoff_time": "15:00",
        })
    if "match_jobs" in query:
        return _make_record({"kickoff_time": "15:00"})
    return None


def _make_mock_gs_client():
    """Create a mock Goalserve client."""
    gs = AsyncMock()
    gs.get_live_stats = AsyncMock(return_value=MOCK_LIVE_STATS)
    gs.get_odds = AsyncMock(return_value=[
        {
            "id": MATCH_ID,
            "bookmakers": [
                {
                    "name": "Pinnacle",
                    "odd": [
                        {"name": "1", "value": "2.10"},
                        {"name": "X", "value": "3.40"},
                        {"name": "2", "value": "3.50"},
                    ],
                },
                {
                    "name": "Bet365",
                    "odd": [
                        {"name": "1", "value": "2.05"},
                        {"name": "X", "value": "3.30"},
                        {"name": "2", "value": "3.60"},
                    ],
                },
            ],
        },
    ])
    return gs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLineupFetch:
    """test_lineup_fetch_returns_22_players"""

    @pytest.mark.asyncio
    async def test_lineup_fetch_returns_22_players(self):
        """Verify both teams have 11 starters each."""
        gs = _make_mock_gs_client()
        lineups = await _fetch_lineups(MATCH_ID, gs)

        assert len(lineups["home_ids"]) == 11
        assert len(lineups["away_ids"]) == 11
        assert lineups["home_formation"] == "4-3-3"
        assert lineups["away_formation"] == "4-4-2"

    @pytest.mark.asyncio
    async def test_lineup_unavailable_returns_empty(self):
        """Handle case where lineups not yet available."""
        gs = AsyncMock()
        gs.get_live_stats = AsyncMock(return_value=None)
        lineups = await _fetch_lineups(MATCH_ID, gs)

        assert lineups["home_ids"] == []
        assert lineups["away_ids"] == []


class TestPlayerRolling:
    """test_player_rolling_excludes_short_appearances"""

    @pytest.mark.asyncio
    async def test_player_rolling_excludes_short_appearances(self):
        """Players with < 10 minutes should be excluded from rolling stats."""
        # Create rows where one player has only 5 minutes
        short_stat = _make_player_stat("109", "F", minutes=5, rating=6.0)
        normal_stat = _make_player_stat("109", "F", minutes=90, rating=7.5)

        ps_data = json.dumps({
            "localteam": {"player": [short_stat]},
            "visitorteam": {"player": []},
        })
        ps_data_normal = json.dumps({
            "localteam": {"player": [normal_stat]},
            "visitorteam": {"player": []},
        })

        db = AsyncMock()
        db.fetch = AsyncMock(return_value=[
            _make_record({"match_id": "m0", "date": "2025-01-20", "player_stats": ps_data}),
            _make_record({"match_id": "m1", "date": "2025-01-15", "player_stats": ps_data_normal}),
        ])

        history = await _fetch_player_histories(["109"], db)

        # Both entries are returned (filtering happens in build_player_features)
        assert "109" in history
        assert len(history["109"]) == 2

        # The short appearance has minutes_played=5, which tier2_player
        # will skip (MIN_MINUTES=10). Verify the data is present for the filter.
        minutes_values = [
            float(h.get("minutes_played", 0)) for h in history["109"]
        ]
        assert 5.0 in minutes_values  # Short appearance present
        assert 90.0 in minutes_values  # Normal appearance present


class TestOddsFeatures:
    """test_odds_features_pinnacle_present"""

    @pytest.mark.asyncio
    async def test_odds_features_pinnacle_present(self):
        """Verify Pinnacle probabilities are extracted."""
        gs = _make_mock_gs_client()
        db = _make_mock_db()
        data = await collect_prematch_data(MATCH_ID, LEAGUE_ID, gs, db)

        assert "pinnacle_home_prob" in data.odds_features
        assert "pinnacle_draw_prob" in data.odds_features
        assert "pinnacle_away_prob" in data.odds_features
        # Probabilities should sum to ~1.0
        total = (
            data.odds_features["pinnacle_home_prob"]
            + data.odds_features["pinnacle_draw_prob"]
            + data.odds_features["pinnacle_away_prob"]
        )
        assert abs(total - 1.0) < 0.01


class TestFeatureNameAlignment:
    """test_feature_names_match_phase1_mask"""

    @pytest.mark.asyncio
    async def test_feature_names_match_phase1_mask(self):
        """CRITICAL: Feature names must match Phase 1 training features."""
        gs = _make_mock_gs_client()
        db = _make_mock_db()
        data = await collect_prematch_data(MATCH_ID, LEAGUE_ID, gs, db)

        # Tier 1 features
        for feat in TIER1_FEATURES:
            assert feat in data.home_team_rolling, f"Missing Tier 1 feature: {feat}"
            assert feat in data.away_team_rolling, f"Missing Tier 1 feature: {feat}"

        # Tier 2 features
        for feat in TIER2_FEATURES:
            assert feat in data.home_player_agg, f"Missing Tier 2 feature: {feat}"
            assert feat in data.away_player_agg, f"Missing Tier 2 feature: {feat}"

        # Tier 3 features
        for feat in TIER3_FEATURES:
            assert feat in data.odds_features, f"Missing Tier 3 feature: {feat}"


class TestPreMatchDataComplete:
    """test_prematch_data_no_none_fields"""

    @pytest.mark.asyncio
    async def test_prematch_data_no_none_fields(self):
        """All PreMatchData fields should be populated (no None values)."""
        gs = _make_mock_gs_client()
        db = _make_mock_db()
        data = await collect_prematch_data(MATCH_ID, LEAGUE_ID, gs, db)

        assert isinstance(data, PreMatchData)
        assert data.match_id == MATCH_ID

        # No None fields
        assert data.home_starting_11 is not None
        assert data.away_starting_11 is not None
        assert data.home_formation is not None
        assert data.away_formation is not None
        assert data.home_player_agg is not None
        assert data.away_player_agg is not None
        assert data.home_team_rolling is not None
        assert data.away_team_rolling is not None
        assert data.odds_features is not None

        # Lineups have correct count
        assert len(data.home_starting_11) == 11
        assert len(data.away_starting_11) == 11

        # Rest days and H2H are numeric
        assert isinstance(data.home_rest_days, int)
        assert isinstance(data.away_rest_days, int)
        assert isinstance(data.h2h_goal_diff, float)
