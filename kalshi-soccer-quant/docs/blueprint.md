# Implementation Blueprint — 24/7 Automated System

## Overview

This is the implementation blueprint for a Kalshi soccer quant auto-trading system that runs 24/7 from the moment it is turned on.

Without human intervention, it will:
- scan daily match schedules,
- automatically run Pre-Match 60 minutes before kickoff,
- trade in real time during matches,
- auto-settle and run post-match analytics,
- periodically retrain the model,
- and self-recover from failures.

### Design Principles

1. **Lights-Out Operations:** No human intervention required under normal conditions.
2. **Graceful Degradation:** Safely scale down operations under partial failures.
3. **Self-Healing:** Automatically recover from transient failures.
4. **Observable:** Make all states visible via dashboards + alerts.
5. **Auditable:** Permanently record all decisions and trades.

---

## System Architecture

### Process Topology — 5 Always-On Processes + 2 Periodic Processes

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Single Server (VPS)                         │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process 1: SCHEDULER (always running)                      │   │
│  │  • Scan daily fixtures → create per-match jobs              │   │
│  │  • 60 min before kickoff: spawn MatchEngine instance        │   │
│  │  • After kickoff: monitor MatchEngine state                 │   │
│  │  • After match end: trigger settlement + post-analysis      │   │
│  └─────────────────────────┬───────────────────────────────────┘   │
│                            │ spawn/monitor                         │
│  ┌─────────────────────────▼───────────────────────────────────┐   │
│  │  Process 2~N: MATCH_ENGINE (1 per match, dynamic spawn)     │   │
│  │  • Phase 2 (Pre-Match) → Phase 3 (Live) → Phase 4 (Exec)    │   │
│  │  • 3 asyncio coroutines: tick_loop + live_odds + live_score │   │
│  │  • Auto-terminate at match end                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+1: DASHBOARD_SERVER (always running)             │   │
│  │  • FastAPI + WebSocket → serve React frontend               │   │
│  │  • Subscribe to Redis real-time data → push to browser      │   │
│  │  • Query analytics data from PostgreSQL                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+2: ALERT_SERVICE (always running)                │   │
│  │  • Subscribe to Redis events → send Slack/Telegram alerts   │   │
│  │  • System health monitoring (process liveness, RAM, CPU)    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+3: DATA_COLLECTOR (always running)               │   │
│  │  • Collect Goalserve Fixtures/Results + Live Game Stats     │   │
│  │  • Load historical match data into DB (for Phase 1)         │   │
│  │  • Collect and archive pregame odds                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────── Periodic Processes ──────────────────────────┐   │
│  │  CRON 1: RECALIBRATION (on trigger or season start)         │   │
│  │  • Re-run full Phase 1 (Step 1.1~1.5)                       │   │
│  │  • Generate new production parameters → hot-reload          │   │
│  │                                                              │   │
│  │  CRON 2: ANALYTICS_DAILY (daily at midnight)                │   │
│  │  • Aggregate Step 4.6 post-match analytics                  │   │
│  │  • Adaptive parameter updates                               │   │
│  │  • Generate daily report + send to Slack                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────── Infrastructure ──────────────────────────────────┐   │
│  │  Redis          │  PostgreSQL + TimescaleDB                 │   │
│  │  (real-time bus)│  (persistent storage)                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 24-Hour Timeline — Automated Operating Flow

```
00:00 ─── ANALYTICS_DAILY runs
│         • Post-analysis for prior day settled matches
│         • Adaptive parameter updates
│         • Daily report sent to Slack
│
06:00 ─── SCHEDULER: scan today’s matches
│         • Fetch today’s fixtures from Goalserve
│         • Identify kickoff times
│         • Create `match_jobs` records in DB
│
(example: kickoff at 12:30)
11:30 ─── SCHEDULER: spawn MatchEngine (60 min pre-kickoff)
│         • Auto-run Phase 2 Step 2.1~2.5
│         • Sanity Check → auto verdict GO/HOLD/SKIP
│         • If GO, wait; if SKIP, terminate engine
│
12:25 ─── Final pre-kickoff check (5 min pre-kickoff)
│         • Re-check lineup
│         • Verify connectivity
│
12:30 ─── Phase 3 starts: Live Trading Engine
│         • 3-Layer detection enabled
│         • 1-second tick loop
│         • Process goal/red-card events
│         • Phase 4 signal generation → order submission
│
14:15 ─── Match ends (expected)
│         • Detect Goalserve status "Finished"
│         • Wait for expiry settlement of remaining positions
│         • Receive Kalshi settlement result
│
14:20 ─── MatchEngine cleanup
│         • Final trade logs written to PostgreSQL
│         • Set Redis TTL for match data
│         • Terminate MatchEngine process
│
(same cycle for subsequent matches)
│
24:00 ─── ANALYTICS_DAILY runs again (day close)
```

---

## Process 1: SCHEDULER — Automated Scheduling Engine

### Role

Scans daily fixtures and automatically spawns/manages MatchEngines according to kickoff times.

### Implementation

