"""Odds-API Live Odds WebSocket Source — Sub-100ms Event Detection.

Drop-in replacement for GoalserveLiveOddsSource. Connects to the
Odds-API WebSocket and yields the same NormalizedEvent types:
  - odds_spike (abrupt ML odds move)
  - score_change_hint (inferred from sudden odds collapse — not authoritative)

Unlike Goalserve, Odds-API WebSocket does NOT provide:
  - Match score, period, minute, ball position
  - These continue to come from Goalserve LiveScoreSource (REST poller)

The primary advantage is sub-100ms odds push from 265 bookmakers,
enabling faster odds_spike detection and richer consensus data.

WebSocket endpoint:
  wss://api.odds-api.io/v3/ws?apiKey=KEY&markets=ML,Spread,Totals&sport=football&status=live

Reference: docs/feeds_odds-api.txt → WEBSOCKET API
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

import websockets
import websockets.client

from src.common.logging import get_logger
from src.common.types import NormalizedEvent
from src.goalserve.live_odds_source import EventSource
from src.odds_api.parsers import parse_ws_odds_update

log = get_logger(__name__)

# Default odds-change threshold for spike detection (10%)
DEFAULT_ODDS_THRESHOLD = 0.10

# Max reconnect attempts before yielding source_failure
MAX_RECONNECT_ATTEMPTS = 5

# Delay between reconnect attempts (seconds)
RECONNECT_DELAY = 2.0


class OddsApiLiveOddsSource(EventSource):
    """WebSocket PUSH source for Odds-API (<100ms latency).

    Detects:
      - Abrupt odds moves (odds_spike) across multiple bookmakers
      - Potential score changes inferred from odds collapse (score_change_hint)

    Does NOT detect (use GoalserveLiveScoreSource instead):
      - Actual score changes
      - Period changes
      - Stoppage-time entry
    """

    def __init__(
        self,
        api_key: str,
        ws_url: str = "wss://api.odds-api.io/v3/ws",
        markets: str = "ML,Spread,Totals",
        league_slugs: list[str] | None = None,
        odds_threshold: float = DEFAULT_ODDS_THRESHOLD,
    ):
        self._api_key = api_key
        self._ws_url = ws_url
        self._markets = markets
        self._league_slugs = league_slugs
        self._odds_threshold = odds_threshold
        self._ws: websockets.client.WebSocketClientProtocol | None = None
        self._running: bool = False

        # Track last ML home odds per bookmaker for spike detection
        self._last_home_odds: dict[str, float] = {}

        # Track consensus odds for score-change inference
        self._last_consensus_home: float | None = None

        # Event ID filter (set via connect for per-match filtering)
        self._event_ids: list[str] = []

    async def connect(self, match_id: str) -> None:
        """Connect to Odds-API WebSocket.

        Args:
            match_id: Used as event_id filter for the WebSocket.
                      Can be an Odds-API event ID or comma-separated IDs.
        """
        self._event_ids = [match_id] if match_id else []

        params = {
            "apiKey": self._api_key,
            "markets": self._markets,
            "sport": "football",
            "status": "live",
        }

        # Prefer event ID filtering for per-match engines
        if self._event_ids:
            params["eventIds"] = ",".join(self._event_ids)
        elif self._league_slugs:
            params["leagues"] = ",".join(self._league_slugs)

        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._ws_url}?{query}"

        self._ws = await websockets.connect(url)

        # Wait for welcome message
        first_msg = await asyncio.wait_for(self._ws.recv(), timeout=10)
        parsed = json.loads(first_msg)

        if parsed.get("type") != "welcome":
            raise ConnectionError(
                f"Odds-API WS: expected welcome, got {parsed.get('type')}"
            )

        log.info(
            "odds_api_ws_connected",
            match_id=match_id,
            bookmakers=parsed.get("bookmakers", []),
            sport_filter=parsed.get("sport_filter", []),
            status_filter=parsed.get("status_filter", ""),
        )

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def listen(self) -> AsyncIterator[NormalizedEvent]:
        """Yield NormalizedEvents from the Odds-API WebSocket stream."""
        if not self._ws:
            raise RuntimeError("Not connected. Call connect() first.")

        self._running = True
        reconnect_attempts = 0

        while self._running:
            try:
                async for raw_msg in self._ws:
                    reconnect_attempts = 0

                    try:
                        parsed = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    msg_type = parsed.get("type")

                    if msg_type == "updated":
                        # Parse ML odds update
                        odds_update = parse_ws_odds_update(parsed)
                        if odds_update is None:
                            continue

                        # --- Odds spike detection (per-bookmaker) ---
                        evt = self._detect_odds_spike(odds_update)
                        if evt:
                            yield evt

                        # --- Consensus score-change hint ---
                        evt = self._detect_score_change_hint(odds_update)
                        if evt:
                            yield evt

                    elif msg_type == "deleted":
                        # Match removed — could indicate finish
                        yield NormalizedEvent(
                            type="match_removed",
                            source="odds_api",
                            confidence="preliminary",
                            timestamp=time.time(),
                            extra={"event_id": parsed.get("id", "")},
                        )

            except websockets.ConnectionClosed:
                if not self._running:
                    break

                reconnect_attempts += 1
                log.warning(
                    "odds_api_ws_disconnected",
                    attempt=reconnect_attempts,
                )

                if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                    yield NormalizedEvent(
                        type="source_failure",
                        source="odds_api",
                        confidence="confirmed",
                        timestamp=time.time(),
                    )
                    break

                await asyncio.sleep(RECONNECT_DELAY)
                try:
                    match_id = self._event_ids[0] if self._event_ids else ""
                    await self.connect(match_id)
                except Exception:
                    log.error(
                        "odds_api_reconnect_failed",
                        attempt=reconnect_attempts,
                    )

    # -----------------------------------------------------------------------
    # Detection helpers
    # -----------------------------------------------------------------------

    def _detect_odds_spike(self, odds_update: dict) -> NormalizedEvent | None:
        """Detect abrupt change in ML home odds from any bookmaker."""
        bookie = odds_update["bookie"]
        current = odds_update["home_odds"]

        last = self._last_home_odds.get(bookie)
        self._last_home_odds[bookie] = current

        if last is not None and last > 0:
            delta = abs(current - last) / last
            if delta >= self._odds_threshold:
                return NormalizedEvent(
                    type="odds_spike",
                    source="odds_api",
                    confidence="preliminary",
                    delta=delta,
                    timestamp=time.time(),
                    extra={
                        "bookie": bookie,
                        "old_odds": last,
                        "new_odds": current,
                        "event_id": odds_update.get("event_id", ""),
                    },
                )

        return None

    def _detect_score_change_hint(
        self, odds_update: dict
    ) -> NormalizedEvent | None:
        """Infer potential score change from sudden consensus odds collapse.

        When multiple bookmakers simultaneously drop home odds by >25%,
        it strongly suggests a goal was scored. This is a HINT only —
        the actual score confirmation comes from GoalserveLiveScoreSource.

        This gives us a ~50-100ms head start over Goalserve's WebSocket
        for triggering ob_freeze.
        """
        current = odds_update["home_odds"]

        # Update running consensus (simple exponential moving average)
        if self._last_consensus_home is None:
            self._last_consensus_home = current
            return None

        # Check for massive drop (>25% = likely goal scored for home)
        # or massive rise (>25% = likely goal scored for away)
        delta = (current - self._last_consensus_home) / self._last_consensus_home

        # Update consensus with EMA (alpha=0.3 for responsiveness)
        self._last_consensus_home = 0.3 * current + 0.7 * self._last_consensus_home

        if abs(delta) >= 0.25:
            return NormalizedEvent(
                type="score_change_hint",
                source="odds_api",
                confidence="preliminary",
                delta=delta,
                timestamp=time.time(),
                extra={
                    "bookie": odds_update["bookie"],
                    "direction": "home_scored" if delta < 0 else "away_scored",
                    "event_id": odds_update.get("event_id", ""),
                },
            )

        return None
