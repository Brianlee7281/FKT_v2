"""Step 5.1: Match Engine — Per-Match Lifecycle Orchestrator.

One instance per match. Manages the full Phase 2→4 pipeline:
  - Prematch: data collection, feature mask, sanity check, model init
  - Live: 3 concurrent coroutines (tick, live_odds, live_score)
  - Trading cycle: signal → sizing → risk → execution + exit evaluation
  - State snapshots → Redis for dashboard/alerts

Lifecycle: SPAWNED → PREMATCH → PREMATCH_READY → LIVE → FINISHED → SHUTDOWN

Reference: docs/blueprint.md → MatchEngine, docs/implementation_roadmap.md → Step 5.1
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from src.common.config import SystemConfig
from src.common.logging import get_logger
from src.common.types import NormalizedEvent, SanityResult, Signal, TradeLog
from src.prematch.step_2_5_initialization import load_phase1_params, normalize_Q_off
from src.engine.event_handler import (
    dispatch_live_odds_event,
    dispatch_live_score_event,
)
from src.engine.state_machine import (
    FINISHED,
    FIRST_HALF,
    HALFTIME,
    SECOND_HALF,
    WAITING_FOR_KICKOFF,
    EngineState,
    check_cooldown_release,
    check_ob_freeze_release,
    record_stable_tick,
    record_unstable_tick,
    transition_to_first_half,
)
from src.engine.step_3_4_pricing import PricingResult, price_hybrid_async
from src.engine.step_3_5_stoppage import StoppageTimeManager
from src.kalshi.execution import PaperExecutionLayer, PaperFill
from src.kalshi.orderbook import OrderBookSync
from src.trading.risk_manager import RiskManager
from src.trading.step_4_2_edge_detection import generate_signal
from src.trading.step_4_3_position_sizing import compute_kelly
from src.trading.step_4_4_exit_logic import ExitSignal, OpenPosition, evaluate_exit

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Engine lifecycle states
# ---------------------------------------------------------------------------

class EngineLifecycle(str, Enum):
    SPAWNED = "SPAWNED"
    PREMATCH = "PREMATCH"
    PREMATCH_READY = "PREMATCH_READY"
    LIVE = "LIVE"
    FINISHED = "FINISHED"
    SHUTDOWN = "SHUTDOWN"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Model parameters container (populated during prematch)
# ---------------------------------------------------------------------------

@dataclass
class ModelParams:
    """Holds Phase 1/2 model parameters needed by the live engine."""

    a_H: float = 0.0
    a_A: float = 0.0
    b: np.ndarray = field(default_factory=lambda: np.zeros(6))
    gamma_H: np.ndarray = field(default_factory=lambda: np.zeros(4))
    gamma_A: np.ndarray = field(default_factory=lambda: np.zeros(4))
    delta_H: np.ndarray = field(default_factory=lambda: np.zeros(5))
    delta_A: np.ndarray = field(default_factory=lambda: np.zeros(5))
    Q_diag: np.ndarray = field(default_factory=lambda: np.zeros(4))
    Q_off: np.ndarray = field(default_factory=lambda: np.zeros(4))
    basis_bounds: np.ndarray = field(
        default_factory=lambda: np.array([0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0])
    )
    # Precomputed transition probability matrices (Phase 2)
    # P_grid[dt_round] = 4x4 matrix for dt_round minutes, dt_round in [0,100]
    P_grid: np.ndarray | None = None
    # P_fine_grid[dt_10sec] = 4x4 matrix for dt_10sec*10 seconds, dt_10sec in [0,30]
    P_fine_grid: np.ndarray | None = None
    T_exp: float = 93.0
    DELTA_SIGNIFICANT: bool = False


# ---------------------------------------------------------------------------
# Match Engine
# ---------------------------------------------------------------------------

class MatchEngine:
    """One instance per match. Manages the full Phase 2→4 pipeline.

    Attributes:
        match_id: Unique match identifier.
        config: System configuration.
        lifecycle: Current engine lifecycle state.
        state: EngineState (phase + event state machine).
        model: ModelParams (populated during prematch).
        ob_sync: OrderBookSync for Kalshi + bet365 data.
        risk_manager: 3-layer risk limit enforcer.
        execution: PaperExecutionLayer for order simulation.
        positions: Open positions by market ticker.
        bankroll: Current bankroll in dollars.
        stoppage_mgr: StoppageTimeManager for T_exp adjustment.
    """

    def __init__(
        self,
        match_id: str,
        config: SystemConfig,
        *,
        risk_manager: RiskManager | None = None,
        redis_client=None,
        live_odds_source=None,
        live_score_source=None,
    ):
        self.match_id = match_id
        self.config = config
        self.lifecycle = EngineLifecycle.SPAWNED

        # State machine
        self.state = EngineState()

        # Model params (populated by run_prematch)
        self.model = ModelParams()

        # Order book
        self.ob_sync = OrderBookSync()

        # Risk management (shared across engines for Layer 3)
        self.risk_manager = risk_manager or RiskManager(
            f_order_cap=config.f_order_cap,
            f_match_cap=config.f_match_cap,
            f_total_cap=config.f_total_cap,
        )

        # Execution layer
        self.execution = PaperExecutionLayer(slippage_ticks=1)

        # Open positions: market_ticker -> list[OpenPosition]
        self.positions: dict[str, list[OpenPosition]] = {}

        # Bankroll
        self.bankroll = config.initial_bankroll

        # Stoppage time manager (initialized in prematch)
        self.stoppage_mgr: StoppageTimeManager | None = None

        # Infrastructure (injected for testability)
        self._redis = redis_client
        self._live_odds_source = live_odds_source
        self._live_score_source = live_score_source

        # Timing
        self._last_tick_time: float = 0.0
        self._mc_version: int = 0

        # Trade log
        self.trade_log: list[TradeLog] = []

        # Shutdown event for coordinating coroutine teardown
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Phase 2: Prematch
    # ------------------------------------------------------------------

    async def run_prematch(self, sanity_result: SanityResult | None = None,
                           model_params: ModelParams | None = None) -> SanityResult:
        """Phase 2: Pre-match initialization.

        Loads Phase 1 calibrated parameters from disk and populates ModelParams.
        For testability, sanity_result and model_params can be injected directly.

        Args:
            sanity_result: Pre-computed sanity check result (test injection).
            model_params: Pre-computed model params (test injection).

        Returns:
            SanityResult with verdict (GO/GO_WITH_CAUTION/HOLD/SKIP).
        """
        self.lifecycle = EngineLifecycle.PREMATCH

        if sanity_result is None:
            sanity_result = SanityResult(verdict="GO")

        if model_params is not None:
            self.model = model_params
        else:
            # Load Phase 1 params from disk
            try:
                phase1 = load_phase1_params(self.config.params_dir)
                Q = phase1["Q"]
                Q_off_norm = normalize_Q_off(Q)

                # Use league-average a as fallback (XGBoost not wired yet)
                self.model = ModelParams(
                    a_H=-3.5,
                    a_A=-3.5,
                    b=phase1["b"],
                    gamma_H=phase1["gamma_H"],
                    gamma_A=phase1["gamma_A"],
                    delta_H=phase1["delta_H"],
                    delta_A=phase1["delta_A"],
                    Q_diag=np.diag(Q).copy(),
                    Q_off=Q_off_norm,
                    DELTA_SIGNIFICANT=phase1["delta_significant"],
                )

                log.info(
                    "phase1_params_loaded",
                    match_id=self.match_id,
                    params_dir=self.config.params_dir,
                    b=self.model.b.tolist(),
                    delta_significant=self.model.DELTA_SIGNIFICANT,
                )
            except FileNotFoundError:
                log.error(
                    "phase1_params_not_found",
                    match_id=self.match_id,
                    params_dir=self.config.params_dir,
                )
                sanity_result = SanityResult(
                    verdict="SKIP",
                    warning=f"Phase 1 params not found at {self.config.params_dir}",
                )

        if sanity_result.verdict in ("GO", "GO_WITH_CAUTION"):
            self.stoppage_mgr = StoppageTimeManager(T_exp=self.model.T_exp)
            self.lifecycle = EngineLifecycle.PREMATCH_READY
            log.info(
                "prematch_ready",
                match_id=self.match_id,
                verdict=sanity_result.verdict,
                T_exp=self.model.T_exp,
            )
        else:
            self.lifecycle = EngineLifecycle.SKIPPED
            log.info(
                "prematch_skipped",
                match_id=self.match_id,
                verdict=sanity_result.verdict,
                warning=sanity_result.warning,
            )

        return sanity_result

    # ------------------------------------------------------------------
    # Phase 3+4: Live Trading
    # ------------------------------------------------------------------

    async def run_live(self) -> None:
        """Phase 3+4: Live trading — 3 concurrent coroutines.

        Sequence: wait_for_kickoff → pre_kickoff_check → 3 coroutines.
        Runs until match finishes or emergency shutdown.
        """
        if self.lifecycle != EngineLifecycle.PREMATCH_READY:
            log.warning(
                "run_live_not_ready",
                lifecycle=self.lifecycle.value,
            )
            return

        # Wait for kickoff (blocks until engine_phase leaves WAITING_FOR_KICKOFF)
        await self._wait_for_kickoff()

        # Final pre-kickoff check
        if not self._pre_kickoff_check():
            self.lifecycle = EngineLifecycle.SKIPPED
            log.info("pre_kickoff_check_failed", match_id=self.match_id)
            return

        self.lifecycle = EngineLifecycle.LIVE
        log.info("engine_live", match_id=self.match_id)

        # Warn if data sources are missing
        if self._live_odds_source is None:
            log.warning("live_odds_source_missing", match_id=self.match_id)
        if self._live_score_source is None:
            log.warning("live_score_source_missing", match_id=self.match_id)

        try:
            await asyncio.gather(
                self._tick_loop(),
                self._live_odds_listener(),
                self._live_score_poller(),
            )
        except Exception as e:
            log.error("engine_crash", match_id=self.match_id, error=str(e))
            await self._emergency_shutdown(e)
        finally:
            self.lifecycle = EngineLifecycle.FINISHED
            log.info("engine_finished", match_id=self.match_id)

    async def _wait_for_kickoff(self) -> None:
        """Block until engine_phase transitions away from WAITING_FOR_KICKOFF.

        In production, the live_odds_listener detects a period_change event
        and transitions the state. This method polls at 1s intervals.
        For tests, the state can be pre-set to FIRST_HALF before calling run_live.
        """
        while (self.state.engine_phase == WAITING_FOR_KICKOFF
               and not self._shutdown_event.is_set()):
            await asyncio.sleep(1)

    def _pre_kickoff_check(self) -> bool:
        """Final sanity check before going live.

        Returns True if the engine should proceed to LIVE state.
        Checks that critical components are initialized.
        """
        if self._shutdown_event.is_set():
            return False
        if self.stoppage_mgr is None:
            log.error("pre_kickoff_no_stoppage_mgr", match_id=self.match_id)
            return False
        return True

    # ------------------------------------------------------------------
    # Coroutine 1: Tick Loop (1s)
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """Every-1s tick: time advance → μ → pricing → trading → snapshot."""
        while self.state.engine_phase != FINISHED:
            if self._shutdown_event.is_set():
                break

            self._last_tick_time = time.time()

            # Release cooldown/freeze if expired
            check_cooldown_release(self.state)
            check_ob_freeze_release(self.state)

            # Track ob_freeze stabilization (3-tick release path)
            if self.state.ob_freeze:
                # TODO: compare current vs previous best_ask for instability
                # For now, count all ticks during ob_freeze as stable
                # (unstable ticks are recorded by the live_odds_listener
                # when odds_spike events arrive)
                record_stable_tick(self.state)

            if self.state.pricing_active:
                # Advance match time by 1 second = 1/60 minute
                self.state.current_time += 1 / 60

                # Step 3.2: remaining expected goals
                mu_H, mu_A = self._compute_remaining_mu()

                # Step 3.4: hybrid pricing
                pricing = await self._price(mu_H, mu_A)

                if pricing is not None:
                    # Step 4: trading cycle
                    await self._execute_trading_cycle(pricing)

                # Publish state snapshot every tick (even if pricing is None)
                await self._publish_state_snapshot(pricing, mu_H, mu_A)

            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Coroutine 2: Live Odds Listener
    # ------------------------------------------------------------------

    async def _live_odds_listener(self) -> None:
        """Listen to Live Odds WebSocket for preliminary event detection."""
        if self._live_odds_source is None:
            return

        try:
            async for event in self._live_odds_source:
                if self._shutdown_event.is_set():
                    break

                dispatch_live_odds_event(self.state, event)

                # Bump MC version on state-changing events
                if event.type in ("goal_detected", "score_rollback"):
                    self._mc_version += 1

                # Reset stable tick counter on odds_spike
                if event.type == "odds_spike":
                    record_unstable_tick(self.state)

                # Update stoppage time if minute data available
                if (self.stoppage_mgr is not None
                        and event.minute is not None
                        and event.period is not None):
                    self.stoppage_mgr.update_from_live_odds(
                        event.minute, event.period
                    )

                # Update bet365 odds if provided
                if event.extra.get("bet365_data"):
                    self.ob_sync.update_bet365(event.extra["bet365_data"])

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("live_odds_listener_error", error=str(e))

    # ------------------------------------------------------------------
    # Coroutine 3: Live Score Poller
    # ------------------------------------------------------------------

    async def _live_score_poller(self) -> None:
        """Poll Live Score REST every 3s for authoritative confirmation."""
        if self._live_score_source is None:
            return

        consecutive_failures = 0
        poll_interval = self.config.live_score_poll_interval

        try:
            while (self.state.engine_phase != FINISHED
                   and not self._shutdown_event.is_set()):
                try:
                    events = await self._live_score_source.poll()
                    consecutive_failures = 0

                    for event in events:
                        dispatch_live_score_event(self.state, event)

                        # Bump MC version on confirmed state changes
                        if event.type in ("goal_confirmed", "red_card"):
                            self._mc_version += 1

                        # Update stoppage time
                        if (self.stoppage_mgr is not None
                                and event.minute is not None
                                and event.period is not None):
                            self.stoppage_mgr.update_from_live_score(
                                event.minute, event.period
                            )

                except Exception as e:
                    consecutive_failures += 1
                    log.warning(
                        "live_score_poll_error",
                        error=str(e),
                        consecutive=consecutive_failures,
                    )

                    # 5 consecutive failures → emergency ob_freeze
                    if consecutive_failures >= 5:
                        from src.engine.event_handler import (
                            handle_live_score_failure,
                        )
                        handle_live_score_failure(self.state)
                        consecutive_failures = 0

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Step 3.2: Remaining Expected Goals
    # ------------------------------------------------------------------

    def _compute_remaining_mu(self) -> tuple[float, float]:
        """Compute remaining expected goals μ_H, μ_A.

        Markov-modulated integral formula (phase3.md Step 3.2):

        μ_T(t,T) = Σ_ℓ Σ_{j=0}^{3} P̄_{X(t),j}^{(ℓ)} ·
                    exp(a_T + b_{iℓ} + γ^T_j + δ_T(ΔS)) · Δτ_ℓ

        Where P̄ is the average transition probability from current state X
        to state j over subinterval ℓ, looked up from precomputed P_grid.

        When P_grid is unavailable (no Markov modulation), falls back to
        using the current X state directly (equivalent when X=0, 11v11).
        """
        t = self.state.current_time
        T = self.stoppage_mgr.current_T if self.stoppage_mgr else self.model.T_exp
        m = self.model

        if t >= T:
            return 0.0, 0.0

        # Clamp delta_S index to [0, 4] for the 5-element delta arrays
        di = max(0, min(4, self.state.delta_S + 2))

        X = self.state.X
        bounds = m.basis_bounds
        has_P_grid = m.P_grid is not None

        mu_H = 0.0
        mu_A = 0.0

        # Iterate over basis-function subintervals [τ_i, τ_{i+1}]
        for i in range(len(bounds) - 1):
            seg_start = bounds[i]
            seg_end = bounds[i + 1]

            # Clip to [t, T]
            seg_start = max(seg_start, t)
            seg_end = min(seg_end, T)

            if seg_start >= seg_end:
                continue

            dt = seg_end - seg_start

            # b_i: basis function coefficient for this interval
            bi = m.b[i] if i < len(m.b) else 0.0

            if has_P_grid:
                # Markov-modulated: sum over all 4 states weighted by
                # transition probabilities P_bar[X, j] for this subinterval
                P_trans = self._get_transition_prob(dt)
                for j in range(4):
                    weight = P_trans[X, j]
                    if weight < 1e-12:
                        continue
                    lam_H = np.exp(m.a_H + bi + m.gamma_H[j] + m.delta_H[di])
                    lam_A = np.exp(m.a_A + bi + m.gamma_A[j] + m.delta_A[di])
                    mu_H += weight * lam_H * dt
                    mu_A += weight * lam_A * dt
            else:
                # Fallback: use current X directly (exact when X=0)
                lambda_H = np.exp(m.a_H + bi + m.gamma_H[X] + m.delta_H[di])
                lambda_A = np.exp(m.a_A + bi + m.gamma_A[X] + m.delta_A[di])
                mu_H += lambda_H * dt
                mu_A += lambda_A * dt

        return float(mu_H), float(mu_A)

    def _get_transition_prob(self, dt_min: float) -> np.ndarray:
        """Lookup transition probability matrix from P_grid or P_fine_grid.

        Uses fine grid (10-second increments) near match end (dt <= 5 min),
        standard grid (1-minute increments) otherwise.

        Args:
            dt_min: Time interval in minutes.

        Returns:
            4x4 transition probability matrix.
        """
        m = self.model
        if dt_min <= 5 and m.P_fine_grid is not None:
            dt_10sec = int(round(dt_min * 6))
            dt_10sec = max(0, min(len(m.P_fine_grid) - 1, dt_10sec))
            return m.P_fine_grid[dt_10sec]
        else:
            dt_round = max(0, min(len(m.P_grid) - 1, round(dt_min)))
            return m.P_grid[int(dt_round)]

    # ------------------------------------------------------------------
    # Step 3.4: Pricing
    # ------------------------------------------------------------------

    async def _price(self, mu_H: float, mu_A: float) -> PricingResult | None:
        """Run hybrid pricing (analytic or MC)."""
        m = self.model
        T = self.stoppage_mgr.current_T if self.stoppage_mgr else m.T_exp

        return await price_hybrid_async(
            mu_H=mu_H,
            mu_A=mu_A,
            current_score=self.state.score,
            X=self.state.X,
            delta_S=self.state.delta_S,
            delta_significant=m.DELTA_SIGNIFICANT,
            t_now=self.state.current_time,
            T_end=T,
            a_H=m.a_H,
            a_A=m.a_A,
            b=m.b,
            gamma_H=m.gamma_H,
            gamma_A=m.gamma_A,
            delta_H=m.delta_H,
            delta_A=m.delta_A,
            Q_diag=m.Q_diag,
            Q_off=m.Q_off,
            basis_bounds=m.basis_bounds,
            match_id=self.match_id,
            mc_version=self._mc_version,
        )

    # ------------------------------------------------------------------
    # Phase 4: Trading Cycle
    # ------------------------------------------------------------------

    async def _execute_trading_cycle(self, pricing: PricingResult) -> None:
        """Phase 4: signal → sizing → risk → order + exit evaluation.

        Called every tick when pricing is available.
        """
        P_true = pricing.P_true
        sigma_MC = pricing.sigma_MC

        for market in self.config.active_markets:
            p_true_market = P_true.get(market)
            if p_true_market is None:
                continue

            P_bet365 = self.ob_sync.bet365_implied.get(market)

            # Step 4.2: 2-pass VWAP signal generation
            signal = generate_signal(
                P_true=p_true_market,
                sigma_MC=sigma_MC,
                ob_sync=self.ob_sync,
                P_bet365=P_bet365,
                c=self.config.fee_rate,
                z=self.config.z,
                K_frac=self.config.K_frac,
                bankroll=self.bankroll,
                market_ticker=market,
                theta_entry=self.config.theta_entry,
            )

            # Entry: only if order_allowed and signal is actionable
            if signal.direction != "HOLD" and self.state.order_allowed:
                # Step 4.3: Kelly sizing
                f = compute_kelly(signal, self.config.fee_rate, self.config.K_frac)

                # Risk limits (3-layer)
                amount = self.risk_manager.apply_risk_limits(
                    f, self.match_id, self.bankroll
                )

                if amount > 0:
                    # Step 4.5: execution
                    fill = self.execution.execute_order(
                        signal, amount, self.ob_sync
                    )
                    if fill is not None:
                        self._record_fill(signal, fill, pricing, f_kelly=f)

            # Step 4.4: exit evaluation for open positions
            T = (self.stoppage_mgr.current_T
                 if self.stoppage_mgr else self.model.T_exp)

            P_kalshi_bid = self.ob_sync.kalshi_best_bid or 0.0

            for pos in self.positions.get(market, []):
                exit_signal = evaluate_exit(
                    position=pos,
                    P_true=p_true_market,
                    sigma_MC=sigma_MC,
                    P_kalshi_bid=P_kalshi_bid,
                    P_bet365=P_bet365,
                    c=self.config.fee_rate,
                    z=self.config.z,
                    t=self.state.current_time,
                    T=T,
                    bet365_divergence_auto_exit=(
                        self.config.bet365_divergence_auto_exit
                    ),
                )
                if exit_signal is not None:
                    self._execute_exit(pos, exit_signal, market)

    # ------------------------------------------------------------------
    # Fill & Exit Recording
    # ------------------------------------------------------------------

    def _record_fill(
        self, signal: Signal, fill: PaperFill, pricing: PricingResult,
        *, f_kelly: float = 0.0,
    ) -> None:
        """Record a successful fill: update positions, exposure, bankroll."""
        # Create open position
        pos = OpenPosition(
            direction=signal.direction,
            entry_price=fill.price,
            market_ticker=signal.market_ticker,
            match_id=self.match_id,
            contracts=fill.quantity,
        )

        market = signal.market_ticker
        if market not in self.positions:
            self.positions[market] = []
        self.positions[market].append(pos)

        # Record exposure
        exposure_amount = fill.price * fill.quantity
        self.risk_manager.record_exposure(self.match_id, exposure_amount)

        # Debit bankroll
        self.bankroll -= exposure_amount

        # Trade log
        trade = TradeLog(
            timestamp=time.time(),
            match_id=self.match_id,
            market_ticker=signal.market_ticker,
            direction=signal.direction,
            order_type="LIMIT",
            quantity_ordered=fill.target_quantity,
            quantity_filled=fill.quantity,
            limit_price=signal.P_kalshi,
            fill_price=fill.price,
            P_true_at_order=signal.P_cons,
            P_true_cons_at_order=signal.P_cons,
            P_kalshi_at_order=signal.P_kalshi,
            P_kalshi_best_at_order=self.ob_sync.kalshi_best_ask or 0.0,
            EV_adj=signal.EV,
            sigma_MC=pricing.sigma_MC,
            pricing_mode=pricing.pricing_mode,
            f_kelly=f_kelly,
            K_frac=self.config.K_frac,
            alignment_status=signal.alignment_status,
            kelly_multiplier=signal.kelly_multiplier,
            cooldown_active=self.state.cooldown,
            ob_freeze_active=self.state.ob_freeze,
            event_state=self.state.event_state,
            engine_phase=self.state.engine_phase,
            bankroll_before=self.bankroll + exposure_amount,
            bankroll_after=self.bankroll,
            is_paper=True,
            paper_slippage=fill.slippage,
        )
        self.trade_log.append(trade)

        log.info(
            "trade_filled",
            match_id=self.match_id,
            market=market,
            direction=signal.direction,
            qty=fill.quantity,
            price=fill.price,
        )

    def _execute_exit(
        self, pos: OpenPosition, exit_signal: ExitSignal, market: str
    ) -> None:
        """Close a position and update bookkeeping."""
        # Remove from positions
        if market in self.positions:
            try:
                self.positions[market].remove(pos)
            except ValueError:
                pass
            if not self.positions[market]:
                del self.positions[market]

        # Remove exposure
        exposure_amount = pos.entry_price * pos.contracts
        self.risk_manager.remove_exposure(self.match_id, exposure_amount)

        # Credit bankroll at current best bid, with fee deduction
        exit_price = self.ob_sync.kalshi_best_bid or pos.entry_price
        c = self.config.fee_rate

        if pos.direction == "BUY_YES":
            gross_pnl = (exit_price - pos.entry_price) * pos.contracts
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.contracts

        # Fee applies only on profit (per Phase 4 spec)
        fee = c * max(0.0, gross_pnl)
        pnl = gross_pnl - fee

        self.bankroll += exposure_amount + pnl

        log.info(
            "position_exited",
            match_id=self.match_id,
            market=market,
            reason=exit_signal.reason,
            pnl=pnl,
            contracts=pos.contracts,
        )

    # ------------------------------------------------------------------
    # State Snapshot → Redis
    # ------------------------------------------------------------------

    async def _publish_state_snapshot(
        self, pricing: PricingResult | None, mu_H: float, mu_A: float
    ) -> None:
        """Publish state snapshot to Redis for dashboard + alert service."""
        if self._redis is None:
            return

        snapshot = {
            "match_id": self.match_id,
            "timestamp": time.time(),
            "t": self.state.current_time,
            "score": list(self.state.score),
            "X": self.state.X,
            "delta_S": self.state.delta_S,
            "mu_H": mu_H,
            "mu_A": mu_A,
            "P_true": pricing.P_true if pricing else None,
            "sigma_MC": pricing.sigma_MC if pricing else None,
            "pricing_mode": pricing.pricing_mode if pricing else None,
            "engine_phase": self.state.engine_phase,
            "event_state": self.state.event_state,
            "cooldown": self.state.cooldown,
            "ob_freeze": self.state.ob_freeze,
            "order_allowed": self.state.order_allowed,
            "P_bet365": self.ob_sync.bet365_implied,
            "P_kalshi_bid": self.ob_sync.kalshi_best_bid,
            "P_kalshi_ask": self.ob_sync.kalshi_best_ask,
            "positions": {
                mkt: [
                    {"direction": p.direction, "entry_price": p.entry_price,
                     "contracts": p.contracts}
                    for p in pos_list
                ]
                for mkt, pos_list in self.positions.items()
            },
            "bankroll": self.bankroll,
            "lifecycle": self.lifecycle.value,
        }

        try:
            await self._redis.publish(
                f"match:{self.match_id}:state",
                json.dumps(snapshot, default=str),
            )
        except Exception as e:
            log.warning("redis_publish_error", error=str(e))

    # ------------------------------------------------------------------
    # Emergency Shutdown
    # ------------------------------------------------------------------

    async def _emergency_shutdown(self, error: Exception) -> None:
        """Safe handling on abnormal termination.

        1. Signal all coroutines to stop
        2. Cancel all unfilled orders
        3. Publish critical alert
        4. Persist crash state
        """
        self._shutdown_event.set()

        # Step 1: Cancel unfilled orders (if execution layer supports it)
        if hasattr(self.execution, "cancel_all_orders"):
            try:
                self.execution.cancel_all_orders()
                log.info("emergency_orders_cancelled", match_id=self.match_id)
            except Exception as cancel_err:
                log.error("cancel_orders_failed", error=str(cancel_err))

        # Step 2: Publish critical alert via Redis
        if self._redis is not None:
            try:
                await self._redis.publish(
                    "alerts",
                    json.dumps({
                        "severity": "CRITICAL",
                        "title": f"Engine Crash: {self.match_id}",
                        "body": str(error),
                        "timestamp": time.time(),
                    }),
                )
            except Exception:
                pass

        # Step 3: Persist crash state (trade log captures last known state)
        self._crash_error = str(error)
        self._crash_timestamp = time.time()

        log.error(
            "emergency_shutdown",
            match_id=self.match_id,
            error=str(error),
            open_positions=sum(
                len(v) for v in self.positions.values()
            ),
            bankroll=self.bankroll,
        )

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    def is_finished(self) -> bool:
        return self.lifecycle in (
            EngineLifecycle.FINISHED,
            EngineLifecycle.SHUTDOWN,
        )

    def is_healthy(self) -> bool:
        if self.lifecycle != EngineLifecycle.LIVE:
            return True
        return (time.time() - self._last_tick_time) < 5

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_event.set()
        self.lifecycle = EngineLifecycle.SHUTDOWN
