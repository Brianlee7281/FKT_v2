"""Tests for Step 5.2: Match Scheduler.

Verifies fixture scanning, engine spawning, cleanup, and concurrency.

Reference: implementation_roadmap.md -> Step 5.2 tests
├── test_scan_finds_matches()
├── test_engine_spawned_on_time()
├── test_finished_engine_removed()
└── test_concurrent_engines()
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.common.config import SystemConfig
from src.common.types import SanityResult
from src.engine.match_engine import EngineLifecycle, MatchEngine, ModelParams
from src.engine.state_machine import transition_to_first_half, transition_to_finished
from src.scheduler.main import (
    MatchJob,
    MatchScheduler,
    _extract_team_name,
    _parse_kickoff_time,
)
from src.trading.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Fake Goalserve Client
# ---------------------------------------------------------------------------

class FakeGoalserveClient:
    """Returns pre-loaded fixtures without hitting the real API."""

    def __init__(self, fixtures_by_league: dict[str, list[dict]] | None = None):
        self._fixtures = fixtures_by_league or {}
        self.get_fixtures_calls: list[tuple[str, str | None]] = []

    async def get_fixtures(self, league_id: str, date: str | None = None) -> list[dict]:
        self.get_fixtures_calls.append((league_id, date))
        return self._fixtures.get(league_id, [])


class FakeRedis:
    """In-memory Redis substitute."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25"],
        target_leagues=["1204", "1399"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,
        goalserve_api_key="test_key",
    )


def _make_fixture(match_id: str, time_str: str,
                  home: str = "Home FC", away: str = "Away FC") -> dict:
    """Helper to create a Goalserve-style fixture dict."""
    return {
        "id": match_id,
        "time": time_str,
        "localteam": {"name": home},
        "visitorteam": {"name": away},
    }


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# test_scan_finds_matches
# ---------------------------------------------------------------------------