```python
# src/scheduler/main.py

import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

class MatchScheduler:
    """
    Runs 24/7. Scans daily fixtures → spawns MatchEngine before kickoff.
    """
    def __init__(self, config: SystemConfig):
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.active_engines: Dict[str, MatchEngine] = {}
        self.db = PostgresClient(config.db_url)
        self.goalserve = GoalserveClient(config.goalserve_api_key)
        self.alerter = AlertService(config.alert_config)

    async def start(self):
        """System entry point"""
        # 1. Daily 06:00 UTC — scan today’s fixtures
        self.scheduler.add_job(
            self.scan_today_matches,
            'cron', hour=6, minute=0
        )

        # 2. Every 5 minutes — refresh schedule (time changes, postponements)
        self.scheduler.add_job(
            self.refresh_schedule,
            'interval', minutes=5
        )

        # 3. Every 10 seconds — engine health monitoring
        self.scheduler.add_job(
            self.monitor_engines,
            'interval', seconds=10
        )

        self.scheduler.start()

        # Immediate scan at startup
        await self.scan_today_matches()

        # Infinite loop
        while True:
            await asyncio.sleep(1)

    async def scan_today_matches(self):
        """Collect today’s fixtures from Goalserve Fixtures"""
        today = datetime.utcnow().strftime("%d.%m.%Y")

        for league_id in self.config.target_leagues:
            fixtures = await self.goalserve.get_fixtures(league_id, today)

            for match in fixtures:
                kickoff = parse_kickoff_time(match)
                match_id = match["id"]

                # Persist to DB
                await self.db.upsert_match_job(
                    match_id=match_id,
                    league_id=league_id,
                    kickoff_time=kickoff,
                    home_team=match["localteam"]["name"],
                    away_team=match["visitorteam"]["name"],
                    status="SCHEDULED"
                )

                # Schedule engine spawn 60 minutes before kickoff
                spawn_time = kickoff - timedelta(minutes=60)
                if spawn_time > datetime.utcnow():
                    self.scheduler.add_job(
                        self.spawn_engine,
                        'date',
                        run_date=spawn_time,
                        args=[match_id],
                        id=f"spawn_{match_id}",
                        replace_existing=True
                    )

        log.info(f"Scanned {today}: {len(fixtures)} matches scheduled")

    async def spawn_engine(self, match_id: str):
        """Create MatchEngine instance + run Phase 2"""
        if match_id in self.active_engines:
            return  # already running

        try:
            engine = MatchEngine(match_id, self.config)
            self.active_engines[match_id] = engine

            # Run Phase 2 (Pre-Match Initialization)
            result = await engine.run_prematch()

            if result.verdict == "SKIP":
                log.info(f"Match {match_id} SKIPPED by sanity check")
                await self.db.update_match_status(match_id, "SKIPPED")
                del self.active_engines[match_id]
                return

            if result.verdict == "HOLD":
                await self.alerter.send(
                    "WARNING",
                    f"Match {match_id} on HOLD — manual review needed"
                )

            # Wait for kickoff → auto-start Phase 3+4
            asyncio.create_task(engine.run_live())

            await self.db.update_match_status(match_id, "LIVE")
            log.info(f"Engine spawned for {match_id}")

        except Exception as e:
            log.error(f"Failed to spawn engine for {match_id}: {e}")
            await self.alerter.send("CRITICAL", f"Engine spawn failed: {match_id}\n{e}")

    async def monitor_engines(self):
        """Monitor health of active engines"""
        for match_id, engine in list(self.active_engines.items()):
            # Check match end
            if engine.is_finished():
                await self.handle_match_finished(match_id, engine)
                continue

            # Health check
            if not engine.is_healthy():
                await self.alerter.send(
                    "CRITICAL",
                    f"Engine unhealthy: {match_id}\n{engine.health_report()}"
                )

    async def handle_match_finished(self, match_id: str, engine: MatchEngine):
        """Cleanup after match end"""
        # 1. Final trade logs
        await engine.finalize_logs()

        # 2. Schedule post-analysis (immediate or batch)
        await self.db.update_match_status(match_id, "FINISHED")

        # 3. Engine teardown
        await engine.shutdown()
        del self.active_engines[match_id]

        log.info(f"Engine for {match_id} shut down after match end")
```

### Target League Configuration

```python
# config/leagues.yaml
target_leagues:
  - id: "1204"     # EPL
    name: "Premier League"
    priority: 1
  - id: "1399"     # La Liga
    name: "La Liga"
    priority: 1
  - id: "1229"     # Bundesliga
    name: "Bundesliga"
    priority: 2
  - id: "1269"     # Serie A
    name: "Serie A"
    priority: 2
  - id: "1221"     # Ligue 1
    name: "Ligue 1"
    priority: 2

# Include only leagues tradable on Kalshi.
# Adding a new league requires Phase 1 retraining.
```

---

## Process 2~N: MATCH_ENGINE — Per-Match Trading Engine

### Role

Manages the full lifecycle of a single match (Phase 2 → 3 → 4 → settlement).

### Lifecycle

```
SPAWNED ──(Phase 2)──▶ PREMATCH_READY ──(kickoff)──▶ LIVE
    │                       │                          │
    │                    SKIPPED                   FINISHED
    │                 (sanity failed)                  │
    └──────────────────────────────────────────────── SHUTDOWN
```

### Implementation

