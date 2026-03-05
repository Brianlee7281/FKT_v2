"""Goalserve Live Score REST Source — Authoritative Confirmation (3-8s).

Polls Goalserve Live Score REST endpoint every 3 seconds and yields
NormalizedEvents for confirmed goals, red cards, period changes,
and match completion.

Provides authoritative data that Live Odds WebSocket cannot:
  - Goal scorer identity
  - Own goal / penalty flags
  - VAR cancellation status
  - Red card details

Reference: phase3.md -> Step 3.1 -> Source 2
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import httpx

from src.common.logging import get_logger
from src.common.types import NormalizedEvent
from src.goalserve.live_odds_source import EventSource
from src.goalserve.parsers import ensure_list

log = get_logger(__name__)


# Max consecutive failures before yielding source_failure
MAX_CONSECUTIVE_FAILURES = 5

# Default polling interval (seconds)
DEFAULT_POLL_INTERVAL = 3.0


class GoalserveLiveScoreSource(EventSource):
    """REST polling source for Goalserve Live Score (3-8s latency).

    Detects:
      - Confirmed goals (goal_confirmed) with scorer and VAR status
      - Red cards (red_card)
      - Period changes (period_change) — halftime, full time
      - Match finished (match_finished)
      - Source failure after 5 consecutive poll errors
    """

    def __init__(
        self,
        api_key: str,
        match_id: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self._api_key = api_key
        self._match_id = match_id
        self._poll_interval = poll_interval
        self._running: bool = False

        # State tracking for diff detection
        self._last_home_goals: int = 0
        self._last_away_goals: int = 0
        self._last_home_reds: int = 0
        self._last_away_reds: int = 0
        self._last_status: str | None = None
        self._consecutive_failures: int = 0

    @property
    def match_id(self) -> str:
        return self._match_id

    async def connect(self, match_id: str) -> None:
        """Initialize the source for a match."""
        self._match_id = match_id
        self._running = True
        self._consecutive_failures = 0
        log.info("live_score_source_initialized", match_id=match_id)

    async def disconnect(self) -> None:
        """Stop polling."""
        self._running = False

    async def listen(self) -> AsyncIterator[NormalizedEvent]:
        """Poll Live Score endpoint and yield events on state changes."""
        self._running = True
        base_url = (
            f"http://www.goalserve.com/getfeed/{self._api_key}/soccerlive/home"
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            while self._running:
                try:
                    response = await client.get(base_url, params={"json": 1})
                    response.raise_for_status()
                    data = response.json()

                    match = self._find_match(data)
                    if match:
                        self._consecutive_failures = 0
                        async for event in self._diff(match):
                            yield event

                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    self._consecutive_failures += 1
                    log.warning(
                        "live_score_poll_failed",
                        error=str(e),
                        failures=self._consecutive_failures,
                    )

                    if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        yield NormalizedEvent(
                            type="source_failure",
                            source="live_score",
                            confidence="confirmed",
                            timestamp=time.time(),
                        )
                        break

                except Exception as e:
                    log.error("live_score_unexpected_error", error=str(e))
                    self._consecutive_failures += 1

                await asyncio.sleep(self._poll_interval)

    # -----------------------------------------------------------------------
    # Match finder
    # -----------------------------------------------------------------------

    def _find_match(self, data: dict) -> dict | None:
        """Find our match in the Live Score response."""
        tournaments = data.get("scores", {}).get("tournament", [])
        if isinstance(tournaments, dict):
            tournaments = [tournaments]

        for tournament in tournaments:
            matches = ensure_list(tournament.get("match", []))
            for m in matches:
                mid = m.get("id", m.get("static_id", ""))
                if str(mid) == str(self._match_id):
                    return m
        return None

    # -----------------------------------------------------------------------
    # Diff detection
    # -----------------------------------------------------------------------

    async def _diff(self, match: dict) -> AsyncIterator[NormalizedEvent]:
        """Compare current match state with previous and yield changes."""

        # --- Goal detection (confirmed) ---
        async for evt in self._detect_goals(match):
            yield evt

        # --- Red card detection ---
        async for evt in self._detect_red_cards(match):
            yield evt

        # --- Period / status change ---
        evt = self._detect_status_change(match)
        if evt:
            yield evt

    async def _detect_goals(self, match: dict) -> AsyncIterator[NormalizedEvent]:
        """Detect confirmed goals from score changes."""
        local = match.get("localteam", {})
        visitor = match.get("visitorteam", {})

        home_goals = _safe_int(local.get("goals", 0))
        away_goals = _safe_int(visitor.get("goals", 0))

        # Extract event details for enrichment
        events = ensure_list(
            match.get("events", {}).get("event", [])
        )
        minute_str = match.get("timer", match.get("status", ""))

        # Home goals increased
        if home_goals > self._last_home_goals:
            for _ in range(home_goals - self._last_home_goals):
                goal_detail = self._find_latest_goal_event(events, "localteam")
                yield NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    score=(home_goals, away_goals),
                    team="localteam",
                    var_cancelled=False,
                    timestamp=time.time(),
                    minute=_parse_minute_str(minute_str),
                    scorer_id=goal_detail.get("player_id"),
                    extra=goal_detail,
                )

        # Away goals increased
        if away_goals > self._last_away_goals:
            for _ in range(away_goals - self._last_away_goals):
                goal_detail = self._find_latest_goal_event(events, "visitorteam")
                yield NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    score=(home_goals, away_goals),
                    team="visitorteam",
                    var_cancelled=False,
                    timestamp=time.time(),
                    minute=_parse_minute_str(minute_str),
                    scorer_id=goal_detail.get("player_id"),
                    extra=goal_detail,
                )

        # Score decreased -> VAR cancellation detected
        if home_goals < self._last_home_goals or away_goals < self._last_away_goals:
            yield NormalizedEvent(
                type="goal_confirmed",
                source="live_score",
                confidence="confirmed",
                score=(home_goals, away_goals),
                var_cancelled=True,
                timestamp=time.time(),
                minute=_parse_minute_str(minute_str),
            )

        self._last_home_goals = home_goals
        self._last_away_goals = away_goals

    async def _detect_red_cards(self, match: dict) -> AsyncIterator[NormalizedEvent]:
        """Detect red cards from live stats."""
        home_reds, away_reds = self._extract_red_card_counts(match)

        if home_reds > self._last_home_reds:
            for _ in range(home_reds - self._last_home_reds):
                yield NormalizedEvent(
                    type="red_card",
                    source="live_score",
                    confidence="confirmed",
                    team="localteam",
                    timestamp=time.time(),
                )

        if away_reds > self._last_away_reds:
            for _ in range(away_reds - self._last_away_reds):
                yield NormalizedEvent(
                    type="red_card",
                    source="live_score",
                    confidence="confirmed",
                    team="visitorteam",
                    timestamp=time.time(),
                )

        self._last_home_reds = home_reds
        self._last_away_reds = away_reds

    def _detect_status_change(self, match: dict) -> NormalizedEvent | None:
        """Detect period/status transitions."""
        status = match.get("status", "")
        if not status or status == self._last_status:
            return None

        evt = None
        if status == "HT":
            evt = NormalizedEvent(
                type="period_change",
                source="live_score",
                confidence="confirmed",
                period="Halftime",
                timestamp=time.time(),
            )
        elif status in ("Finished", "FT", "AET"):
            evt = NormalizedEvent(
                type="match_finished",
                source="live_score",
                confidence="confirmed",
                timestamp=time.time(),
            )
        elif self._last_status == "HT" and status not in ("HT", "Finished", "FT"):
            # Resuming from halftime
            evt = NormalizedEvent(
                type="period_change",
                source="live_score",
                confidence="confirmed",
                period="2nd Half",
                timestamp=time.time(),
            )

        self._last_status = status
        return evt

    # -----------------------------------------------------------------------
    # Extraction helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _find_latest_goal_event(events: list[dict], team: str) -> dict:
        """Find the most recent goal event for a team."""
        for event in reversed(events):
            etype = event.get("type", "").lower()
            eteam = event.get("team", "")
            if etype == "goal" and eteam == team:
                return {
                    "player_id": event.get("id", ""),
                    "player_name": event.get("player", ""),
                    "minute": event.get("minute", ""),
                    "is_owngoal": event.get("own_goal", "") == "True",
                    "is_penalty": event.get("penalty", "") == "True",
                }
        return {}

    @staticmethod
    def _extract_red_card_counts(match: dict) -> tuple[int, int]:
        """Extract red card counts from live_stats or events."""
        # Try live_stats first (structured format)
        stats = match.get("stats", match.get("live_stats", {}))
        if isinstance(stats, dict):
            home_reds = _safe_int(stats.get("localteam", {}).get("redcards", 0))
            away_reds = _safe_int(stats.get("visitorteam", {}).get("redcards", 0))
            if home_reds > 0 or away_reds > 0:
                return home_reds, away_reds

        # Fallback: count red card events
        events = ensure_list(match.get("events", {}).get("event", []))
        home_reds = 0
        away_reds = 0
        for event in events:
            if event.get("type", "").lower() in ("redcard", "red card", "yellowred"):
                team = event.get("team", "")
                if team == "localteam":
                    home_reds += 1
                elif team == "visitorteam":
                    away_reds += 1

        return home_reds, away_reds


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_int(val) -> int:
    if val is None or val == "":
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _parse_minute_str(minute_str: str) -> float | None:
    if not minute_str:
        return None
    try:
        return float(minute_str)
    except (ValueError, TypeError):
        return None