class TestScanFindsMatches:
    """Scheduler discovers matches from Goalserve fixtures."""

    @pytest.mark.asyncio
    async def test_scan_discovers_matches(self, config, now_utc):
        """scan_today_matches finds fixtures from all target leagues."""
        kickoff_1h = (now_utc + timedelta(hours=1)).strftime("%H:%M")
        kickoff_3h = (now_utc + timedelta(hours=3)).strftime("%H:%M")

        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff_1h, "Liverpool", "Arsenal")],
            "1399": [_make_fixture("m2", kickoff_3h, "Barcelona", "Real Madrid")],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        jobs = await scheduler.scan_today_matches()

        assert len(jobs) == 2
        assert "m1" in scheduler.jobs
        assert "m2" in scheduler.jobs
        assert scheduler.jobs["m1"].home_team == "Liverpool"
        assert scheduler.jobs["m2"].away_team == "Real Madrid"

    @pytest.mark.asyncio
    async def test_scan_deduplicates(self, config, now_utc):
        """Scanning twice doesn't duplicate jobs."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        jobs_1 = await scheduler.scan_today_matches()
        jobs_2 = await scheduler.scan_today_matches()

        assert len(jobs_1) == 1
        assert len(jobs_2) == 0
        assert len(scheduler.jobs) == 1

    @pytest.mark.asyncio
    async def test_scan_handles_api_error(self, config):
        """API errors for one league don't block other leagues."""
        class FailingClient:
            async def get_fixtures(self, league_id, date=None):
                if league_id == "1204":
                    raise ConnectionError("API down")
                return [_make_fixture("m2", "15:00")]

        scheduler = MatchScheduler(config, goalserve_client=FailingClient())
        jobs = await scheduler.scan_today_matches()

        assert len(jobs) == 1
        assert "m2" in scheduler.jobs

    @pytest.mark.asyncio
    async def test_scan_skips_no_time(self, config):
        """Matches without time are skipped."""
        goalserve = FakeGoalserveClient({
            "1204": [{"id": "m1"}],  # no "time" field
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        jobs = await scheduler.scan_today_matches()

        assert len(jobs) == 0

    @pytest.mark.asyncio
    async def test_scan_calls_all_leagues(self, config):
        """Scan queries all target leagues."""
        goalserve = FakeGoalserveClient({"1204": [], "1399": []})
        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        called_leagues = [call[0] for call in goalserve.get_fixtures_calls]
        assert "1204" in called_leagues
        assert "1399" in called_leagues


# ---------------------------------------------------------------------------
# test_engine_spawned_on_time
# ---------------------------------------------------------------------------

class TestEngineSpawnedOnTime:
    """Engines are spawned at the right time."""

    @pytest.mark.asyncio
    async def test_spawn_creates_engine(self, config, now_utc):
        """spawn_engine creates a MatchEngine and runs prematch."""
        kickoff = now_utc + timedelta(hours=2)
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff.strftime("%H:%M"))],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        engine = await scheduler.spawn_engine("m1")

        assert engine is not None
        assert scheduler.jobs["m1"].status == "LIVE"
        assert scheduler.jobs["m1"].engine is engine
        assert engine.lifecycle in (
            EngineLifecycle.PREMATCH_READY,
            EngineLifecycle.LIVE,
        )

    @pytest.mark.asyncio
    async def test_spawn_unknown_match_returns_none(self, config):
        """Spawning unknown match_id returns None."""
        scheduler = MatchScheduler(config, goalserve_client=FakeGoalserveClient())
        result = await scheduler.spawn_engine("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_spawn_shares_risk_manager(self, config, now_utc):
        """All engines share the same RiskManager for Layer 3."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [
                _make_fixture("m1", kickoff),
                _make_fixture("m2", kickoff),
            ],
        })

        shared_rm = RiskManager(
            f_order_cap=0.03,
            f_match_cap=0.05,
            f_total_cap=0.20,
        )
        scheduler = MatchScheduler(
            config, goalserve_client=goalserve, risk_manager=shared_rm,
        )
        await scheduler.scan_today_matches()

        engine1 = await scheduler.spawn_engine("m1")
        engine2 = await scheduler.spawn_engine("m2")

        assert engine1 is not None
        assert engine2 is not None
        assert engine1.risk_manager is shared_rm
        assert engine2.risk_manager is shared_rm

    @pytest.mark.asyncio
    async def test_spawn_idempotent(self, config, now_utc):
        """Spawning same match twice returns existing engine."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        engine1 = await scheduler.spawn_engine("m1")
        engine2 = await scheduler.spawn_engine("m1")

        assert engine1 is engine2


# ---------------------------------------------------------------------------
# test_finished_engine_removed
# ---------------------------------------------------------------------------

class TestFinishedEngineRemoved:
    """Finished engines are cleaned up by the monitor."""

    @pytest.mark.asyncio
    async def test_finished_engine_cleaned_up(self, config, now_utc):
        """monitor_engines detects FINISHED and cleans up."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()
        await scheduler.spawn_engine("m1")

        job = scheduler.jobs["m1"]
        # Simulate engine finishing
        job.engine.lifecycle = EngineLifecycle.FINISHED

        cleaned = await scheduler.monitor_engines()
        assert "m1" in cleaned
        assert job.status == "FINISHED"

    @pytest.mark.asyncio
    async def test_unhealthy_engine_logged(self, config, now_utc):
        """monitor_engines detects unhealthy engine (no cleanup)."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()
        await scheduler.spawn_engine("m1")

        job = scheduler.jobs["m1"]
        # Simulate unhealthy: LIVE but stale tick
        job.engine.lifecycle = EngineLifecycle.LIVE
        job.engine._last_tick_time = 0  # very stale

        cleaned = await scheduler.monitor_engines()
        # Not cleaned up (still running), but logged as unhealthy
        assert "m1" not in cleaned
        assert job.status == "LIVE"


# ---------------------------------------------------------------------------
# test_concurrent_engines
# ---------------------------------------------------------------------------

class TestConcurrentEngines:
    """Multiple engines can run concurrently."""

    @pytest.mark.asyncio
    async def test_multiple_engines_active(self, config, now_utc):
        """Multiple engines can be spawned and tracked simultaneously."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [
                _make_fixture("m1", kickoff, "Liverpool", "Arsenal"),
                _make_fixture("m2", kickoff, "Chelsea", "Spurs"),
                _make_fixture("m3", kickoff, "City", "United"),
            ],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        e1 = await scheduler.spawn_engine("m1")
        e2 = await scheduler.spawn_engine("m2")
        e3 = await scheduler.spawn_engine("m3")

        active = scheduler.get_active_engines()
        assert len(active) == 3
        assert all(e is not None for e in [e1, e2, e3])

    @pytest.mark.asyncio
    async def test_get_job_summary(self, config, now_utc):
        """Job summary includes all tracked matches."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [
                _make_fixture("m1", kickoff, "Liverpool", "Arsenal"),
                _make_fixture("m2", kickoff, "Chelsea", "Spurs"),
            ],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()
        await scheduler.spawn_engine("m1")

        summary = scheduler.get_job_summary()
        assert len(summary) == 2

        m1_summary = next(s for s in summary if s["match_id"] == "m1")
        assert m1_summary["status"] == "LIVE"
        assert m1_summary["home"] == "Liverpool"

        m2_summary = next(s for s in summary if s["match_id"] == "m2")
        assert m2_summary["status"] == "SCHEDULED"

    @pytest.mark.asyncio
    async def test_stop_shuts_down_all(self, config, now_utc):
        """stop() shuts down all active engines."""
        kickoff = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()
        await scheduler.spawn_engine("m1")

        await scheduler.stop()

        assert scheduler._shutdown_event.is_set()
        engine = scheduler.jobs["m1"].engine
        assert engine.lifecycle in (
            EngineLifecycle.SHUTDOWN,
            EngineLifecycle.FINISHED,
        )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    """Test helper functions."""

    def test_parse_kickoff_time_valid(self):
        today = datetime(2026, 3, 5, tzinfo=timezone.utc)
        match = {"time": "15:30"}
        result = _parse_kickoff_time(match, today)
        assert result is not None
        assert result.hour == 15
        assert result.minute == 30

    def test_parse_kickoff_time_missing(self):
        today = datetime(2026, 3, 5, tzinfo=timezone.utc)
        result = _parse_kickoff_time({}, today)
        assert result is None

    def test_parse_kickoff_time_invalid(self):
        today = datetime(2026, 3, 5, tzinfo=timezone.utc)
        result = _parse_kickoff_time({"time": "TBD"}, today)
        assert result is None

    def test_extract_team_name_dict(self):
        assert _extract_team_name({"localteam": {"name": "Liverpool"}}, "localteam") == "Liverpool"

    def test_extract_team_name_string(self):
        assert _extract_team_name({"localteam": "Liverpool"}, "localteam") == "Liverpool"

    def test_extract_team_name_missing(self):
        assert _extract_team_name({}, "localteam") == ""