```python
# src/engine/match_engine.py

class MatchEngine:
    """
    One instance per match. Manages the full Phase 2~4 pipeline.
    """
    def __init__(self, match_id: str, config: SystemConfig):
        self.match_id = match_id
        self.config = config
        self.model = None  # LiveFootballQuantModel
        self.state = "SPAWNED"

        # Infrastructure connections
        self.redis = RedisClient(config.redis_url)
        self.db = PostgresClient(config.db_url)

        # Goalserve sources
        self.live_odds_source = GoalserveLiveOddsSource(config.goalserve_api_key)
        self.live_score_source = GoalserveLiveScoreSource(config.goalserve_api_key, match_id)

        # Kalshi
        self.kalshi = KalshiClient(config.kalshi_api_key)
        self.execution = ExecutionLayer(config.trading_mode)  # PAPER or LIVE

    async def run_prematch(self) -> SanityResult:
        """Phase 2: Pre-Match Initialization"""
        self.state = "PREMATCH"

        # Step 2.1~2.3
        prematch_data = await collect_prematch_data(self.match_id, self.config)
        X_match = apply_feature_mask(prematch_data, self.config.feature_mask)
        a_H, a_A, C_time = compute_a_parameters(X_match, self.config)

        # Step 2.4
        sanity = combined_sanity_check(a_H, a_A, prematch_data.odds_features)

        if sanity.verdict in ("GO", "GO_WITH_CAUTION"):
            # Step 2.5
            self.model = initialize_model(
                match_id=self.match_id,
                a_H=a_H, a_A=a_A, C_time=C_time,
                config=self.config
            )
            self.state = "PREMATCH_READY"

        return sanity

    async def run_live(self):
        """Phase 3+4: Live Trading — wait for kickoff then auto-start"""
        # Wait for kickoff
        await self._wait_for_kickoff()

        # Final check 5 minutes before kickoff
        if not await self._pre_kickoff_check():
            self.state = "SKIPPED"
            return

        self.state = "LIVE"

        try:
            # Run 3 coroutines concurrently
            await asyncio.gather(
                self._tick_loop(),
                self._live_odds_listener(),
                self._live_score_poller(),
            )
        except Exception as e:
            log.error(f"Engine {self.match_id} crashed: {e}")
            await self._emergency_shutdown(e)
        finally:
            self.state = "FINISHED"

    async def _tick_loop(self):
        """Coroutine 1: 1-second tick"""
        while self.model.engine_phase != "FINISHED":
            if self.model.engine_phase in ("FIRST_HALF", "SECOND_HALF"):
                self.model.t += 1/60

                # Step 3.2
                μ_H, μ_A = compute_remaining_mu(self.model)

                # Step 3.4
                P_true, σ_MC = await step_3_4_async(self.model, μ_H, μ_A)

                if P_true is not None:
                    # Step 4.2~4.5
                    await self._execute_trading_cycle(P_true, σ_MC)

                # State snapshot → Redis (dashboard)
                await self._publish_state_snapshot(P_true, σ_MC, μ_H, μ_A)

            await asyncio.sleep(1)

    async def _execute_trading_cycle(self, P_true, σ_MC):
        """Phase 4: signal → sizing → order (Phase 4 v2)"""
        order_allowed = (
            not self.model.cooldown
            and not self.model.ob_freeze
            and self.model.event_state == "IDLE"
        )

        for market in self.config.active_markets:
            P_bet365 = self.model.ob_sync.bet365_implied.get(market)

            # Step 4.2: signal generation (2-pass VWAP + market alignment)
            # Inside `generate_signal`:
            #   Pass 1: rough qty using best ask/bid
            #   Pass 2: final EV using VWAP for rough qty
            #   + market alignment check with bet365 → alignment_status
            signal = generate_signal(
                P_true[market], σ_MC,
                self.model.ob_sync,     # includes depth for VWAP
                P_bet365,
                self.config.fee_rate, self.config.z,
                self.config.K_frac,     # needed to derive rough qty in 2-pass
                self.model.bankroll,
                market
            )

            if signal.direction != "HOLD" and order_allowed:
                # Step 4.3: sizing (signal.P_kalshi is already VWAP effective price)
                f = compute_kelly(signal, self.config.fee_rate, self.config.K_frac)
                amount = apply_risk_limits(f, self.match_id, self.model.bankroll)

                if amount > 0:
                    # Step 4.5: execution (PAPER mode: VWAP + slippage simulation)
                    fill = await self.execution.execute_order(
                        signal, amount, self.model.ob_sync
                    )
                    if fill:
                        await self._record_trade(signal, fill)

            # Step 4.4: evaluate exits for existing positions (directional formulas)
            P_kalshi_bid = self.model.ob_sync.kalshi_best_bid
            for pos in self.model.positions.get(market, []):
                exit_signal = await evaluate_exit(
                    pos, P_true[market], σ_MC,
                    P_kalshi_bid, P_bet365,
                    self.config.fee_rate, self.config.z,
                    self.model.t, self.model.T
                )
                if exit_signal:
                    await self._execute_exit(pos, exit_signal)

    async def _publish_state_snapshot(self, P_true, σ_MC, μ_H, μ_A):
        """Publish state snapshot to Redis (consumed by dashboard + alert service)"""
        snapshot = {
            "match_id": self.match_id,
            "timestamp": time.time(),
            "t": self.model.t,
            "score": self.model.S,
            "X": self.model.X,
            "delta_S": self.model.delta_S,
            "mu_H": μ_H,
            "mu_A": μ_A,
            "P_true": P_true,
            "sigma_MC": σ_MC,
            "engine_phase": self.model.engine_phase,
            "event_state": self.model.event_state,
            "cooldown": self.model.cooldown,
            "ob_freeze": self.model.ob_freeze,
            "P_bet365": self.model.ob_sync.bet365_implied,
            "P_kalshi_bid": self.model.ob_sync.kalshi_best_bid,
            "P_kalshi_ask": self.model.ob_sync.kalshi_best_ask,
            "positions": serialize_positions(self.model.positions),
            "bankroll": self.model.bankroll,
        }
        await self.redis.publish(f"match:{self.match_id}:state", json.dumps(snapshot))

    async def _emergency_shutdown(self, error: Exception):
        """Safe handling on abnormal termination"""
        # 1. Cancel all unfilled orders
        await self.kalshi.cancel_all_orders()

        # 2. Send alert
        await self.redis.publish("alerts", json.dumps({
            "severity": "CRITICAL",
            "title": f"Engine Crash: {self.match_id}",
            "body": str(error),
        }))

        # 3. Persist crash state
        await self.db.record_engine_crash(self.match_id, str(error))

    def is_finished(self) -> bool:
        return self.state == "FINISHED"

    def is_healthy(self) -> bool:
        if self.state != "LIVE":
            return True
        # Unhealthy if last tick is older than 5 seconds
        return (time.time() - self.model.last_tick_time) < 5
```

---

## Process N+1: DASHBOARD_SERVER

### Implementation

```python
# src/dashboard/server.py

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Serve React build files
app.mount("/static", StaticFiles(directory="src/dashboard/frontend/build/static"))

@app.websocket("/ws/live/{match_id}")
async def ws_match_live(websocket: WebSocket, match_id: str):
    """Per-match real-time data stream"""
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"match:{match_id}:state")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe()

@app.websocket("/ws/portfolio")
async def ws_portfolio(websocket: WebSocket):
    """Portfolio-level real-time stream"""
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("match:*:state")

    try:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe()

@app.get("/api/analytics/health")
async def get_health_dashboard():
    """Layer 3: model health dashboard data"""
    return await compute_health_metrics(db)

@app.get("/api/analytics/calibration")
async def get_calibration():
    """Layer 3: calibration plot data"""
    return await compute_calibration_data(db)

@app.get("/api/analytics/pnl")
async def get_cumulative_pnl():
    """Layer 3: cumulative P&L data"""
    return await compute_cumulative_pnl(db)
```

---

## Process N+2: ALERT_SERVICE

```python
# src/alerts/main.py

class AlertService:
    """Subscribe to Redis events → send to Slack/Telegram"""

    async def start(self):
        pubsub = redis.pubsub()
        await pubsub.subscribe("alerts")
        await pubsub.psubscribe("match:*:state")

        async for message in pubsub.listen():
            if message["channel"] == "alerts":
                await self._handle_alert(json.loads(message["data"]))
            elif message["type"] == "pmessage":
                await self._check_state_alerts(json.loads(message["data"]))

    async def _check_state_alerts(self, state: dict):
        """Check auto-alert conditions from state snapshots"""
        # Drawdown check
        if state.get("drawdown_pct", 0) > 10:
            await self.send("CRITICAL", f"Drawdown {state['drawdown_pct']:.1f}%")

        # PRELIMINARY for over 30 seconds
        if (state.get("event_state") == "PRELIMINARY"
            and time.time() - state.get("preliminary_start", 0) > 30):
            await self.send("WARNING",
                f"PRELIMINARY >30s for {state['match_id']}. Possible VAR.")

        # Data source failure
        if state.get("live_odds_healthy") == False:
            await self.send("CRITICAL",
                f"Live Odds WS down for {state['match_id']}")
```

