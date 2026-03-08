"""Odds-API Live Odds WebSocket Source — Sub-100ms Event Detection.

Drop-in replacement for GoalserveLiveOddsSource. Connects to the
Odds-API WebSocket and yields NormalizedEvent types:
  - odds_spike (abrupt ML odds move, unclassified)
  - score_change_hint (inferred goal from consensus odds collapse >25%)
  - penalty_hint (15-25% odds shift, consistent with penalty awarded)
  - red_card_hint (8-15% sustained shift, no bounce-back over 3+ updates)
  - var_review_hint (rapid oscillation — multiple direction reversals)
  - penalty_missed_hint (bounce-back after penalty_hint state)

Unlike Goalserve, Odds-API WebSocket does NOT provide:
  - Match score, period, minute, ball position
  - These continue to come from Goalserve LiveScoreSource (REST poller)

Event classification uses odds movement signatures:
  Goal:     >25% collapse, high consensus (>60% bookmakers same direction)
  Penalty:  15-25% drop, moderate consensus, not yet full collapse
  Red card: 8-15% sustained shift, no reversal over 3+ ticks
  VAR:      3+ direction reversals within 10-second window
  Pen miss: Reversal >15% toward pre-penalty levels after penalty_hint

WebSocket endpoint:
  wss://api.odds-api.io/v3/ws?apiKey=KEY&markets=ML,Spread,Totals&sport=football&status=live

Reference: docs/feeds_odds-api.txt → WEBSOCKET API
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
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

# --- Event classification thresholds ---
# Goal: consensus collapse >25%
GOAL_THRESHOLD = 0.25
# Penalty: odds shift 15-25% (penalty conversion ~76%, so not full collapse)
PENALTY_LOW = 0.15
PENALTY_HIGH = 0.25
# Red card: moderate sustained shift 8-15%
RED_CARD_LOW = 0.08
RED_CARD_HIGH = 0.15
# VAR review: 3+ direction reversals within this window
VAR_REVERSAL_COUNT = 3
VAR_WINDOW_SECONDS = 10.0
# Penalty missed: bounce-back >15% toward pre-penalty levels
PENALTY_MISSED_BOUNCE = 0.15
# Sustained shift: must hold for this many bookmaker updates without reversal
SUSTAINED_TICK_COUNT = 3


class OddsApiLiveOddsSource(EventSource):
    """WebSocket PUSH source for Odds-API (<100ms latency).

    Detects and classifies in-game events from odds movement signatures:
      - Goal (score_change_hint): consensus collapse >25%
      - Penalty awarded (penalty_hint): 15-25% drop, moderate consensus
      - Red card (red_card_hint): 8-15% sustained shift, no bounce-back
      - VAR review (var_review_hint): rapid direction reversals
      - Penalty missed (penalty_missed_hint): bounce-back after penalty_hint
      - Generic odds_spike: significant move that doesn't match above patterns

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

        # --- Enhanced tracking for event classification ---

        # Ring buffer of recent odds movements: (timestamp, bookie, delta_pct)
        # Used for consensus analysis and pattern detection
        self._recent_moves: deque[tuple[float, str, float]] = deque(maxlen=100)

        # Per-bookmaker direction history: bookie → deque of (timestamp, delta)
        # Used for VAR oscillation detection
        self._direction_history: dict[str, deque[tuple[float, float]]] = {}

        # Sustained shift tracker: counts consecutive same-direction moves
        # without reversal (for red card detection)
        self._sustained_direction: float = 0.0  # positive = home odds rising
        self._sustained_count: int = 0

        # Current classified state (for penalty_missed detection)
        self._active_hint: str | None = None  # "penalty_hint" when active
        self._pre_hint_consensus: float | None = None  # odds before the hint

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

                        # --- Classify the odds movement ---
                        events = self._classify_odds_movement(odds_update)
                        for evt in events:
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
    # Event classification engine
    # -----------------------------------------------------------------------

    def _classify_odds_movement(
        self, odds_update: dict
    ) -> list[NormalizedEvent]:
        """Classify an odds update into specific event type(s).

        Priority order (highest first):
          1. Penalty missed (bounce-back after penalty_hint)
          2. VAR review (rapid oscillation)
          3. Goal (consensus collapse >25%)
          4. Penalty awarded (15-25% shift, moderate consensus)
          5. Red card (8-15% sustained shift)
          6. Generic odds_spike (>10% but no pattern match)

        Returns list of events (usually 0 or 1, occasionally 2 if
        both a per-bookmaker spike and a consensus event fire).
        """
        bookie = odds_update["bookie"]
        current = odds_update["home_odds"]
        now = time.time()
        events: list[NormalizedEvent] = []

        # Compute per-bookmaker delta
        last = self._last_home_odds.get(bookie)
        self._last_home_odds[bookie] = current

        if last is None or last <= 0:
            # First update for this bookmaker — initialize tracking
            if self._last_consensus_home is None:
                self._last_consensus_home = current
            self._direction_history.setdefault(
                bookie, deque(maxlen=20)
            )
            return events

        delta_pct = (current - last) / last  # signed: positive = odds rising
        abs_delta = abs(delta_pct)

        # Record movement for consensus and pattern analysis
        self._recent_moves.append((now, bookie, delta_pct))
        self._direction_history.setdefault(bookie, deque(maxlen=20))
        self._direction_history[bookie].append((now, delta_pct))

        event_id = odds_update.get("event_id", "")

        # --- Check 1: Penalty missed (bounce-back after penalty_hint) ---
        if self._active_hint == "penalty_hint" and self._pre_hint_consensus:
            consensus_now = self._get_consensus_home()
            if consensus_now and self._pre_hint_consensus > 0:
                recovery = abs(
                    consensus_now - self._pre_hint_consensus
                ) / self._pre_hint_consensus
                # If odds bounced back >15% toward pre-penalty levels
                if recovery < PENALTY_MISSED_BOUNCE:
                    # Still shifted — penalty still active
                    pass
                else:
                    events.append(NormalizedEvent(
                        type="penalty_missed_hint",
                        source="odds_api",
                        confidence="preliminary",
                        delta=delta_pct,
                        timestamp=now,
                        extra={
                            "bookie": bookie,
                            "recovery_pct": recovery,
                            "event_id": event_id,
                        },
                    ))
                    self._active_hint = None
                    self._pre_hint_consensus = None
                    log.info(
                        "penalty_missed_detected",
                        recovery=f"{recovery:.1%}",
                    )

        # --- Check 2: VAR review (rapid oscillation) ---
        if self._detect_var_oscillation(now):
            events.append(NormalizedEvent(
                type="var_review_hint",
                source="odds_api",
                confidence="preliminary",
                delta=delta_pct,
                timestamp=now,
                extra={
                    "bookie": bookie,
                    "reversal_count": self._count_reversals(now),
                    "event_id": event_id,
                },
            ))
            log.info(
                "var_review_detected",
                reversals=self._count_reversals(now),
            )
            # Update consensus EMA and return — don't double-classify
            self._update_consensus(current)
            return events

        # --- Check 3-6: Magnitude-based classification ---
        # Compute consensus delta (across all recent bookmaker moves)
        consensus_delta = self._compute_consensus_delta(now)

        if abs_delta >= self._odds_threshold:
            # Determine event type by magnitude and consensus
            if abs(consensus_delta) >= GOAL_THRESHOLD:
                # Goal: massive consensus collapse
                direction = "home_scored" if consensus_delta < 0 else "away_scored"
                events.append(NormalizedEvent(
                    type="score_change_hint",
                    source="odds_api",
                    confidence="preliminary",
                    delta=consensus_delta,
                    timestamp=now,
                    extra={
                        "bookie": bookie,
                        "direction": direction,
                        "consensus_delta": consensus_delta,
                        "event_id": event_id,
                    },
                ))
                self._active_hint = None  # goal supersedes penalty
                log.info(
                    "goal_hint_detected",
                    direction=direction,
                    consensus_delta=f"{consensus_delta:.1%}",
                )

            elif PENALTY_LOW <= abs(consensus_delta) < PENALTY_HIGH:
                # Penalty awarded: significant but not full collapse
                team = "localteam" if consensus_delta < 0 else "visitorteam"
                events.append(NormalizedEvent(
                    type="penalty_hint",
                    source="odds_api",
                    confidence="preliminary",
                    delta=consensus_delta,
                    timestamp=now,
                    extra={
                        "bookie": bookie,
                        "favored_team": team,
                        "consensus_delta": consensus_delta,
                        "event_id": event_id,
                    },
                ))
                # Track penalty state for missed-penalty detection
                self._active_hint = "penalty_hint"
                self._pre_hint_consensus = self._last_consensus_home
                log.info(
                    "penalty_hint_detected",
                    team=team,
                    consensus_delta=f"{consensus_delta:.1%}",
                )

            elif RED_CARD_LOW <= abs(consensus_delta) < RED_CARD_HIGH:
                # Red card candidate — only if sustained (no bounce-back)
                if self._is_sustained_shift(delta_pct):
                    team = "localteam" if consensus_delta > 0 else "visitorteam"
                    events.append(NormalizedEvent(
                        type="red_card_hint",
                        source="odds_api",
                        confidence="preliminary",
                        delta=consensus_delta,
                        timestamp=now,
                        extra={
                            "bookie": bookie,
                            "team_hint": team,
                            "sustained_ticks": self._sustained_count,
                            "consensus_delta": consensus_delta,
                            "event_id": event_id,
                        },
                    ))
                    log.info(
                        "red_card_hint_detected",
                        team=team,
                        sustained_ticks=self._sustained_count,
                    )
                else:
                    # Not sustained yet — emit generic spike
                    events.append(self._make_spike_event(
                        bookie, last, current, delta_pct, event_id, now,
                    ))
            else:
                # Generic odds_spike (doesn't match known patterns)
                events.append(self._make_spike_event(
                    bookie, last, current, delta_pct, event_id, now,
                ))

        # Update sustained shift tracker
        self._update_sustained_tracker(delta_pct)

        # Update consensus EMA
        self._update_consensus(current)

        return events

    # -----------------------------------------------------------------------
    # Classification helpers
    # -----------------------------------------------------------------------

    def _compute_consensus_delta(self, now: float) -> float:
        """Compute consensus odds movement across all bookmakers in last 2s.

        Returns weighted average delta_pct (negative = home odds dropping).
        """
        window = 2.0  # seconds
        recent = [
            delta for ts, _, delta in self._recent_moves
            if now - ts <= window
        ]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    def _get_consensus_home(self) -> float | None:
        """Get current consensus home odds from all tracked bookmakers."""
        if not self._last_home_odds:
            return None
        vals = [v for v in self._last_home_odds.values() if v > 0]
        return sum(vals) / len(vals) if vals else None

    def _detect_var_oscillation(self, now: float) -> bool:
        """Detect VAR-review pattern: 3+ direction reversals in 10s window.

        A reversal = consecutive moves in opposite directions for the
        same bookmaker (or across bookmakers in the consensus).
        """
        return self._count_reversals(now) >= VAR_REVERSAL_COUNT

    def _count_reversals(self, now: float) -> int:
        """Count direction reversals across all bookmakers in the VAR window."""
        reversals = 0
        for bookie, history in self._direction_history.items():
            # Filter to VAR window
            recent = [
                (ts, d) for ts, d in history
                if now - ts <= VAR_WINDOW_SECONDS
            ]
            if len(recent) < 2:
                continue
            for i in range(1, len(recent)):
                prev_d = recent[i - 1][1]
                curr_d = recent[i][1]
                # Reversal = sign flip (positive→negative or vice versa)
                if prev_d * curr_d < 0 and abs(curr_d) >= 0.03:
                    reversals += 1
        return reversals

    def _is_sustained_shift(self, delta_pct: float) -> bool:
        """Check if the current movement is part of a sustained directional shift.

        A red card causes a moderate, one-directional shift across multiple
        bookmaker updates. Returns True if we've seen SUSTAINED_TICK_COUNT+
        consecutive same-direction moves without reversal.
        """
        return self._sustained_count >= SUSTAINED_TICK_COUNT

    def _update_sustained_tracker(self, delta_pct: float) -> None:
        """Update the sustained-direction tracker with the latest movement."""
        if abs(delta_pct) < 0.02:
            # Negligible move — don't reset
            return

        if self._sustained_direction == 0.0:
            # First significant move
            self._sustained_direction = delta_pct
            self._sustained_count = 1
        elif (delta_pct > 0) == (self._sustained_direction > 0):
            # Same direction — increment
            self._sustained_count += 1
        else:
            # Direction reversed — reset
            self._sustained_direction = delta_pct
            self._sustained_count = 1

    def _update_consensus(self, current_home: float) -> None:
        """Update consensus home odds with EMA (alpha=0.3)."""
        if self._last_consensus_home is None:
            self._last_consensus_home = current_home
        else:
            self._last_consensus_home = (
                0.3 * current_home + 0.7 * self._last_consensus_home
            )

    def _make_spike_event(
        self,
        bookie: str,
        old_odds: float,
        new_odds: float,
        delta_pct: float,
        event_id: str,
        now: float,
    ) -> NormalizedEvent:
        """Create a generic odds_spike event."""
        return NormalizedEvent(
            type="odds_spike",
            source="odds_api",
            confidence="preliminary",
            delta=abs(delta_pct),
            timestamp=now,
            extra={
                "bookie": bookie,
                "old_odds": old_odds,
                "new_odds": new_odds,
                "event_id": event_id,
            },
        )
