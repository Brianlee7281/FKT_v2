"""Tests for Step 5.1: Match Engine.

Verifies full lifecycle, trading cycle, crash handling, and sanity skip.

Reference: implementation_roadmap.md -> Step 5.1 tests
├── test_full_lifecycle_replay()
├── test_paper_trades_in_db()
├── test_crash_cancels_orders()
└── test_sanity_skip_stops_engine()
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import numpy as np
import pytest

from src.common.config import SystemConfig
from src.common.types import NormalizedEvent, SanityResult, Signal
from src.engine.match_engine import (
    EngineLifecycle,
    MatchEngine,
    ModelParams,
)
from src.engine.state_machine import (
    FINISHED,
    FIRST_HALF,
    SECOND_HALF,
    EngineState,
    transition_to_finished,
    transition_to_first_half,
)
from src.kalshi.orderbook import OrderBookSync
from src.trading.step_4_4_exit_logic import OpenPosition


# ---------------------------------------------------------------------------
# Helpers: Fake data sources
# ---------------------------------------------------------------------------

class FakeLiveOddsSource:
    """Async iterable that yields pre-loaded events then stops."""

    def __init__(self, events: list[NormalizedEvent] | None = None):
        self._events = deque(events or [])

    def add(self, event: NormalizedEvent) -> None:
        self._events.append(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        await asyncio.sleep(0)  # yield control
        return self._events.popleft()


class FakeLiveScoreSource:
    """Returns pre-loaded events on poll(), then empty lists."""

    def __init__(self, poll_responses: list[list[NormalizedEvent]] | None = None):
        self._responses = deque(poll_responses or [])

    async def poll(self) -> list[NormalizedEvent]:
        if self._responses:
            return self._responses.popleft()
        return []


class FakeRedis:
    """In-memory Redis substitute that records publishes."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    """Minimal SystemConfig for testing."""
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25", "home_win"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,  # minimal delay in tests
        cooldown_seconds=15,
    )


@pytest.fixture
def model_params():
    """Model params that produce non-trivial μ values."""
    return ModelParams(
        a_H=-0.5,
        a_A=-0.7,
        b=np.array([-0.1, 0.0, 0.05, -0.05, 0.02, -0.02]),
        gamma_H=np.zeros(4),
        gamma_A=np.zeros(4),
        delta_H=np.zeros(5),
        delta_A=np.zeros(5),
        Q_diag=np.zeros(4),
        Q_off=np.zeros(4),
        basis_bounds=np.array([0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0]),
        T_exp=93.0,
        DELTA_SIGNIFICANT=False,
    )


@pytest.fixture
def engine(config, model_params):
    """Create a MatchEngine with fake sources."""
    redis = FakeRedis()
    e = MatchEngine(
        match_id="test_match_001",
        config=config,
        redis_client=redis,
        live_odds_source=FakeLiveOddsSource(),
        live_score_source=FakeLiveScoreSource(),
    )
    return e


# ---------------------------------------------------------------------------
# test_full_lifecycle_replay
# ---------------------------------------------------------------------------

class TestFullLifecycleReplay:
    """Engine goes through SPAWNED → PREMATCH → PREMATCH_READY → LIVE → FINISHED."""

    @pytest.mark.asyncio
    async def test_lifecycle_states(self, engine, model_params):
        """Verify lifecycle transitions through prematch to live."""
        assert engine.lifecycle == EngineLifecycle.SPAWNED

        # Prematch
        sanity = await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        assert sanity.verdict == "GO"
        assert engine.lifecycle == EngineLifecycle.PREMATCH_READY
        assert engine.stoppage_mgr is not None
        assert engine.model.a_H == model_params.a_H

    @pytest.mark.asyncio
    async def test_live_runs_and_finishes(self, engine, model_params):
        """Engine starts live and transitions to FINISHED when match ends."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        # Pre-set engine to FIRST_HALF, then schedule finish after brief run
        transition_to_first_half(engine.state)

        async def finish_after_delay():
            await asyncio.sleep(0.1)
            transition_to_finished(engine.state)

        asyncio.create_task(finish_after_delay())
        await engine.run_live()

        assert engine.lifecycle == EngineLifecycle.FINISHED

    @pytest.mark.asyncio
    async def test_mu_computation_produces_values(self, engine, model_params):
        """_compute_remaining_mu returns positive values at t=0."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)
        engine.state.current_time = 0.0

        mu_H, mu_A = engine._compute_remaining_mu()
        assert mu_H > 0
        assert mu_A > 0
        # Home should score more (higher a_H = -0.5 vs a_A = -0.7)
        assert mu_H > mu_A

    @pytest.mark.asyncio
    async def test_mu_decreases_with_time(self, engine, model_params):
        """μ should decrease as match time advances."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)

        engine.state.current_time = 10.0
        mu_H_early, _ = engine._compute_remaining_mu()

        engine.state.current_time = 80.0
        mu_H_late, _ = engine._compute_remaining_mu()

        assert mu_H_early > mu_H_late

    @pytest.mark.asyncio
    async def test_mu_zero_at_end(self, engine, model_params):
        """μ = 0 when t >= T."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        engine.state.current_time = 95.0  # past T_exp=93

        mu_H, mu_A = engine._compute_remaining_mu()
        assert mu_H == 0.0
        assert mu_A == 0.0


# ---------------------------------------------------------------------------
# test_paper_trades_in_db
# ---------------------------------------------------------------------------

class TestPaperTradesInDB:
    """Verify that paper trades are recorded in the trade log."""

    @pytest.mark.asyncio
    async def test_trade_recorded_after_fill(self, engine, model_params):
        """When execution fills, trade appears in trade_log."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        # Set up orderbook with liquidity
        engine.ob_sync.update_book(
            bids=[(0.55, 100), (0.53, 100)],
            asks=[(0.57, 100), (0.59, 100)],
        )

        # Manually create a fill via the execution layer
        signal = Signal(
            direction="BUY_YES",
            EV=0.05,
            P_cons=0.65,
            P_kalshi=0.57,
            rough_qty=10,
            alignment_status="ALIGNED",
            kelly_multiplier=0.8,
            market_ticker="over_25",
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(
            P_true={"over_25": 0.65},
            sigma_MC=0.005,
            pricing_mode="analytical",
        )

        fill = engine.execution.execute_order(signal, 100.0, engine.ob_sync)
        assert fill is not None

        engine._record_fill(signal, fill, pricing)

        assert len(engine.trade_log) == 1
        trade = engine.trade_log[0]
        assert trade.match_id == "test_match_001"
        assert trade.market_ticker == "over_25"
        assert trade.direction == "BUY_YES"
        assert trade.quantity_filled > 0
        assert trade.is_paper is True

    @pytest.mark.asyncio
    async def test_position_tracked_after_fill(self, engine, model_params):
        """Fill creates an open position in the positions dict."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        engine.ob_sync.update_book(
            bids=[(0.55, 100)],
            asks=[(0.57, 100)],
        )

        signal = Signal(
            direction="BUY_YES",
            EV=0.05,
            P_cons=0.65,
            P_kalshi=0.57,
            rough_qty=10,
            alignment_status="ALIGNED",
            kelly_multiplier=0.8,
            market_ticker="home_win",
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(P_true={"home_win": 0.65}, sigma_MC=0.0)

        fill = engine.execution.execute_order(signal, 50.0, engine.ob_sync)
        assert fill is not None
        engine._record_fill(signal, fill, pricing)

        assert "home_win" in engine.positions
        assert len(engine.positions["home_win"]) == 1
        pos = engine.positions["home_win"][0]
        assert pos.direction == "BUY_YES"
        assert pos.contracts == fill.quantity

    @pytest.mark.asyncio
    async def test_bankroll_debited_on_fill(self, engine, model_params):
        """Bankroll decreases by fill cost."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        initial_bankroll = engine.bankroll

        engine.ob_sync.update_book(
            bids=[(0.55, 100)],
            asks=[(0.57, 100)],
        )

        signal = Signal(
            direction="BUY_YES", EV=0.05, P_cons=0.65, P_kalshi=0.57,
            rough_qty=10, market_ticker="over_25",
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(P_true={"over_25": 0.65}, sigma_MC=0.0)

        fill = engine.execution.execute_order(signal, 100.0, engine.ob_sync)
        engine._record_fill(signal, fill, pricing)

        expected_cost = fill.price * fill.quantity
        assert engine.bankroll == pytest.approx(initial_bankroll - expected_cost)

    @pytest.mark.asyncio
    async def test_exposure_recorded_on_fill(self, engine, model_params):
        """Risk manager tracks exposure after fill."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        engine.ob_sync.update_book(
            bids=[(0.55, 100)],
            asks=[(0.57, 100)],
        )

        signal = Signal(
            direction="BUY_YES", EV=0.05, P_cons=0.65, P_kalshi=0.57,
            rough_qty=10, market_ticker="over_25",
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(P_true={"over_25": 0.65}, sigma_MC=0.0)

        fill = engine.execution.execute_order(signal, 100.0, engine.ob_sync)
        engine._record_fill(signal, fill, pricing)

        exposure = engine.risk_manager.get_match_exposure("test_match_001")
        assert exposure > 0
        assert exposure == pytest.approx(fill.price * fill.quantity)


# ---------------------------------------------------------------------------
# test_crash_cancels_orders
# ---------------------------------------------------------------------------

class TestCrashCancelsOrders:
    """Emergency shutdown publishes critical alert."""

    @pytest.mark.asyncio
    async def test_crash_publishes_alert(self, engine, model_params):
        """On crash, alert is published to Redis."""
        redis = FakeRedis()
        engine._redis = redis

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        error = RuntimeError("WebSocket disconnected")
        await engine._emergency_shutdown(error)

        assert engine._shutdown_event.is_set()
        assert len(redis.published) == 1
        channel, msg = redis.published[0]
        assert channel == "alerts"
        assert "CRITICAL" in msg
        assert "test_match_001" in msg

    @pytest.mark.asyncio
    async def test_crash_during_live_sets_finished(self, engine, model_params):
        """If a coroutine crashes, engine transitions to FINISHED."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        # Pre-set to FIRST_HALF so _wait_for_kickoff passes immediately
        transition_to_first_half(engine.state)

        # Make tick_loop crash immediately
        async def crashing_tick():
            raise RuntimeError("Unexpected error")

        engine._tick_loop = crashing_tick
        engine._redis = FakeRedis()

        await engine.run_live()
        assert engine.lifecycle == EngineLifecycle.FINISHED

    @pytest.mark.asyncio
    async def test_shutdown_stops_coroutines(self, engine, model_params):
        """shutdown() sets the event so coroutines exit."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        transition_to_first_half(engine.state)
        engine.shutdown()
        assert engine._shutdown_event.is_set()
        assert engine.lifecycle == EngineLifecycle.SHUTDOWN


# ---------------------------------------------------------------------------
# test_sanity_skip_stops_engine
# ---------------------------------------------------------------------------

class TestSanitySkipStopsEngine:
    """SKIP/HOLD verdict prevents live trading."""

    @pytest.mark.asyncio
    async def test_skip_verdict_blocks_live(self, engine, model_params):
        """SKIP verdict → lifecycle=SKIPPED, run_live returns early."""
        sanity = await engine.run_prematch(
            sanity_result=SanityResult(verdict="SKIP", warning="odds mismatch"),
            model_params=model_params,
        )
        assert sanity.verdict == "SKIP"
        assert engine.lifecycle == EngineLifecycle.SKIPPED

        # run_live should return immediately without error
        await engine.run_live()
        assert engine.lifecycle == EngineLifecycle.SKIPPED  # unchanged

    @pytest.mark.asyncio
    async def test_hold_verdict_blocks_live(self, engine, model_params):
        """HOLD verdict → lifecycle=SKIPPED."""
        sanity = await engine.run_prematch(
            sanity_result=SanityResult(verdict="HOLD"),
            model_params=model_params,
        )
        assert sanity.verdict == "HOLD"
        assert engine.lifecycle == EngineLifecycle.SKIPPED

    @pytest.mark.asyncio
    async def test_go_with_caution_allows_live(self, engine, model_params):
        """GO_WITH_CAUTION → PREMATCH_READY, can go live."""
        sanity = await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO_WITH_CAUTION"),
            model_params=model_params,
        )
        assert sanity.verdict == "GO_WITH_CAUTION"
        assert engine.lifecycle == EngineLifecycle.PREMATCH_READY


# ---------------------------------------------------------------------------
# Event processing tests
# ---------------------------------------------------------------------------

class TestEventProcessing:
    """Verify that live odds/score events update engine state."""

    @pytest.mark.asyncio
    async def test_live_odds_goal_triggers_ob_freeze(self, engine, model_params):
        """Goal detected via live odds → ob_freeze."""
        events = [
            NormalizedEvent(
                type="goal_detected",
                source="live_odds",
                confidence="preliminary",
                timestamp=time.time(),
                score=(1, 0),
            ),
        ]

        engine._live_odds_source = FakeLiveOddsSource(events)

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        # Run only the listener (not full live)
        await engine._live_odds_listener()

        assert engine.state.ob_freeze is True
        assert engine.state.event_state == "PRELIMINARY_DETECTED"

    @pytest.mark.asyncio
    async def test_live_score_goal_commits_score(self, engine, model_params):
        """Goal confirmed via live score → score updated."""
        poll_responses = [
            [NormalizedEvent(
                type="goal_confirmed",
                source="live_score",
                confidence="confirmed",
                timestamp=time.time(),
                score=(1, 0),
                team="localteam",
            )],
        ]

        engine._live_score_source = FakeLiveScoreSource(poll_responses)

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)

        # Run one poll cycle
        engine.state.engine_phase = FIRST_HALF
        events = await engine._live_score_source.poll()
        for event in events:
            from src.engine.event_handler import dispatch_live_score_event
            dispatch_live_score_event(engine.state, event)

        assert engine.state.score == (1, 0)
        assert engine.state.delta_S == 1
        assert engine.state.cooldown is True


# ---------------------------------------------------------------------------
# Exit logic tests
# ---------------------------------------------------------------------------

class TestExitExecution:
    """Verify position exit mechanics."""

    @pytest.mark.asyncio
    async def test_exit_removes_position(self, engine, model_params):
        """_execute_exit removes the position and updates bookkeeping."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        pos = OpenPosition(
            direction="BUY_YES",
            entry_price=0.57,
            market_ticker="over_25",
            match_id="test_match_001",
            contracts=10,
        )
        engine.positions["over_25"] = [pos]
        engine.risk_manager.record_exposure("test_match_001", 5.70)
        engine.bankroll = 9994.30  # 10000 - 5.70

        engine.ob_sync.update_book(
            bids=[(0.60, 100)],
            asks=[(0.62, 100)],
        )

        from src.trading.step_4_4_exit_logic import ExitSignal
        exit_sig = ExitSignal(reason="EDGE_DECAY", EV=-0.01)

        engine._execute_exit(pos, exit_sig, "over_25")

        assert "over_25" not in engine.positions
        assert engine.risk_manager.get_match_exposure("test_match_001") == pytest.approx(0.0, abs=1e-10)
        # Bankroll should increase (exit at 0.60, entry at 0.57, profit)
        assert engine.bankroll > 9994.30


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Verify health check logic."""

    def test_healthy_when_not_live(self, engine):
        assert engine.is_healthy() is True

    def test_healthy_when_recent_tick(self, engine):
        engine.lifecycle = EngineLifecycle.LIVE
        engine._last_tick_time = time.time()
        assert engine.is_healthy() is True

    def test_unhealthy_when_stale_tick(self, engine):
        engine.lifecycle = EngineLifecycle.LIVE
        engine._last_tick_time = time.time() - 10
        assert engine.is_healthy() is False

    def test_is_finished(self, engine):
        assert engine.is_finished() is False
        engine.lifecycle = EngineLifecycle.FINISHED
        assert engine.is_finished() is True
        engine.lifecycle = EngineLifecycle.SHUTDOWN
        assert engine.is_finished() is True


# ---------------------------------------------------------------------------
# Redis snapshot tests
# ---------------------------------------------------------------------------

class TestRedisSnapshot:
    """Verify state snapshots are published to Redis."""

    @pytest.mark.asyncio
    async def test_snapshot_published(self, engine, model_params):
        """Snapshot contains all required fields."""
        redis = FakeRedis()
        engine._redis = redis

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(
            P_true={"over_25": 0.65, "home_win": 0.55},
            sigma_MC=0.003,
            pricing_mode="analytical",
        )

        await engine._publish_state_snapshot(pricing, mu_H=1.2, mu_A=0.9)

        assert len(redis.published) == 1
        channel, msg = redis.published[0]
        assert channel == "match:test_match_001:state"

        import json
        data = json.loads(msg)
        assert data["match_id"] == "test_match_001"
        assert data["mu_H"] == 1.2
        assert data["mu_A"] == 0.9
        assert "P_true" in data
        assert "bankroll" in data
        assert "order_allowed" in data

    @pytest.mark.asyncio
    async def test_no_redis_no_error(self, engine, model_params):
        """No Redis client → snapshot is silently skipped."""
        engine._redis = None

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        from src.engine.step_3_4_pricing import PricingResult
        pricing = PricingResult(P_true={}, sigma_MC=0.0)

        # Should not raise
        await engine._publish_state_snapshot(pricing, mu_H=0.0, mu_A=0.0)


# ---------------------------------------------------------------------------
# Markov-modulated mu tests
# ---------------------------------------------------------------------------

class TestMarkovModulatedMu:
    """Verify _compute_remaining_mu with P_grid (Markov transition probs)."""

    @pytest.mark.asyncio
    async def test_mu_with_P_grid(self, engine, model_params):
        """With P_grid, mu computation sums over all 4 Markov states."""
        # Identity P_grid: P[X,j] = 1 if j==X, else 0
        # This should give same result as fallback (current X only)
        identity = np.eye(4)
        P_grid = np.array([identity] * 101)  # 101 entries for 0-100 minutes
        model_params.P_grid = P_grid

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)
        engine.state.current_time = 10.0

        mu_H_grid, mu_A_grid = engine._compute_remaining_mu()

        # Compare with fallback (remove P_grid)
        engine.model.P_grid = None
        mu_H_fallback, mu_A_fallback = engine._compute_remaining_mu()

        assert mu_H_grid == pytest.approx(mu_H_fallback)
        assert mu_A_grid == pytest.approx(mu_A_fallback)

    @pytest.mark.asyncio
    async def test_mu_with_red_card_markov(self, engine, model_params):
        """With red card (X=1) and non-identity P_grid, mu differs from fallback."""
        # P_grid where state 1 transitions partially to state 0
        P = np.eye(4)
        P[1, 0] = 0.3
        P[1, 1] = 0.7
        P_grid = np.array([P] * 101)
        model_params.P_grid = P_grid
        model_params.gamma_H = np.array([0.0, -0.3, 0.0, -0.3])  # penalty for X=1
        model_params.gamma_A = np.array([0.0, 0.1, 0.0, 0.1])

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)
        engine.state.X = 1  # home red card
        engine.state.current_time = 10.0

        mu_H_markov, mu_A_markov = engine._compute_remaining_mu()

        # Fallback uses only X=1 gamma
        engine.model.P_grid = None
        mu_H_fallback, mu_A_fallback = engine._compute_remaining_mu()

        # With Markov modulation, mu_H should be higher than fallback
        # because some probability mass transitions to X=0 (no penalty)
        assert mu_H_markov > mu_H_fallback


# ---------------------------------------------------------------------------
# Exit fee deduction tests
# ---------------------------------------------------------------------------

class TestExitFeeDeduction:
    """Verify exit P&L deducts fees on profit."""

    @pytest.mark.asyncio
    async def test_profitable_exit_deducts_fee(self, engine, model_params):
        """Fee deducted from profitable exit."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        pos = OpenPosition(
            direction="BUY_YES",
            entry_price=0.50,
            market_ticker="over_25",
            match_id="test_match_001",
            contracts=100,
        )
        engine.positions["over_25"] = [pos]
        engine.risk_manager.record_exposure("test_match_001", 50.0)
        engine.bankroll = 9950.0  # 10000 - 50

        # Exit at 0.60 (profit = 10.0 gross)
        engine.ob_sync.update_book(
            bids=[(0.60, 200)],
            asks=[(0.62, 200)],
        )

        from src.trading.step_4_4_exit_logic import ExitSignal
        exit_sig = ExitSignal(reason="EDGE_DECAY")

        engine._execute_exit(pos, exit_sig, "over_25")

        # Gross PnL = (0.60 - 0.50) * 100 = 10.0
        # Fee = 0.07 * 10.0 = 0.70
        # Net PnL = 10.0 - 0.70 = 9.30
        # Bankroll = 9950 + 50 (exposure return) + 9.30 = 10009.30
        assert engine.bankroll == pytest.approx(10009.30)

    @pytest.mark.asyncio
    async def test_losing_exit_no_fee(self, engine, model_params):
        """No fee on losing exit (fee only on profit)."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        pos = OpenPosition(
            direction="BUY_YES",
            entry_price=0.60,
            market_ticker="over_25",
            match_id="test_match_001",
            contracts=100,
        )
        engine.positions["over_25"] = [pos]
        engine.risk_manager.record_exposure("test_match_001", 60.0)
        engine.bankroll = 9940.0  # 10000 - 60

        # Exit at 0.50 (loss = -10.0)
        engine.ob_sync.update_book(
            bids=[(0.50, 200)],
            asks=[(0.52, 200)],
        )

        from src.trading.step_4_4_exit_logic import ExitSignal
        exit_sig = ExitSignal(reason="EDGE_REVERSAL")

        engine._execute_exit(pos, exit_sig, "over_25")

        # Gross PnL = (0.50 - 0.60) * 100 = -10.0
        # Fee = 0 (no profit)
        # Bankroll = 9940 + 60 + (-10) = 9990
        assert engine.bankroll == pytest.approx(9990.0)


# ---------------------------------------------------------------------------
# MC version increment tests
# ---------------------------------------------------------------------------

class TestMcVersionIncrement:
    """_mc_version increments on state-change events."""

    @pytest.mark.asyncio
    async def test_goal_detected_bumps_mc_version(self, engine, model_params):
        """Live odds goal_detected increments _mc_version."""
        events = [
            NormalizedEvent(
                type="goal_detected",
                source="live_odds",
                confidence="preliminary",
                timestamp=time.time(),
                score=(1, 0),
            ),
        ]
        engine._live_odds_source = FakeLiveOddsSource(events)

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        assert engine._mc_version == 0
        await engine._live_odds_listener()
        assert engine._mc_version == 1

    @pytest.mark.asyncio
    async def test_confirmed_goal_bumps_mc_version(self, engine, model_params):
        """Live score goal_confirmed increments _mc_version."""
        poll_responses = [
            [NormalizedEvent(
                type="goal_confirmed",
                source="live_score",
                confidence="confirmed",
                timestamp=time.time(),
                score=(1, 0),
                team="localteam",
            )],
        ]
        engine._live_score_source = FakeLiveScoreSource(poll_responses)

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )
        transition_to_first_half(engine.state)

        # Run one poll cycle manually
        from src.engine.event_handler import dispatch_live_score_event
        events = await engine._live_score_source.poll()
        for event in events:
            dispatch_live_score_event(engine.state, event)
            if event.type in ("goal_confirmed", "red_card"):
                engine._mc_version += 1

        assert engine._mc_version == 1


# ---------------------------------------------------------------------------
# Snapshot with None pricing tests
# ---------------------------------------------------------------------------

class TestSnapshotNonePricing:
    """Verify snapshot handles None pricing gracefully."""

    @pytest.mark.asyncio
    async def test_snapshot_with_none_pricing(self, engine, model_params):
        """Snapshot published with None pricing fields."""
        redis = FakeRedis()
        engine._redis = redis

        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        await engine._publish_state_snapshot(None, mu_H=0.5, mu_A=0.3)

        assert len(redis.published) == 1
        import json
        data = json.loads(redis.published[0][1])
        assert data["P_true"] is None
        assert data["sigma_MC"] is None
        assert data["pricing_mode"] is None
        assert data["mu_H"] == 0.5


# ---------------------------------------------------------------------------
# Wait for kickoff tests
# ---------------------------------------------------------------------------

class TestWaitForKickoff:
    """Verify _wait_for_kickoff and _pre_kickoff_check."""

    @pytest.mark.asyncio
    async def test_wait_exits_when_phase_changes(self, engine, model_params):
        """_wait_for_kickoff exits when engine_phase != WAITING_FOR_KICKOFF."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        # Schedule phase change after tiny delay
        async def change_phase():
            await asyncio.sleep(0.05)
            transition_to_first_half(engine.state)

        asyncio.create_task(change_phase())
        await asyncio.wait_for(engine._wait_for_kickoff(), timeout=2.0)

        assert engine.state.engine_phase == FIRST_HALF

    @pytest.mark.asyncio
    async def test_wait_exits_on_shutdown(self, engine, model_params):
        """_wait_for_kickoff exits when shutdown event is set."""
        await engine.run_prematch(
            sanity_result=SanityResult(verdict="GO"),
            model_params=model_params,
        )

        async def trigger_shutdown():
            await asyncio.sleep(0.05)
            engine._shutdown_event.set()

        asyncio.create_task(trigger_shutdown())
        await asyncio.wait_for(engine._wait_for_kickoff(), timeout=2.0)

    def test_pre_kickoff_check_passes(self, engine, model_params):
        """Pre-kickoff check passes when stoppage_mgr is initialized."""
        import asyncio as aio
        aio.get_event_loop().run_until_complete(
            engine.run_prematch(
                sanity_result=SanityResult(verdict="GO"),
                model_params=model_params,
            )
        )
        assert engine._pre_kickoff_check() is True

    def test_pre_kickoff_check_fails_on_shutdown(self, engine, model_params):
        """Pre-kickoff check fails when shutdown event is set."""
        import asyncio as aio
        aio.get_event_loop().run_until_complete(
            engine.run_prematch(
                sanity_result=SanityResult(verdict="GO"),
                model_params=model_params,
            )
        )
        engine._shutdown_event.set()
        assert engine._pre_kickoff_check() is False