---

## Process N+3: DATA_COLLECTOR

```python
# src/data/collector.py

class DataCollector:
    """
    Runs 24/7. Continuously collects Goalserve historical data into DB.
    Prepares training inputs for Phase 1 recalibration.
    """
    async def start(self):
        scheduler = AsyncIOScheduler()

        # Daily 05:00 — collect yesterday results + stats
        scheduler.add_job(self.collect_yesterday_results, 'cron', hour=5)

        # Every 6 hours — pregame odds snapshot
        scheduler.add_job(self.collect_odds_snapshot, 'interval', hours=6)

        # Weekly — verify historical data integrity
        scheduler.add_job(self.verify_data_integrity, 'cron', day_of_week='sun', hour=3)

        scheduler.start()

    async def collect_yesterday_results(self):
        """Load completed matches (yesterday) with outcomes + detailed stats into DB"""
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d.%m.%Y")

        for league_id in config.target_leagues:
            # Fixtures/Results
            fixtures = await goalserve.get_fixtures(league_id, yesterday)
            for match in fixtures:
                if match["status"] == "Full-time":
                    await db.upsert_match_result(match)

            # Live Game Stats (historical matches)
            for match in fixtures:
                stats = await goalserve.get_match_stats(match["id"])
                if stats:
                    await db.upsert_match_stats(match["id"], stats)

            # Pregame Odds (closing odds)
            odds = await goalserve.get_odds(league_id, yesterday)
            for match_odds in odds:
                await db.upsert_match_odds(match_odds)
```

---

## CRON 1: RECALIBRATION — Phase 1 Retraining

```python
# src/calibration/recalibrate.py

class Recalibrator:
    """
    Re-runs full Phase 1 pipeline.
    Trigger: manual, season start, or Step 4.6 automated trigger.
    """
    async def run(self, trigger_reason: str):
        log.info(f"Recalibration started. Reason: {trigger_reason}")

        # Step 1.1: interval segmentation
        intervals = await build_intervals_from_db(self.db, self.config)

        # Step 1.2: Q matrix
        Q = estimate_Q_matrix(intervals, self.config)

        # Step 1.3: XGBoost ML
        model, feature_mask = train_xgboost_prior(intervals, self.db, self.config)

        # Step 1.4: NLL optimization
        params = joint_nll_optimization(intervals, model, Q, self.config)

        # Step 1.5: validation
        validation = walk_forward_validation(intervals, params, self.config)

        if validation.passes_all_criteria():
            # Deploy new parameters to production (hot-reload)
            new_version = await self.deploy_parameters(params, feature_mask, Q)
            log.info(f"New parameters deployed: version {new_version}")
            await self.alerter.send("INFO",
                f"Recalibration complete. New params v{new_version}")
        else:
            log.warning("Recalibration FAILED validation. Keeping old params.")
            await self.alerter.send("WARNING",
                f"Recalibration failed validation:\n{validation.report()}")

    async def deploy_parameters(self, params, feature_mask, Q):
        """
        Save new parameters + publish hot-reload signal via Redis.
        Active MatchEngines load new parameters starting from the next match.
        """
        version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        param_dir = f"data/parameters/{version}"
        os.makedirs(param_dir)

        # Save parameters
        save_params(params, f"{param_dir}/params.json")
        save_feature_mask(feature_mask, f"{param_dir}/feature_mask.json")
        save_Q_matrix(Q, f"{param_dir}/Q.npy")
        model.save_model(f"{param_dir}/xgboost.xgb")

        # Update symbolic link
        symlink_path = "data/parameters/production"
        if os.path.islink(symlink_path):
            os.unlink(symlink_path)
        os.symlink(param_dir, symlink_path)

        # Hot-reload signal
        await redis.publish("system:param_reload", version)

        return version
```

---

## CRON 2: ANALYTICS_DAILY

```python
# src/analytics/daily.py

class DailyAnalytics:
    """Runs daily at midnight. Post-match Step 4.6 analytics + adaptive updates."""

    async def run(self):
        # 1. Post-analysis for today’s settled matches
        today_trades = await self.db.get_today_settled_trades()

        if not today_trades:
            return

        # 2. Compute 11 metrics
        analytics = compute_all_analytics(today_trades, self.db)

        # 3. Adaptive parameter updates
        param_updates = adaptive_parameter_update(analytics)
        if param_updates:
            await self.apply_param_updates(param_updates)

        # 4. Check Phase 1 retraining trigger
        if analytics.get("brier_score_trend") == "worsening_3weeks":
            await self.trigger_recalibration("brier_score_degradation")

        # 5. Generate daily report + send Slack
        report = generate_daily_report(analytics, today_trades)
        await alerter.send("INFO", report)

        # 6. Save analytics to DB
        await self.db.save_daily_analytics(analytics)
```

---

## Parameter Hot-Reload

Mechanism to apply new parameters without restarting active MatchEngines:

```python
# inside src/engine/match_engine.py

async def _param_reload_listener(self):
    """Receive parameter reload signals from Redis"""
    pubsub = redis.pubsub()
    await pubsub.subscribe("system:param_reload")

    async for message in pubsub.listen():
        if message["type"] == "message":
            new_version = message["data"]
            log.info(f"Parameter reload signal: v{new_version}")

            # Do not apply to an in-progress match
            # Load in Phase 2 for the next match
            self.config.pending_param_version = new_version
```

> **Safety Rule:** Never change parameters during an active match.
> New parameters are loaded in Phase 2 of the next match.

---

## Failure Recovery (Self-Healing)

### Process Supervision — systemd

