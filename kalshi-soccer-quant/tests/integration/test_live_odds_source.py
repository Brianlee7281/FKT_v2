"""Integration tests for Goalserve Live Odds WebSocket Source.

Uses mock WebSocket to verify event detection logic without
requiring a live Goalserve connection.

Reference: implementation_roadmap.md -> Step 3.1 tests
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.goalserve.live_odds_source import GoalserveLiveOddsSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws_message(
    score: str = "0:0",
    minute: str = "30",
    period: str = "1st Half",
    home_odds: str = "2.10",
    draw_odds: str = "3.40",
    away_odds: str = "3.50",
) -> str:
    """Build a mock Live Odds WebSocket message."""
    return json.dumps({
        "info": {
            "score": score,
            "minute": minute,
            "period": period,
            "ball_pos": "x50;y50",
            "state": "1000",
        },
        "markets": {
            "1777": {
                "participants": {
                    "1": {"name": "Home", "short_name": "Home", "value_eu": home_odds},
                    "2": {"name": "Draw", "short_name": "Draw", "value_eu": draw_odds},
                    "3": {"name": "Away", "short_name": "Away", "value_eu": away_odds},
                },
            },
        },
    })


async def _collect_events(source, messages, init_score="0:0", init_period="1st Half"):
    """Feed messages through detection logic and collect events."""
    # Initialize state
    source._last_score = source._parse_score(init_score)
    source._last_period = init_period
    source._running = True

    events = []
    for msg in messages:
        parsed = json.loads(msg)
        info = parsed.get("info", {})

        async for evt in source._detect_score_change(info):
            events.append(evt)

        evt = source._detect_period_change(info)
        if evt:
            events.append(evt)

        evt = source._detect_stoppage_entry(info)
        if evt:
            events.append(evt)

        markets = parsed.get("markets", {})
        evt = source._detect_odds_spike(markets)
        if evt:
            events.append(evt)

    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebSocketConnects:
    """test_websocket_connects"""

    @pytest.mark.asyncio
    async def test_websocket_connects(self):
        """Verify connect() initializes state from first message."""
        source = GoalserveLiveOddsSource(api_key="test_key")

        first_msg = _make_ws_message(score="0:0", period="1st Half", minute="1")

        with patch("src.goalserve.live_odds_source.websockets") as mock_ws:
            mock_conn = AsyncMock()
            mock_conn.recv = AsyncMock(return_value=first_msg)
            mock_ws.connect = AsyncMock(return_value=mock_conn)

            await source.connect("12345")

            assert source._last_score == (0, 0)
            assert source._last_period == "1st Half"
            assert source._match_id == "12345"


class TestScoreChangeDetection:
    """test_score_change_yields_event"""

    @pytest.mark.asyncio
    async def test_score_change_yields_goal_detected(self):
        """Score increase should yield goal_detected event."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(score="1:0", minute="25"),
        ]

        events = await _collect_events(source, messages, init_score="0:0")

        assert len(events) >= 1
        goal_events = [e for e in events if e.type == "goal_detected"]
        assert len(goal_events) == 1
        assert goal_events[0].score == (1, 0)
        assert goal_events[0].team == "localteam"
        assert goal_events[0].source == "live_odds"
        assert goal_events[0].confidence == "preliminary"

    @pytest.mark.asyncio
    async def test_away_goal_detected(self):
        """Away team scoring should be detected."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(score="0:1", minute="40"),
        ]

        events = await _collect_events(source, messages, init_score="0:0")

        goal_events = [e for e in events if e.type == "goal_detected"]
        assert len(goal_events) == 1
        assert goal_events[0].team == "visitorteam"


class TestOddsSpike:
    """test_odds_spike_yields_event"""

    @pytest.mark.asyncio
    async def test_odds_spike_yields_event(self):
        """Large odds change should yield odds_spike event."""
        source = GoalserveLiveOddsSource(api_key="test_key", odds_threshold=0.10)

        # First message establishes baseline odds
        msg1 = _make_ws_message(home_odds="2.00")
        # Second message has 25% jump (well above 10% threshold)
        msg2 = _make_ws_message(home_odds="2.50")

        events = await _collect_events(source, [msg1, msg2])

        spike_events = [e for e in events if e.type == "odds_spike"]
        assert len(spike_events) == 1
        assert spike_events[0].delta >= 0.10

    @pytest.mark.asyncio
    async def test_small_odds_change_no_spike(self):
        """Small odds change should not yield spike."""
        source = GoalserveLiveOddsSource(api_key="test_key", odds_threshold=0.10)

        msg1 = _make_ws_message(home_odds="2.00")
        msg2 = _make_ws_message(home_odds="2.05")  # 2.5% change

        events = await _collect_events(source, [msg1, msg2])

        spike_events = [e for e in events if e.type == "odds_spike"]
        assert len(spike_events) == 0


class TestPeriodChange:
    """test_period_change_yields_event"""

    @pytest.mark.asyncio
    async def test_period_change_yields_event(self):
        """Period transition should yield period_change event."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(period="Paused", minute="45"),
        ]

        events = await _collect_events(source, messages, init_period="1st Half")

        period_events = [e for e in events if e.type == "period_change"]
        assert len(period_events) == 1
        assert period_events[0].period == "Paused"

    @pytest.mark.asyncio
    async def test_stoppage_entry_first_half(self):
        """Minute > 45 in 1st Half should yield stoppage_entered."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(period="1st Half", minute="46"),
        ]

        events = await _collect_events(source, messages, init_period="1st Half")

        stoppage_events = [e for e in events if e.type == "stoppage_entered"]
        assert len(stoppage_events) == 1
        assert stoppage_events[0].period == "first"

    @pytest.mark.asyncio
    async def test_stoppage_entry_only_once(self):
        """Stoppage entry should only fire once per half."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(period="1st Half", minute="46"),
            _make_ws_message(period="1st Half", minute="47"),
        ]

        events = await _collect_events(source, messages, init_period="1st Half")

        stoppage_events = [e for e in events if e.type == "stoppage_entered"]
        assert len(stoppage_events) == 1


class TestReconnectOnDisconnect:
    """test_reconnect_on_disconnect"""

    @pytest.mark.asyncio
    async def test_max_reconnect_yields_source_failure(self):
        """After MAX_RECONNECT_ATTEMPTS, should yield source_failure."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        source._match_id = "12345"
        source._running = True

        # Mock a WebSocket that always disconnects
        import websockets

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = MagicMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )
        source._ws = mock_ws

        # Mock connect to always fail
        source.connect = AsyncMock(side_effect=ConnectionError("failed"))

        events = []
        async for evt in source.listen():
            events.append(evt)
            if evt.type == "source_failure":
                break

        failure_events = [e for e in events if e.type == "source_failure"]
        assert len(failure_events) == 1


class TestScoreRollback:
    """test_score_rollback_yields_event"""

    @pytest.mark.asyncio
    async def test_score_rollback_yields_event(self):
        """Score decrease (VAR cancellation) should yield score_rollback."""
        source = GoalserveLiveOddsSource(api_key="test_key")
        messages = [
            _make_ws_message(score="0:0", minute="35"),
        ]

        # Start with score 1:0 (goal was scored, then VAR cancels)
        events = await _collect_events(source, messages, init_score="1:0")

        rollback_events = [e for e in events if e.type == "score_rollback"]
        assert len(rollback_events) == 1
        assert rollback_events[0].score == (0, 0)
