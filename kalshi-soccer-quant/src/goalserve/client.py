"""Goalserve REST API client for Fixtures, Stats, and Odds."""

from __future__ import annotations

from typing import Any

import httpx

from src.common.logging import get_logger

log = get_logger(__name__)

# Default timeout for all requests
DEFAULT_TIMEOUT = 30.0


class GoalserveClient:
    """REST client for the Goalserve Full Soccer Package.

    Covers three APIs:
      1. Fixtures/Results — historical match data, goals, red cards, lineups
      2. Live Game Stats — detailed team/player stats, xG
      3. Pregame Odds — 20+ bookmakers, 50+ markets
    """

    def __init__(self, api_key: str,
                 base_url: str = "http://www.goalserve.com/getfeed"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Make a GET request and return parsed JSON."""
        client = await self._ensure_client()
        url = f"{self._base_url}/{self._api_key}/{path}"
        merged_params = {"json": 1}
        if params:
            merged_params.update(params)

        response = await client.get(url, params=merged_params)
        response.raise_for_status()
        return response.json()

    # ── Fixtures / Results API ──

    async def get_fixtures(self, league_id: str,
                           date: str | None = None) -> list[dict]:
        """Fetch fixtures/results for a league.

        Args:
            league_id: Goalserve league ID (e.g., "1204" for EPL).
            date: Optional date filter (format: "dd.mm.yyyy").

        Returns:
            List of match dicts.
        """
        params = {}
        if date:
            params["date"] = date

        data = await self._get(f"soccerfixtures/league/{league_id}", params)
        return _extract_matches(data, league_id)

    async def get_fixtures_by_date(self, date: str) -> list[dict]:
        """Fetch all fixtures across all leagues for a date.

        Args:
            date: Date in "dd.mm.yyyy" format.

        Returns:
            List of match dicts from all leagues.
        """
        data = await self._get("soccerfixtures/date", {"d": date})
        matches = []
        tournaments = data.get("scores", {}).get("tournament", [])
        if isinstance(tournaments, dict):
            tournaments = [tournaments]
        for tournament in tournaments:
            league_id = tournament.get("league_id", "")
            tournament_matches = tournament.get("match", [])
            if isinstance(tournament_matches, dict):
                tournament_matches = [tournament_matches]
            for m in tournament_matches:
                m["league_id"] = league_id
            matches.extend(tournament_matches)
        return matches

    # ── Live Game Stats API ──

    async def get_match_stats(self, match_id: str) -> dict | None:
        """Fetch detailed match stats (team + player level).

        Args:
            match_id: Goalserve match ID.

        Returns:
            Dict with 'stats' and 'player_stats' keys, or None if unavailable.
        """
        try:
            data = await self._get(f"soccerstats/match/{match_id}")
            match_data = data.get("match", data)
            if not match_data or match_data == {}:
                return None
            return match_data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("match_stats_not_found", match_id=match_id)
                return None
            raise

    async def get_live_stats(self, match_id: str) -> dict | None:
        """Fetch live game stats (lineups + formation) for an upcoming/live match."""
        try:
            data = await self._get(f"soccerstats/match/{match_id}")
            return data.get("match", data)
        except httpx.HTTPStatusError:
            return None

    # ── Pregame Odds API ──

    async def get_odds(self, league_id: str,
                       date: str | None = None) -> list[dict]:
        """Fetch pregame odds for a league.

        Args:
            league_id: Goalserve league ID.
            date: Optional date filter.

        Returns:
            List of match dicts with bookmaker odds.
        """
        params = {}
        if date:
            params["date"] = date

        data = await self._get(f"soccernew/{league_id}", params)
        matches = []
        categories = data.get("scores", {}).get("category", {})
        tournament_matches = categories.get("match", [])
        if isinstance(tournament_matches, dict):
            tournament_matches = [tournament_matches]
        for m in tournament_matches:
            m["league_id"] = league_id
        matches.extend(tournament_matches)
        return matches

    # ── Live Score API (REST polling) ──

    async def get_live_scores(self) -> list[dict]:
        """Fetch all live match scores (for polling).

        Returns:
            List of live match dicts.
        """
        data = await self._get("soccerlive/home")
        matches = []
        tournaments = data.get("scores", {}).get("tournament", [])
        if isinstance(tournaments, dict):
            tournaments = [tournaments]
        for tournament in tournaments:
            league_id = tournament.get("league_id", "")
            t_matches = tournament.get("match", [])
            if isinstance(t_matches, dict):
                t_matches = [t_matches]
            for m in t_matches:
                m["league_id"] = league_id
            matches.extend(t_matches)
        return matches

    async def get_live_score_for_match(self, match_id: str) -> dict | None:
        """Fetch live score data for a specific match."""
        all_live = await self.get_live_scores()
        for m in all_live:
            if m.get("id") == match_id or m.get("static_id") == match_id:
                return m
        return None


def _extract_matches(data: dict, league_id: str) -> list[dict]:
    """Extract match list from Goalserve fixtures response."""
    matches = []
    # Navigate various response shapes
    scores = data.get("scores", data)
    if isinstance(scores, dict):
        category = scores.get("category", scores.get("tournament", {}))
        if isinstance(category, dict):
            raw_matches = category.get("match", [])
        elif isinstance(category, list):
            raw_matches = []
            for cat in category:
                cat_matches = cat.get("match", [])
                if isinstance(cat_matches, dict):
                    cat_matches = [cat_matches]
                raw_matches.extend(cat_matches)
        else:
            raw_matches = []
    else:
        raw_matches = []

    if isinstance(raw_matches, dict):
        raw_matches = [raw_matches]

    for m in raw_matches:
        m["league_id"] = league_id
        matches.append(m)

    return matches