```ini
# /etc/systemd/system/kalshi-scheduler.service
[Unit]
Description=Kalshi Trading Scheduler
After=network.target redis.service postgresql.service

[Service]
Type=simple
User=kalshi
WorkingDirectory=/opt/kalshi
ExecStart=/opt/kalshi/.venv/bin/python -m src.scheduler.main
Restart=always
RestartSec=5
Environment=PYTHONPATH=/opt/kalshi

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/kalshi-dashboard.service
[Unit]
Description=Kalshi Dashboard Server
After=network.target redis.service

[Service]
Type=simple
User=kalshi
WorkingDirectory=/opt/kalshi
ExecStart=/opt/kalshi/.venv/bin/uvicorn src.dashboard.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/kalshi-alerts.service
# /etc/systemd/system/kalshi-collector.service
# (same pattern)
```

### Auto-Recovery by Failure Scenario

| Failure | Detection | Auto-Recovery | Alert |
|------|------|----------|------|
| MatchEngine crash | Scheduler monitor (10s) | Cancel unfilled orders, keep positions | 🔴 CRITICAL |
| Live Odds WS disconnect | 5s heartbeat | Reconnect 3 times → fallback to 2-Layer | 🔴 CRITICAL |
| Live Score polling failure | 3 consecutive HTTP failures | Retry + skip match at 5 failures | 🔴 CRITICAL |
| Kalshi WS disconnect | 10s heartbeat | Cancel unfilled orders → reconnect | 🔴 CRITICAL |
| Redis down | Connection failure | Retry every 10s + local queue | ⚠️ WARNING |
| PostgreSQL down | Connection failure | Buffer logs to local file | ⚠️ WARNING |
| Scheduler crash | systemd | Auto-restart after 5s | 🔴 CRITICAL |
| Server reboot | `systemd enable` | Auto-start all services | 🔴 CRITICAL |
| Goalserve API key expired | HTTP 401 | Alert → manual key refresh required | 🔴 CRITICAL |

---

## Configuration Management

### Global Configuration

```yaml
# config/system.yaml

# Trading mode
trading_mode: "paper"  # "paper" or "live"

# Goalserve
goalserve:
  api_key: "${GOALSERVE_API_KEY}"  # loaded from env var
  live_score_poll_interval: 3      # seconds
  live_odds_ws_url: "wss://goalserve.com/liveodds"

# Kalshi
kalshi:
  api_key: "${KALSHI_API_KEY}"
  api_secret: "${KALSHI_API_SECRET}"
  ws_url: "wss://trading-api.kalshi.com/trade-api/ws/v2"
  rest_url: "https://trading-api.kalshi.com/trade-api/v2"

# Risk parameters
risk:
  f_order_cap: 0.03
  f_match_cap: 0.05
  f_total_cap: 0.20
  initial_bankroll: 5000  # for PAPER mode

# Trading parameters (adaptive update targets)
trading:
  K_frac: 0.25
  z: 1.645
  theta_entry: 0.02
  theta_exit: 0.005
  cooldown_seconds: 15
  low_confidence_multiplier: 0.5
  rapid_entry_enabled: false
  bet365_divergence_auto_exit: false

# Infrastructure
redis:
  url: "redis://localhost:6379/0"
postgres:
  url: "postgresql://kalshi:${DB_PASSWORD}@localhost:5432/kalshi"

# Alerts
alerts:
  slack_webhook: "${SLACK_WEBHOOK_URL}"
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID}"

# Target leagues
target_leagues:
  - "1204"  # EPL
  - "1399"  # La Liga

# Target markets
active_markets:
  - "over_25"
  - "home_win"
  - "away_win"
  - "btts"
```

### Environment Separation

```
config/
├── system.yaml          # base config
├── system.paper.yaml    # PAPER mode overrides
├── system.live.yaml     # LIVE mode overrides
└── secrets.env          # API keys (gitignore)
```

---

## Database Schema

### PostgreSQL Tables

