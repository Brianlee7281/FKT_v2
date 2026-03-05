"""Step 5.2: Match Scheduler — Automated Engine Lifecycle Management.

Runs 24/7. Scans daily fixtures from Goalserve → schedules MatchEngine spawns
60 minutes before kickoff → monitors health → cleans up finished engines.

Jobs:
  1. Daily 06:00 UTC — scan today's fixtures for target leagues
  2. Every 5 minutes — refresh schedule (time changes, postponements)
  3. Every 10 seconds — engine health monitoring

Reference: docs/blueprint.md → Process 1: SCHEDULER,
           docs/implementation_roadmap.md → Step 5.2
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from src.alerts.main import AlertService, AlertSeverity
from src.common.config import SystemConfig
from src.common.logging import get_logger
from src.engine.match_engine import EngineLifecycle, MatchEngine, ModelParams
from src.goalserve.client import GoalserveClient
from src.goalserve.live_odds_source import GoalserveLiveOddsSource
from src.goalserve.live_score_source import GoalserveLiveScoreSource
from src.trading.risk_manager import RiskManager

log = get_logger(__name__)

# How early (minutes) before kickoff to spawn engines
SPAWN_LEAD_MINUTES = 60

# Health check interval (seconds)
HEALTH_CHECK_INTERVAL = 10

# Schedule refresh interval (seconds)
SCHEDULE_REFRESH_INTERVAL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Match Job — tracks scheduled/active matches
# ---------------------------------------------------------------------------

@dataclass
class MatchJob:
    """Tracks a scheduled or active match."""

    match_id: str
    league_id: str
    kickoff_time: datetime
    home_team: str = ""
    away_team: str = ""
    status: str = "SCHEDULED"  # SCHEDULED, SPAWNED, LIVE, FINISHED, SKIPPED, FAILED
    engine: MatchEngine | None = None
    engine_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Match Scheduler
# ---------------------------------------------------------------------------

class MatchScheduler:
    """Runs 24/7. Scans daily fixtures → spawns MatchEngine before kickoff.

    Attributes:
        config: System configuration.
        goalserve: Goalserve REST client for fixtures.
        risk_manager: Shared risk manager across all engines (Layer 3).
        jobs: All tracked match jobs by match_id.
    """

    def __init__(
        self,
        config: SystemConfig,
        *,
        goalserve_client: GoalserveClient | None = None,
        risk_manager: RiskManager | None = None,
        redis_client=None,
        alerter: AlertService | None = None,
    ):
        self.config = config

        # Goalserve client (injected for testability)
        self.goalserve = goalserve_client or GoalserveClient(
            api_key=config.goalserve_api_key,
            base_url=config.goalserve_base_url,
        )

        # Shared risk manager (Layer 3 = total portfolio cap)
        self.risk_manager = risk_manager or RiskManager(
            f_order_cap=config.f_order_cap,
            f_match_cap=config.f_match_cap,
            f_total_cap=config.f_total_cap,
        )

        # Alert service (injected for testability)
        self.alerter = alerter or AlertService(config)

        # Infrastructure
        self._redis = redis_client

        # Match jobs: match_id -> MatchJob
        self.jobs: dict[str, MatchJob] = {}

        # Shutdown coordination
        self._shutdown_event = asyncio.Event()

        # Spawn timers: match_id -> asyncio.Task
        self._spawn_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """System entry point — runs until shutdown.

        Starts three concurrent loops:
          1. Daily fixture scan (06:00 UTC + immediate on startup)
          2. Schedule refresh every 5 minutes
          3. Engine health monitor every 10 seconds
        """
        log.info("scheduler_starting")

        # Immediate scan at startup
        await self.scan_today_matches()

        # Run concurrent loops
        await asyncio.gather(
            self._daily_scan_loop(),
            self._refresh_loop(),
            self._monitor_loop(),
        )

    async def stop(self) -> None:
        """Graceful shutdown — stop all engines and cancel timers."""
        log.info("scheduler_stopping")
        self._shutdown_event.set()

        # Cancel all spawn timers
        for task in self._spawn_tasks.values():
            task.cancel()
        self._spawn_tasks.clear()

        # Shutdown all active engines
        for job in self.jobs.values():
            if job.engine is not None:
                job.engine.shutdown()

        # Wait for engine tasks to complete (with timeout)
        tasks = [j.engine_task for j in self.jobs.values()
                 if j.engine_task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        log.info("scheduler_stopped", engines_shut_down=len(tasks))

    # ------------------------------------------------------------------
    # Loop 1: Daily Fixture Scan
    # ------------------------------------------------------------------

    async def _daily_scan_loop(self) -> None:
        """Run daily scan at 06:00 UTC."""
        while not self._shutdown_event.is_set():
            now = datetime.now(timezone.utc)

            # Next 06:00 UTC
            next_scan = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now >= next_scan:
                next_scan += timedelta(days=1)

            wait_seconds = (next_scan - now).total_seconds()
            log.info("next_daily_scan", wait_seconds=int(wait_seconds))

            # Wait (interruptible by shutdown)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=wait_seconds,
                )
                break  # shutdown was signaled
            except asyncio.TimeoutError:
                pass  # time to scan

            await self.scan_today_matches()

    # ------------------------------------------------------------------
    # Loop 2: Schedule Refresh
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Refresh schedule every 5 minutes (catch postponements, time changes)."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=SCHEDULE_REFRESH_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

            await self._check_spawn_readiness()

    # ------------------------------------------------------------------
    # Loop 3: Engine Health Monitor
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Monitor engine health every 10 seconds."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=HEALTH_CHECK_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

            await self.monitor_engines()

    # ------------------------------------------------------------------
    # Fixture Scanning
    # ------------------------------------------------------------------

    async def scan_today_matches(self) -> list[MatchJob]:
        """Scan today's fixtures from Goalserve for all target leagues.

        Returns:
            List of newly discovered MatchJobs.
        """
        today = datetime.now(timezone.utc)
        today_str = today.strftime("%d.%m.%Y")
        new_jobs: list[MatchJob] = []

        for league_id in self.config.target_leagues:
            try:
                fixtures = await self.goalserve.get_fixtures(league_id, date=today_str)
            except Exception as e:
                log.error(
                    "fixture_scan_error",
                    league_id=league_id,
                    error=str(e),
                )
                continue

            for match in fixtures:
                match_id = str(match.get("id", ""))
                if not match_id or match_id in self.jobs:
                    continue

                kickoff = _parse_kickoff_time(match, today)
                if kickoff is None:
                    continue

                job = MatchJob(
                    match_id=match_id,
                    league_id=league_id,
                    kickoff_time=kickoff,
                    home_team=_extract_team_name(match, "localteam"),
                    away_team=_extract_team_name(match, "visitorteam"),
                    status="SCHEDULED",
                )
                self.jobs[match_id] = job
                new_jobs.append(job)

                # Schedule spawn if within lead time
                self._schedule_spawn(job)

        log.info(
            "fixture_scan_complete",
            date=today_str,
            new_matches=len(new_jobs),
            total_jobs=len(self.jobs),
        )

        return new_jobs

    def _schedule_spawn(self, job: MatchJob) -> None:
        """Schedule engine spawn SPAWN_LEAD_MINUTES before kickoff."""
        now = datetime.now(timezone.utc)
        spawn_time = job.kickoff_time - timedelta(minutes=SPAWN_LEAD_MINUTES)

        if now >= spawn_time and job.status == "SCHEDULED":
            # Already past spawn time — spawn immediately
            task = asyncio.create_task(self.spawn_engine(job.match_id))
            self._spawn_tasks[job.match_id] = task
        elif spawn_time > now:
            # Schedule future spawn
            delay = (spawn_time - now).total_seconds()

            async def _delayed_spawn(mid: str, d: float) -> None:
                try:
                    await asyncio.sleep(d)
                    await self.spawn_engine(mid)
                except asyncio.CancelledError:
                    pass

            task = asyncio.create_task(_delayed_spawn(job.match_id, delay))
            self._spawn_tasks[job.match_id] = task

            log.info(
                "spawn_scheduled",
                match_id=job.match_id,
                spawn_in_minutes=int(delay / 60),
                kickoff=job.kickoff_time.isoformat(),
            )

    async def _check_spawn_readiness(self) -> None:
        """Check if any SCHEDULED jobs should be spawned now."""
        now = datetime.now(timezone.utc)

        for job in list(self.jobs.values()):
            if job.status != "SCHEDULED":
                continue

            spawn_time = job.kickoff_time - timedelta(minutes=SPAWN_LEAD_MINUTES)

            # If spawn time has passed and no spawn task exists
            if now >= spawn_time and job.match_id not in self._spawn_tasks:
                self._schedule_spawn(job)

    # ------------------------------------------------------------------
    # Engine Spawning
    # ------------------------------------------------------------------

    async def spawn_engine(self, match_id: str) -> MatchEngine | None:
        """Create MatchEngine instance, run Phase 2 prematch, then go live.

        Args:
            match_id: Match to spawn engine for.

        Returns:
            The spawned MatchEngine, or None if spawn failed/skipped.
        """
        job = self.jobs.get(match_id)
        if job is None:
            log.warning("spawn_unknown_match", match_id=match_id)
            return None

        if job.status not in ("SCHEDULED", "FAILED"):
            return job.engine  # already spawned or finished

        try:
            # Create live data sources
            live_odds = GoalserveLiveOddsSource(
                api_key=self.config.goalserve_api_key,
            )
            live_score = GoalserveLiveScoreSource(
                api_key=self.config.goalserve_api_key,
                match_id=match_id,
                poll_interval=self.config.live_score_poll_interval,
            )

            engine = MatchEngine(
                match_id=match_id,
                config=self.config,
                risk_manager=self.risk_manager,
                redis_client=self._redis,
                live_odds_source=live_odds,
                live_score_source=live_score,
            )

            job.engine = engine
            job.status = "SPAWNED"

            # Run Phase 2 prematch
            sanity = await engine.run_prematch()

            if sanity.verdict == "SKIP":
                job.status = "SKIPPED"
                log.info(
                    "engine_skipped",
                    match_id=match_id,
                    warning=sanity.warning,
                )
                return None

            if sanity.verdict == "HOLD":
                log.warning(
                    "engine_hold_proceeding_with_caution",
                    match_id=match_id,
                    warning=sanity.warning,
                )
                # Blueprint: HOLD = alert but still proceed (manual review needed)
                await self.alerter.send(
                    AlertSeverity.WARNING,
                    "Match on HOLD — manual review needed",
                    body=sanity.warning or "",
                    match_id=match_id,
                )

            # Launch live trading as background task
            job.engine_task = asyncio.create_task(
                self._run_engine_live(match_id)
            )
            job.status = "LIVE"

            log.info(
                "engine_spawned",
                match_id=match_id,
                home=job.home_team,
                away=job.away_team,
                kickoff=job.kickoff_time.isoformat(),
            )

            return engine

        except Exception as e:
            job.status = "FAILED"
            log.error(
                "engine_spawn_failed",
                match_id=match_id,
                error=str(e),
            )
            await self.alerter.send(
                AlertSeverity.CRITICAL,
                "Engine spawn failed",
                body=f"{match_id}: {e}",
                match_id=match_id,
            )
            return None

    async def _run_engine_live(self, match_id: str) -> None:
        """Wrapper around engine.run_live() that updates job status on completion."""
        job = self.jobs.get(match_id)
        if job is None or job.engine is None:
            return

        try:
            await job.engine.run_live()
        except Exception as e:
            log.error("engine_live_error", match_id=match_id, error=str(e))
        finally:
            job.status = "FINISHED"

    # ------------------------------------------------------------------
    # Health Monitoring
    # ------------------------------------------------------------------

    async def monitor_engines(self) -> list[str]:
        """Monitor health of active engines. Clean up finished ones.

        Returns:
            List of match_ids that were cleaned up.
        """
        cleaned: list[str] = []

        for match_id, job in list(self.jobs.items()):
            if job.engine is None:
                continue

            # Check if finished
            if job.engine.is_finished() or job.status == "FINISHED":
                await self._handle_finished(match_id, job)
                cleaned.append(match_id)
                continue

            # Health check
            if not job.engine.is_healthy():
                log.warning(
                    "engine_unhealthy",
                    match_id=match_id,
                    lifecycle=job.engine.lifecycle.value,
                    last_tick=job.engine._last_tick_time,
                )
                await self.alerter.send(
                    AlertSeverity.CRITICAL,
                    "Engine unhealthy",
                    body=f"Lifecycle: {job.engine.lifecycle.value}, last_tick: {job.engine._last_tick_time}",
                    match_id=match_id,
                )

        return cleaned

    async def _handle_finished(self, match_id: str, job: MatchJob) -> None:
        """Clean up a finished engine."""
        job.status = "FINISHED"

        # Cancel spawn task if exists
        if match_id in self._spawn_tasks:
            self._spawn_tasks[match_id].cancel()
            del self._spawn_tasks[match_id]

        # Shut down engine to release resources
        if job.engine is not None:
            job.engine.shutdown()

        log.info(
            "engine_finished_cleanup",
            match_id=match_id,
            trades=len(job.engine.trade_log) if job.engine else 0,
            bankroll=job.engine.bankroll if job.engine else 0,
        )

        # Release engine reference to prevent repeated cleanup
        # (monitor_engines skips jobs where engine is None)
        job.engine = None
        job.engine_task = None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active_engines(self) -> dict[str, MatchEngine]:
        """Return currently active (LIVE) engines."""
        return {
            mid: job.engine
            for mid, job in self.jobs.items()
            if job.engine is not None and job.status == "LIVE"
        }

    def get_job_summary(self) -> list[dict]:
        """Return summary of all tracked jobs."""
        return [
            {
                "match_id": job.match_id,
                "league_id": job.league_id,
                "home": job.home_team,
                "away": job.away_team,
                "kickoff": job.kickoff_time.isoformat(),
                "status": job.status,
                "lifecycle": (
                    job.engine.lifecycle.value if job.engine else None
                ),
            }
            for job in self.jobs.values()
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_kickoff_time(match: dict, today: datetime) -> datetime | None:
    """Parse kickoff time from Goalserve fixture dict.

    Goalserve provides time in "HH:MM" format. Combines with today's date
    and assumes UTC.
    """
    time_str = match.get("time", match.get("formatted_date", ""))
    if not time_str:
        return None

    try:
        # Try "HH:MM" format
        parts = time_str.strip().split(":")
        if len(parts) >= 2:
            hour = int(parts[0])
            minute = int(parts[1])
            return today.replace(
                hour=hour, minute=minute, second=0, microsecond=0,
                tzinfo=timezone.utc,
            )
    except (ValueError, IndexError):
        pass

    return None


def _extract_team_name(match: dict, key: str) -> str:
    """Extract team name from fixture dict (handles dict or string)."""
    team = match.get(key, "")
    if isinstance(team, dict):
        return team.get("name", team.get("@name", ""))
    return str(team)
