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
            List of normalized match dicts from all leagues.
        """
        data = await self._get("soccerfixtures/date", {"d": date})
        matches = []
        root = data.get("results", data.get("scores", {}))
        tournaments = root.get("tournament", [])
        if isinstance(tournaments, dict):
            tournaments = [tournaments]
        for tournament in tournaments:
            league_id = tournament.get("@id", tournament.get("league_id", ""))
            # Handle week-based or direct match structure
            weeks = tournament.get("week", [])
            if weeks:
                if isinstance(weeks, dict):
                    weeks = [weeks]
                for week in weeks:
                    raw = week.get("match", [])
                    if isinstance(raw, dict):
                        raw = [raw]
                    for m in raw:
                        normalized = _normalize_at_keys(m)
                        normalized["league_id"] = league_id
                        matches.append(normalized)
            else:
                raw = tournament.get("match", [])
                if isinstance(raw, dict):
                    raw = [raw]
                for m in raw:
                    normalized = _normalize_at_keys(m)
                    normalized["league_id"] = league_id
                    matches.append(normalized)
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
            List of normalized match dicts with bookmaker odds.
        """
        params = {}
        if date:
            params["date"] = date

        data = await self._get(f"soccernew/{league_id}", params)
        matches = []
        root = data.get("results", data.get("scores", {}))
        categories = root.get("category", root.get("tournament", {}))
        if isinstance(categories, dict):
            raw = categories.get("match", [])
            if isinstance(raw, dict):
                raw = [raw]
            for m in raw:
                normalized = _normalize_at_keys(m)
                normalized["league_id"] = league_id
                matches.append(normalized)
        elif isinstance(categories, list):
            for cat in categories:
                raw = cat.get("match", [])
                if isinstance(raw, dict):
                    raw = [raw]
                for m in raw:
                    normalized = _normalize_at_keys(m)
                    normalized["league_id"] = league_id
                    matches.append(normalized)
        return matches

    # ── Live Score API (REST polling) ──

    async def get_live_scores(self) -> list[dict]:
        """Fetch all live match scores (for polling).

        Returns:
            List of normalized live match dicts.
        """
        data = await self._get("soccerlive/home")
        matches = []
        root = data.get("results", data.get("scores", {}))
        tournaments = root.get("tournament", [])
        if isinstance(tournaments, dict):
            tournaments = [tournaments]
        for tournament in tournaments:
            league_id = tournament.get("@id", tournament.get("league_id", ""))
            t_matches = tournament.get("match", [])
            if isinstance(t_matches, dict):
                t_matches = [t_matches]
            for m in t_matches:
                normalized = _normalize_at_keys(m)
                normalized["league_id"] = league_id
                matches.append(normalized)
        return matches

    async def get_live_score_for_match(self, match_id: str) -> dict | None:
        """Fetch live score data for a specific match."""
        all_live = await self.get_live_scores()
        for m in all_live:
            if m.get("id") == match_id or m.get("static_id") == match_id:
                return m
        return None


def _normalize_at_keys(obj: Any) -> Any:
    """Recursively strip '@' prefix from dict keys in Goalserve responses."""
    if isinstance(obj, dict):
        return {k.lstrip("@"): _normalize_at_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_at_keys(item) for item in obj]
    return obj


def _extract_matches(data: dict, league_id: str) -> list[dict]:
    """Extract match list from Goalserve fixtures response.

    Handles the actual Goalserve response shape:
      results.tournament.week[].match[]
    Also normalizes @-prefixed keys (e.g. @status -> status).
    """
    matches = []
    raw_matches = []

    # Primary path: results.tournament.week[].match[]
    results = data.get("results", data.get("scores", data))
    if isinstance(results, dict):
        tournament = results.get("tournament", results.get("category", {}))
        if isinstance(tournament, dict):
            # Check for week-based structure
            weeks = tournament.get("week", [])
            if weeks:
                if isinstance(weeks, dict):
                    weeks = [weeks]
                for week in weeks:
                    week_matches = week.get("match", [])
                    if isinstance(week_matches, dict):
                        week_matches = [week_matches]
                    raw_matches.extend(week_matches)
            else:
                # Fallback: direct match list
                direct = tournament.get("match", [])
                if isinstance(direct, dict):
                    direct = [direct]
                raw_matches.extend(direct)
        elif isinstance(tournament, list):
            for t in tournament:
                t_matches = t.get("match", [])
                if isinstance(t_matches, dict):
                    t_matches = [t_matches]
                raw_matches.extend(t_matches)

    # Normalize @-prefixed keys and tag with league_id
    for m in raw_matches:
        normalized = _normalize_at_keys(m)
        normalized["league_id"] = league_id
        matches.append(normalized)

    return matches