```sql
-- Match schedule + status
CREATE TABLE match_jobs (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    home_team       TEXT,
    away_team       TEXT,
    kickoff_time    TIMESTAMPTZ,
    status          TEXT DEFAULT 'SCHEDULED',  -- SCHEDULED/LIVE/FINISHED/SKIPPED
    sanity_verdict  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Trade logs (Step 4.5, Phase 4 v2)
CREATE TABLE trade_logs (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity_ordered INT,
    quantity_filled  INT,
    limit_price     NUMERIC(6,4),
    fill_price      NUMERIC(6,4),
    P_true          NUMERIC(6,4),
    P_true_cons     NUMERIC(6,4),
    P_kalshi        NUMERIC(6,4),        -- VWAP effective price (v2: VWAP, not best)
    P_kalshi_best   NUMERIC(6,4),        -- best ask/bid (added in v2 for VWAP comparison)
    P_bet365        NUMERIC(6,4),
    EV_adj          NUMERIC(6,4),        -- final EV after VWAP (v2)
    sigma_MC        NUMERIC(6,4),
    pricing_mode    TEXT,
    f_kelly         NUMERIC(6,4),
    K_frac          NUMERIC(4,2),
    alignment_status TEXT,               -- v2: "ALIGNED"|"DIVERGENT"|"UNAVAILABLE" (v1: bet365_confidence)
    kelly_multiplier NUMERIC(4,2),       -- v2: 0.8/0.5/0.6 (v1: 1.0/0.5)
    cooldown_active BOOLEAN,
    ob_freeze_active BOOLEAN,
    event_state     TEXT,
    engine_phase    TEXT,
    bankroll_before NUMERIC(10,2),
    bankroll_after  NUMERIC(10,2),
    is_paper        BOOLEAN DEFAULT FALSE,
    paper_slippage  NUMERIC(6,4)         -- v2: simulated paper-mode slippage
);

-- Positions (open + settled)
-- v2: `realized_pnl` uses direction-specific settlement formulas
--   Buy Yes: (settlement - entry_price) × quantity - fee
--   Buy No:  (entry_price - settlement) × quantity - fee
CREATE TABLE positions (
    id              BIGSERIAL PRIMARY KEY,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,       -- BUY_YES | BUY_NO
    entry_price     NUMERIC(6,4),        -- Yes probability space (Buy No: price at which Yes was sold)
    entry_time      TIMESTAMPTZ,
    quantity        INT,
    settlement      NUMERIC(6,4),        -- NULL if open, 1.00 or 0.00 at expiry
    realized_pnl    NUMERIC(10,2),       -- directional settlement (v2)
    closed_at       TIMESTAMPTZ,
    is_paper        BOOLEAN DEFAULT FALSE
);

-- Daily analytics output (Step 4.6)
CREATE TABLE daily_analytics (
    date            DATE PRIMARY KEY,
    brier_score     NUMERIC(6,4),
    delta_bs_pinnacle NUMERIC(6,4),
    edge_realization NUMERIC(6,4),
    max_drawdown_pct NUMERIC(6,4),
    bet365_alignment_value NUMERIC(6,4),  -- v2: market alignment value (v1: bet365_validation_value)
    preliminary_accuracy NUMERIC(6,4),
    yes_edge_realization NUMERIC(6,4),
    no_edge_realization NUMERIC(6,4),
    total_trades    INT,
    total_pnl       NUMERIC(10,2),
    K_frac          NUMERIC(4,2),
    z               NUMERIC(4,2),
    param_version   TEXT
);

-- Event logs (TimescaleDB hypertable)
CREATE TABLE event_logs (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    confidence      TEXT,
    data            JSONB
);
SELECT create_hypertable('event_logs', 'time');

-- Tick snapshots (TimescaleDB hypertable, analytics)
CREATE TABLE tick_snapshots (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    t               NUMERIC(6,2),
    score_h         INT,
    score_a         INT,
    state_x         INT,
    delta_s         INT,
    mu_h            NUMERIC(6,4),
    mu_a            NUMERIC(6,4),
    P_true          JSONB,      -- {"over_25": 0.58, "home_win": 0.42, ...}
    P_kalshi        JSONB,
    P_bet365        JSONB,
    sigma_MC        NUMERIC(6,4),
    engine_phase    TEXT,
    event_state     TEXT
);
SELECT create_hypertable('tick_snapshots', 'time');

-- Phase 1 parameter versioning
CREATE TABLE param_versions (
    version         TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  TEXT,
    validation_report JSONB,
    is_production   BOOLEAN DEFAULT FALSE
);

-- Historical match data (for Phase 1 training)
CREATE TABLE historical_matches (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT,
    date            DATE,
    home_team       TEXT,
    away_team       TEXT,
    ft_score_h      INT,
    ft_score_a      INT,
    ht_score_h      INT,
    ht_score_a      INT,
    added_time_1    INT,
    added_time_2    INT,
    summary         JSONB,    -- goals, redcards, yellowcards
    stats           JSONB,    -- team stats (shots, possession, etc.)
    player_stats    JSONB,    -- per-player stats
    odds            JSONB,    -- pregame odds (20+ bookmakers)
    lineups         JSONB,    -- formations + starting 11
    collected_at    TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Final Folder Structure

```
kalshi-soccer-quant/
│
├── README.md
├── LICENSE
├── pyproject.toml                     # Python package config (poetry/pip)
├── Makefile                           # build/deploy automation
├── docker-compose.yml                 # Redis + PostgreSQL + app (optional)
│
├── config/
│   ├── system.yaml                    # base config
│   ├── system.paper.yaml              # PAPER mode override
│   ├── system.live.yaml               # LIVE mode override
│   ├── leagues.yaml                   # target league list
│   └── secrets.env                    # API keys (gitignore)
│
├── data/
│   ├── parameters/                    # Phase 1 artifacts
│   │   ├── production -> ./20250915_120000/   # symlink (current production)
│   │   ├── 20250915_120000/           # versioned directory
│   │   │   ├── params.json            # b[], γ^H, γ^A, δ_H, δ_A
│   │   │   ├── Q.npy                  # Q matrix (4×4)
│   │   │   ├── xgboost.xgb            # XGBoost weights
│   │   │   ├── feature_mask.json      # selected feature list
│   │   │   ├── median_values.json     # medians for missing-value fill
│   │   │   └── validation_report.json # Step 1.5 validation result
│   │   └── 20250801_090000/           # previous version (rollback)
│   │       └── ...
│   │
│   └── cache/                         # runtime cache
│       ├── player_rolling/            # per-player rolling stats cache
│       └── team_rolling/              # per-team rolling stats cache
│
├── src/
│   ├── __init__.py
│   │
│   ├── common/                        # shared utilities
│   │   ├── __init__.py
│   │   ├── config.py                  # SystemConfig loader
│   │   ├── logging.py                 # structured logging
│   │   ├── redis_client.py            # Redis Pub/Sub wrapper
│   │   ├── db_client.py               # PostgreSQL wrapper
│   │   └── types.py                   # shared data types (NormalizedEvent, Signal, etc.)
│   │
│   ├── goalserve/                     # Goalserve API clients
│   │   ├── __init__.py
│   │   ├── client.py                  # REST client (Fixtures, Stats, Odds)
│   │   ├── live_score_source.py       # GoalserveLiveScoreSource (REST polling)
│   │   ├── live_odds_source.py        # GoalserveLiveOddsSource (WebSocket)
│   │   └── parsers.py                 # Goalserve JSON → internal type conversion
│   │
│   ├── kalshi/                        # Kalshi API clients
│   │   ├── __init__.py
│   │   ├── client.py                  # REST + WebSocket client
│   │   ├── orderbook.py               # OrderBookSync (VWAP buy/sell, depth)
│   │   └── execution.py               # ExecutionLayer (PAPER: VWAP+slippage+partial / LIVE: Kalshi REST)
│   │
│   ├── calibration/                   # Phase 1: Offline Calibration
│   │   ├── __init__.py
│   │   ├── step_1_1_intervals.py      # interval segmentation (VAR filter, own-goal handling)
│   │   ├── step_1_2_Q_matrix.py       # Markov Q matrix estimation
│   │   ├── step_1_3_ml_prior.py       # XGBoost Poisson training + feature selection
│   │   ├── step_1_4_nll.py            # Joint NLL optimization (PyTorch)
│   │   ├── step_1_5_validation.py     # Walk-Forward CV + diagnostics
│   │   ├── recalibrate.py             # retraining orchestrator
│   │   └── features/                  # feature engineering
│   │       ├── __init__.py
│   │       ├── tier1_team.py          # team-level rolling stats
│   │       ├── tier2_player.py        # player-level aggregation
│   │       ├── tier3_odds.py          # odds features
│   │       └── tier4_context.py       # context (H/A, rest days, H2H)
│   │
│   ├── prematch/                      # Phase 2: Pre-Match Initialization
│   │   ├── __init__.py
│   │   ├── step_2_1_data_collection.py    # lineup + stats + odds collection
│   │   ├── step_2_2_feature_selection.py  # apply feature_mask
│   │   ├── step_2_3_a_parameter.py        # invert a-params + C_time
│   │   ├── step_2_4_sanity_check.py       # multi-dimensional sanity check
│   │   └── step_2_5_initialization.py     # model instantiation + wiring
│   │
│   ├── engine/                        # Phase 3: Live Trading Engine
│   │   ├── __init__.py
│   │   ├── match_engine.py            # MatchEngine main class
│   │   ├── state_machine.py           # Engine phase + event state machine
│   │   ├── step_3_2_remaining_mu.py   # remaining expected goals (analytical + P_grid)
│   │   ├── step_3_3_event_handler.py  # preliminary/confirmed handler
│   │   ├── step_3_4_pricing.py        # hybrid pricing (analytical/MC)
│   │   ├── step_3_5_stoppage.py       # StoppageTimeManager
│   │   ├── mc_core.py                 # Numba JIT MC simulation core
│   │   └── ob_freeze.py               # 3-Layer ob_freeze logic
│   │
│   ├── trading/                       # Phase 4: Arbitrage & Execution
│   │   ├── __init__.py
│   │   ├── step_4_1_orderbook_sync.py # order-book sync + bet365 reference
│   │   ├── step_4_2_edge_detection.py # directional EV + 3-way cross-check
│   │   ├── step_4_3_position_sizing.py # Kelly + bet365 multiplier
│   │   ├── step_4_4_exit_logic.py     # 4 exit triggers
│   │   ├── step_4_5_order_execution.py # order submit + Rapid Entry
│   │   └── risk_manager.py            # 3-Layer risk caps
│   │
│   ├── analytics/                     # Step 4.6: post-analysis
│   │   ├── __init__.py
│   │   ├── daily.py                   # DailyAnalytics (daily midnight)
│   │   ├── metrics.py                 # 11 metrics computation
│   │   ├── adaptive_params.py         # adaptive updates for 7 parameters
│   │   └── reports.py                 # daily/weekly report generation
│   │
│   ├── scheduler/                     # automated scheduling
│   │   ├── __init__.py
│   │   └── main.py                    # MatchScheduler (24/7)
│   │
│   ├── data/                          # data collection
│   │   ├── __init__.py
│   │   └── collector.py               # DataCollector (24/7)
│   │
│   ├── alerts/                        # alert service
│   │   ├── __init__.py
│   │   ├── main.py                    # AlertService (24/7)
│   │   ├── slack.py                   # Slack webhook
│   │   └── telegram.py                # Telegram bot
│   │
│   └── dashboard/                     # dashboard
│       ├── __init__.py
│       ├── server.py                  # FastAPI + WebSocket server
│       ├── api/                       # REST API endpoints
│       │   ├── __init__.py
│       │   ├── live.py                # Layer 1 data
│       │   ├── portfolio.py           # Layer 2 data
│       │   └── analytics.py           # Layer 3 data
│       │
│       └── frontend/                  # React frontend
│           ├── package.json
│           ├── src/
│           │   ├── App.jsx
│           │   ├── index.jsx
│           │   ├── components/
│           │   │   ├── Layout/
│           │   │   │   ├── Navbar.jsx
│           │   │   │   └── ModeBadge.jsx      # PAPER/LIVE indicator
│           │   │   │
│           │   │   ├── Layer1_LiveMatch/
│           │   │   │   ├── MatchPanel.jsx      # per-match panel container
│           │   │   │   ├── MatchHeader.jsx     # 1A: status header
│           │   │   │   ├── PriceChart.jsx      # 1B: P_true vs P_kalshi vs P_bet365 ⭐
│           │   │   │   ├── MuChart.jsx         # 1C: μ decay chart
│           │   │   │   ├── SignalPanel.jsx     # 1D: signals + positions
│           │   │   │   ├── EventLog.jsx        # 1E: event log
│           │   │   │   └── SourceStatus.jsx    # 1F: data source status
│           │   │   │
│           │   │   ├── Layer2_Portfolio/
│           │   │   │   ├── RiskDashboard.jsx   # 2A: risk dashboard
│           │   │   │   ├── PositionTable.jsx   # 2B: position table
│           │   │   │   └── PnLTimeline.jsx     # 2C: P&L timeline
│           │   │   │
│           │   │   └── Layer3_Analytics/
│           │   │       ├── HealthDashboard.jsx # 3A: health dashboard
│           │   │       ├── CalibrationPlot.jsx # 3B: calibration plot
│           │   │       ├── CumulativePnL.jsx   # 3C: cumulative P&L + drawdown
│           │   │       ├── DirectionalAnalysis.jsx # 3D: directional analysis
│           │   │       ├── Bet365Effect.jsx    # 3E: bet365 validation effect
│           │   │       ├── PrelimAccuracy.jsx  # 3F: preliminary accuracy
│           │   │       └── ParamHistory.jsx    # 3G: parameter history
│           │   │
│           │   ├── hooks/
│           │   │   ├── useMatchStream.js       # WebSocket live stream
│           │   │   ├── usePortfolio.js         # portfolio aggregation
│           │   │   └── useAnalytics.js         # analytics API calls
│           │   │
│           │   └── utils/
│           │       ├── formatters.js           # price/P&L formatting
│           │       └── colors.js               # status color codes
│           │
│           └── build/                 # React build output (gitignore)
│
├── scripts/
│   ├── setup_db.sql                   # PostgreSQL schema init
│   ├── setup_systemd.sh               # systemd service registration
│   ├── deploy.sh                      # deployment script
│   ├── backup_db.sh                   # DB backup
│   └── run_recalibration.py           # manual retraining trigger
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # pytest fixtures (mock Goalserve, mock Kalshi)
│   │
│   ├── unit/
│   │   ├── test_intervals.py          # Step 1.1 interval segmentation
│   │   ├── test_Q_matrix.py           # Step 1.2 Q estimation
│   │   ├── test_nll.py                # Step 1.4 NLL math validation
│   │   ├── test_a_parameter.py        # Step 2.3 inversion
│   │   ├── test_remaining_mu.py       # Step 3.2 integration
│   │   ├── test_mc_core.py            # Step 3.4 MC simulation
│   │   ├── test_edge_detection.py     # Step 4.2 EV + directional P_cons
│   │   ├── test_kelly.py              # Step 4.3 Kelly
│   │   ├── test_exit_logic.py         # Step 4.4 exit triggers
│   │   └── test_risk_limits.py        # 3-Layer risk
│   │
│   ├── integration/
│   │   ├── test_goalserve_client.py   # Goalserve API integration
│   │   ├── test_kalshi_client.py      # Kalshi API integration
│   │   ├── test_match_engine.py       # full MatchEngine flow
│   │   └── test_scheduler.py          # scheduler spawn logic
│   │
│   └── replay/
│       ├── replay_engine.py           # historical replay (backtest)
│       └── test_replay.py             # replay-based system validation
│
├── docs/
│   ├── phase1.md         # Phase 1 design doc
│   ├── phase2.md         # Phase 2 design doc
│   ├── phase3.md         # Phase 3 design doc
│   ├── phase4.md         # Phase 4 design doc
│   ├── dashboard_design.md         # dashboard design doc
│   └── implementation_blueprint.md    # this document
│
└── logs/                              # runtime logs (gitignore)
    ├── scheduler.log
    ├── engine/
    │   ├── ARS-CHE-20251018.log       # per-match engine logs
    │   └── ...
    ├── dashboard.log
    ├── alerts.log
    └── collector.log
