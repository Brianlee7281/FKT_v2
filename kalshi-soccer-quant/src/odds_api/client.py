"""Odds-API REST Client — Pregame & live odds from 265 bookmakers.

Replaces Goalserve for odds data while keeping the same output format
so Tier 3 features can consume data from either source.

Endpoints used:
  GET /events       — match lookup
  GET /odds         — single-event odds from selected bookmakers
  GET /odds/multi   — batch odds (up to 10 events, counts as 1 call)
  GET /odds/movements — historical line movements
  GET /value-bets   — pre-computed EV opportunities
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.common.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 30


class OddsApiClient:
    """Async REST client for Odds-API.io v3."""

    def __init__(self, api_key: str, base_url: str = "https://api.odds-api.io/v3"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """Execute GET request with API key injection."""
        client = await self._get_client()
        if params is None:
            params = {}
        params["apiKey"] = self._api_key

        url = f"{self._base_url}{path}"
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def get_events(
        self,
        sport: str = "football",
        league: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Fetch events with optional filtering."""
        params: dict[str, str] = {"sport": sport}
        if league:
            params["league"] = league
        if status:
            params["status"] = status
        return await self._get("/events", params)

    async def get_live_events(self, sport: str = "football") -> list[dict]:
        """Fetch currently live events."""
        return await self._get("/events/live", {"sport": sport})

    async def search_events(self, query: str) -> list[dict]:
        """Search events by team name or text."""
        return await self._get("/events/search", {"query": query})

    async def get_event(self, event_id: int) -> dict:
        """Fetch a single event by ID."""
        return await self._get(f"/events/{event_id}")

    # ------------------------------------------------------------------
    # Odds
    # ------------------------------------------------------------------

    async def get_odds(
        self,
        event_id: int,
        bookmakers: list[str],
    ) -> dict:
        """Fetch odds for a single event from selected bookmakers.

        Args:
            event_id: Odds-API event ID.
            bookmakers: List of bookmaker names (max 30).

        Returns:
            Event dict with nested bookmakers → markets → odds.
        """
        return await self._get("/odds", {
            "eventId": str(event_id),
            "bookmakers": ",".join(bookmakers[:30]),
        })

    async def get_odds_multi(
        self,
        event_ids: list[int],
        bookmakers: list[str],
    ) -> list[dict]:
        """Batch fetch odds for up to 10 events (counts as 1 API call)."""
        return await self._get("/odds/multi", {
            "eventIds": ",".join(str(eid) for eid in event_ids[:10]),
            "bookmakers": ",".join(bookmakers[:30]),
        })

    async def get_odds_movements(
        self,
        event_id: int,
        bookmaker: str,
        market: str = "ML",
        market_line: float | None = None,
    ) -> dict:
        """Fetch historical odds movements for a specific market."""
        params: dict[str, str] = {
            "eventId": str(event_id),
            "bookmaker": bookmaker,
            "market": market,
        }
        if market_line is not None:
            params["marketLine"] = str(market_line)
        return await self._get("/odds/movements", params)

    async def get_odds_updated(
        self,
        since: int,
        bookmaker: str,
        sport: str = "Football",
    ) -> list[dict]:
        """Fetch odds updated since a unix timestamp (max 1 min old)."""
        return await self._get("/odds/updated", {
            "since": str(since),
            "bookmaker": bookmaker,
            "sport": sport,
        })

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    async def get_value_bets(
        self,
        bookmaker: str,
        include_event_details: bool = True,
    ) -> list[dict]:
        """Fetch value betting opportunities."""
        return await self._get("/value-bets", {
            "bookmaker": bookmaker,
            "includeEventDetails": str(include_event_details).lower(),
        })

    async def get_arbitrage_bets(
        self,
        bookmakers: list[str],
        limit: int = 50,
        include_event_details: bool = True,
    ) -> list[dict]:
        """Fetch arbitrage opportunities across bookmakers."""
        return await self._get("/arbitrage-bets", {
            "bookmakers": ",".join(bookmakers),
            "limit": str(limit),
            "includeEventDetails": str(include_event_details).lower(),
        })

    # ------------------------------------------------------------------
    # Bookmaker Management
    # ------------------------------------------------------------------

    async def select_bookmakers(self, bookmakers: list[str]) -> None:
        """Add bookmakers to the user's WebSocket selection."""
        client = await self._get_client()
        url = f"{self._base_url}/bookmakers/selected/select"
        resp = await client.put(url, params={
            "apiKey": self._api_key,
            "bookmakers": ",".join(bookmakers),
        })
        resp.raise_for_status()
        log.info("odds_api_bookmakers_selected", bookmakers=bookmakers)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def get_leagues(self, sport: str = "football") -> list[dict]:
        """Fetch available leagues for a sport."""
        return await self._get("/leagues", {"sport": sport})

    async def get_participants(
        self, sport: str = "football", search: str | None = None
    ) -> list[dict]:
        """Fetch teams/participants."""
        params: dict[str, str] = {"sport": sport}
        if search:
            params["search"] = search
        return await self._get("/participants", params)