# ---------------------------------------------------------------------------
# Schedule spawn timing tests
# ---------------------------------------------------------------------------

class TestSpawnScheduling:
    """Verify spawn timing logic."""

    @pytest.mark.asyncio
    async def test_past_spawn_time_immediate(self, config, now_utc):
        """Match with kickoff < 60min away triggers immediate spawn."""
        # Kickoff in 30 minutes (spawn time was 30 min ago)
        kickoff = (now_utc + timedelta(minutes=30)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        # Should have created a spawn task
        assert "m1" in scheduler._spawn_tasks

        # Wait briefly for the immediate spawn
        await asyncio.sleep(0.1)

        # Engine should be spawned
        job = scheduler.jobs["m1"]
        assert job.status in ("LIVE", "SPAWNED")

    @pytest.mark.asyncio
    async def test_future_spawn_scheduled(self, config, now_utc):
        """Match with kickoff > 60min away schedules a delayed spawn."""
        # Kickoff in 3 hours (spawn in 2 hours)
        kickoff = (now_utc + timedelta(hours=3)).strftime("%H:%M")
        goalserve = FakeGoalserveClient({
            "1204": [_make_fixture("m1", kickoff)],
        })

        scheduler = MatchScheduler(config, goalserve_client=goalserve)
        await scheduler.scan_today_matches()

        # Should have scheduled (not yet spawned)
        assert "m1" in scheduler._spawn_tasks
        assert scheduler.jobs["m1"].status == "SCHEDULED"