```

---

## Implementation Roadmap (Build Order)

### Sprint 1: Foundation Infrastructure (1~2 weeks)

```
Goal: processes start, data flows, dashboard is visible

├── config/ configuration file structure
├── src/common/ (config, logging, redis, db, types)
├── src/goalserve/client.py (REST API — Fixtures, Stats, Odds)
├── src/data/collector.py (start historical data ingestion)
├── scripts/setup_db.sql (PostgreSQL schema)
├── docker-compose.yml (Redis + PostgreSQL)
└── Validation: Goalserve API call success + DB insert verified
```

### Sprint 2: Phase 1 — Offline Calibration (2~3 weeks)

```
Goal: train model parameters from historical data

├── src/calibration/step_1_1_intervals.py
├── src/calibration/step_1_2_Q_matrix.py
├── src/calibration/features/ (Tier 1~4)
├── src/calibration/step_1_3_ml_prior.py
├── src/calibration/step_1_4_nll.py (PyTorch)
├── src/calibration/step_1_5_validation.py
├── tests/unit/test_intervals.py, test_nll.py, test_Q_matrix.py
└── Validation: Walk-Forward CV passes, Brier Score < Pinnacle
```

### Sprint 3: Phase 2 + 3 Core (2~3 weeks)

```
Goal: pre-match initialization + real-time μ/P_true computation

