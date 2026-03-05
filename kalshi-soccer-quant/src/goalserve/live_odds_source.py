"""Goalserve Live Odds WebSocket Source — Primary Event Detection (<1s).

Connects to Goalserve Live Odds WebSocket and yields NormalizedEvents
for score changes, period changes, odds spikes, and stoppage entry.

Response format:
{
  "info": {
    "score": "0:0",
    "minute": "45",
    "period": "Paused",
    "ball_pos": "x23;y46",
    "state": "1015"
  },
  "markets": {
    "1777": {  // Fulltime Result
      "participants": {
        "2009353051": {"name": "Home", "value_eu": "1.44", ...},
        ...
      }
    }
  }
}

Reference: phase3.md -> Step 3.1 -> Source 1
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator

import websockets
import websockets.client

from src.common.logging import get_logger
from src.common.types import NormalizedEvent

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class EventSource(ABC):
    """Abstract layer decoupling engine and data sources."""

    @abstractmethod
    async def connect(self, match_id: str) -> None: ...

    @abstractmethod
    async def listen(self) -> AsyncIterator[NormalizedEvent]: ...

    @abstractmethod
    async def disconnect(self) -> None: ...


# ---------------------------------------------------------------------------
# Live Odds WebSocket Source
# ---------------------------------------------------------------------------

# Default odds-change threshold for spike detection (10%)
DEFAULT_ODDS_THRESHOLD = 0.10

# Fulltime Result market ID in Goalserve Live Odds
FULLTIME_RESULT_MARKET_ID = "1777"

# Max reconnect attempts before yielding source_failure
MAX_RECONNECT_ATTEMPTS = 5

# Delay between reconnect attempts (seconds)
RECONNECT_DELAY = 2.0


class GoalserveLiveOddsSource(EventSource):
    """WebSocket PUSH source for Goalserve Live Odds (<1s latency).

    Detects:
      - Score changes (goal_detected / score_rollback)
      - Period changes (period_change)
      - Stoppage-time entry (stoppage_entered)
      - Abrupt odds moves (odds_spike)
    """

    def __init__(
        self,
        api_key: str,
        odds_threshold: float = DEFAULT_ODDS_THRESHOLD,
    ):
        self._api_key = api_key
        self._odds_threshold = odds_threshold
        self._ws: websockets.client.WebSocketClientProtocol | None = None
        self._match_id: str = ""
        self._running: bool = False

        # State tracking for diff detection
        self._last_score: tuple[int, int] | None = None
        self._last_period: str | None = None
        self._last_home_odds: float | None = None
        self._stoppage_entered: dict[str, bool] = {"first": False, "second": False}

    async def connect(self, match_id: str) -> None:
        """Connect to Goalserve Live Odds WebSocket."""
        self._match_id = match_id
        url = f"wss://goalserve.com/liveodds/{self._api_key}/{match_id}"

        self._ws = await websockets.connect(url)

        # Validate connectivity: expect first message within 10s
        first_msg = await asyncio.wait_for(self._ws.recv(), timeout=10)
        parsed = json.loads(first_msg)

        if "info" not in parsed:
            raise ConnectionError("Live Odds: unexpected message format")

        # Initialize state from first message
        info = parsed["info"]
        self._last_score = self._parse_score(info.get("score", "0:0"))
        self._last_period = info.get("period", "")

        log.info(
            "live_odds_ws_connected",
            match_id=match_id,
            score=self._last_score,
            period=self._last_period,
        )

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def listen(self) -> AsyncIterator[NormalizedEvent]:
        """Yield NormalizedEvents from the Live Odds WebSocket stream."""
        if not self._ws:
            raise RuntimeError("Not connected. Call connect() first.")

        self._running = True
        reconnect_attempts = 0

        while self._running:
            try:
                async for raw_msg in self._ws:
                    reconnect_attempts = 0  # Reset on successful message

                    try:
                        parsed = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    info = parsed.get("info")
                    if not info:
                        continue

                    # --- Score change detection ---
                    async for evt in self._detect_score_change(info):
                        yield evt

                    # --- Period change detection ---
                    evt = self._detect_period_change(info)
                    if evt:
                        yield evt

                    # --- Stoppage-time entry detection ---
                    evt = self._detect_stoppage_entry(info)
                    if evt:
                        yield evt

                    # --- Abrupt odds-move detection ---
                    markets = parsed.get("markets", {})
                    evt = self._detect_odds_spike(markets)
                    if evt:
                        yield evt

            except websockets.ConnectionClosed:
                if not self._running:
                    break

                reconnect_attempts += 1
                log.warning(
                    "live_odds_ws_disconnected",
                    attempt=reconnect_attempts,
                    match_id=self._match_id,
                )

                if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                    yield NormalizedEvent(
                        type="source_failure",
                        source="live_odds",
                        confidence="confirmed",
                        timestamp=time.time(),
                    )
                    break

                await asyncio.sleep(RECONNECT_DELAY)
                try:
                    await self.connect(self._match_id)
                except Exception:
                    log.error("live_odds_reconnect_failed", attempt=reconnect_attempts)

    # -----------------------------------------------------------------------
    # Detection helpers
    # -----------------------------------------------------------------------

    async def _detect_score_change(
        self, info: dict
    ) -> AsyncIterator[NormalizedEvent]:
        """Detect score changes from the info block."""
        score_str = info.get("score", "")
        if not score_str:
            return

        new_score = self._parse_score(score_str)
        if self._last_score is not None and new_score != self._last_score:
            # Score decreased -> potential VAR cancellation / rollback
            if (new_score[0] < self._last_score[0] or
                    new_score[1] < self._last_score[1]):
                yield NormalizedEvent(
                    type="score_rollback",
                    source="live_odds",
                    confidence="preliminary",
                    score=new_score,
                    timestamp=time.time(),
                    minute=self._parse_minute(info.get("minute", "")),
                )
            else:
                # Determine scoring team
                team = None
                if new_score[0] > self._last_score[0]:
                    team = "localteam"
                elif new_score[1] > self._last_score[1]:
                    team = "visitorteam"

                yield NormalizedEvent(
                    type="goal_detected",
                    source="live_odds",
                    confidence="preliminary",
                    score=new_score,
                    team=team,
                    timestamp=time.time(),
                    minute=self._parse_minute(info.get("minute", "")),
                )

        self._last_score = new_score

    def _detect_period_change(self, info: dict) -> NormalizedEvent | None:
        """Detect period transitions."""
        new_period = info.get("period", "")
        if not new_period or new_period == self._last_period:
            return None

        evt = NormalizedEvent(
            type="period_change",
            source="live_odds",
            confidence="preliminary",
            period=new_period,
            minute=self._parse_minute(info.get("minute", "")),
            timestamp=time.time(),
        )
        self._last_period = new_period
        return evt

    def _detect_stoppage_entry(self, info: dict) -> NormalizedEvent | None:
        """Detect when stoppage time is entered."""
        minute = self._parse_minute(info.get("minute", ""))
        if minute is None:
            return None

        period = info.get("period", "")

        if period in ("1st Half", "1st") and minute > 45:
            if not self._stoppage_entered["first"]:
                self._stoppage_entered["first"] = True
                return NormalizedEvent(
                    type="stoppage_entered",
                    source="live_odds",
                    confidence="preliminary",
                    period="first",
                    minute=minute,
                    timestamp=time.time(),
                )

        elif period in ("2nd Half", "2nd") and minute > 90:
            if not self._stoppage_entered["second"]:
                self._stoppage_entered["second"] = True
                return NormalizedEvent(
                    type="stoppage_entered",
                    source="live_odds",
                    confidence="preliminary",
                    period="second",
                    minute=minute,
                    timestamp=time.time(),
                )

        return None

    def _detect_odds_spike(self, markets: dict) -> NormalizedEvent | None:
        """Detect abrupt change in Fulltime Result home odds."""
        try:
            ft_market = markets.get(FULLTIME_RESULT_MARKET_ID, {})
            participants = ft_market.get("participants", {})
            for _pid, p in participants.items():
                name = p.get("short_name", p.get("name", ""))
                if "Home" in name:
                    current = float(p["value_eu"])
                    if (self._last_home_odds is not None
                            and self._last_home_odds > 0):
                        delta = abs(current - self._last_home_odds) / self._last_home_odds
                        self._last_home_odds = current
                        if delta >= self._odds_threshold:
                            return NormalizedEvent(
                                type="odds_spike",
                                source="live_odds",
                                confidence="preliminary",
                                delta=delta,
                                timestamp=time.time(),
                            )
                        return None
                    self._last_home_odds = current
        except (KeyError, ValueError, TypeError):
            pass
        return None

    # -----------------------------------------------------------------------
    # Parsing helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_score(score_str: str) -> tuple[int, int]:
        """Parse '1:0' -> (1, 0)."""
        try:
            parts = score_str.split(":")
            return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return (0, 0)

    @staticmethod
    def _parse_minute(minute_str: str) -> float | None:
        """Parse '45' -> 45.0, '90+3' -> 93.0, '' -> None."""
        if not minute_str:
            return None
        try:
            if "+" in minute_str:
                base, extra = minute_str.split("+", 1)
                return float(base) + float(extra)
            return float(minute_str)
        except (ValueError, TypeError):
            return None