├── src/prematch/ (Step 2.1~2.5)
├── src/engine/mc_core.py (Numba JIT)
├── src/engine/step_3_2_remaining_mu.py
├── src/engine/step_3_4_pricing.py
├── src/engine/state_machine.py
├── tests/unit/test_remaining_mu.py, test_mc_core.py
└── Validation: verify P_true output via historical replay
```

### Sprint 4: Phase 3 Event Handling + Phase 4 Execution (2~3 weeks)

```
Goal: event handling + trading signals + order execution (PAPER)

├── src/goalserve/live_odds_source.py (WebSocket)
├── src/goalserve/live_score_source.py (REST polling)
├── src/engine/step_3_3_event_handler.py
├── src/engine/ob_freeze.py
├── src/trading/ (Step 4.1~4.5)
├── src/kalshi/ (client, orderbook, execution)
├── tests/unit/test_edge_detection.py, test_kelly.py, test_exit_logic.py
└── Validation: verify signal generation + simulated orders in replay
```

### Sprint 5: Scheduler + 24/7 Automation (1~2 weeks)

```
Goal: fully automated operation from startup

├── src/scheduler/main.py
├── src/alerts/main.py
├── scripts/setup_systemd.sh
├── tests/integration/test_scheduler.py
└── Validation: scheduler scans fixtures → spawns engine → PAPER trading
```

### Sprint 6: Dashboard (2~3 weeks)

```
Goal: real-time monitoring + analytics view

├── src/dashboard/server.py
├── src/dashboard/frontend/ (React)
│   ├── Layer 1: PriceChart + MatchHeader + EventLog (required in Phase 0)
│   ├── Layer 2: RiskDashboard + PositionTable + PnLTimeline
│   └── Layer 3: HealthDashboard (expand in later sprints)
└── Validation: confirm real-time data in browser
```

### Sprint 7: Post-Analysis + Adaptive Parameters (1~2 weeks)

```
Goal: automate Step 4.6 + feedback loop

├── src/analytics/ (daily, metrics, adaptive_params, reports)
├── CRON 2 setup
├── Slack/Telegram alert integration
└── Validation: daily report auto-generated + received in Slack
```

### Sprint 8: Phase 0 PAPER Trading Period (2~4 weeks ops)

```
Goal: validate system behavior on real matches in PAPER mode

├── Run 24/7 PAPER on live matches
├── Accumulate hypothetical P&L
├── Measure preliminary accuracy
├── Measure bet365 cross-check effectiveness
├── Bug fixes + stabilization
└── Decision: does system meet Phase A transition criteria?
```

### Sprint 9: Phase A LIVE Transition (1 week prep + operations)

```
Goal: start conservative live trading with real capital

├── configure `config/system.live.yaml`
├── `trading_mode`: "paper" → "live"
├── `K_frac = 0.25`, `z = 1.645`
├── connect Kalshi live account
└── stronger monitoring: drawdown alert threshold 10%
```

---

## Deployment Checklist

### Server Requirements

| Item | Minimum Spec | Recommended Spec |
|------|----------|----------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Storage | 50 GB SSD | 100 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Network | Static IP (Goalserve whitelist) | Static IP |
| Location | US East (close to Kalshi servers) | US East |

### Initial Deployment Sequence

```
1. Provision server + secure static IP
2. Register Goalserve IP whitelist
3. Install Redis + PostgreSQL
4. Set up Python 3.11+ environment
5. Deploy code + install dependencies
6. Initialize DB schema (`scripts/setup_db.sql`)
7. Run initial Phase 1 (create `data/parameters/production`)
8. Register systemd services (`scripts/setup_systemd.sh`)
9. Build/serve dashboard React app
10. Start in PAPER mode → enter Sprint 8
```
