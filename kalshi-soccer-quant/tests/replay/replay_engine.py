"""Replay Engine — Primary debugging tool for Phase 3.

Replays a historical match through the full Phase 3 pipeline
using stored events, producing a time series of snapshots
with P_true, state transitions, and pricing mode.

Usage:
    engine = ReplayEngine(model_params)
    snapshots = await engine.replay(events)

Reference: implementation_roadmap.md → Step 3.5
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from src.common.types import NormalizedEvent
from src.engine.event_handler import (
    dispatch_live_odds_event,
    dispatch_live_score_event,
)
from src.engine.mc_core import mc_simulate_remaining
from src.engine.state_machine import (
    FIRST_HALF,
    FINISHED,
    HALFTIME,
    IDLE,
    SECOND_HALF,
    EngineState,
    check_cooldown_release,
    check_ob_freeze_release,
    transition_to_first_half,
)
from src.engine.step_3_4_pricing import (
    PricingResult,
    aggregate_markets,
    analytical_pricing,
    price_analytical,
    price_from_mc,
)
from src.engine.step_3_5_stoppage import StoppageTimeManager


# ---------------------------------------------------------------------------
# Snapshot — recorded state at a point in time
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """Captured engine state at a point in time during replay."""

    # Time
    match_minute: float = 0.0
    wall_time: float = 0.0

    # Match state
    score: tuple[int, int] = (0, 0)
    delta_S: int = 0
    X: int = 0

    # Engine state
    engine_phase: str = "IDLE"
    event_state: str = "IDLE"
    cooldown: bool = False
    ob_freeze: bool = False
    order_allowed: bool = True

    # Pricing
    P_true: dict[str, float] = field(default_factory=dict)
    sigma_MC: float = 0.0
    pricing_mode: str = "analytical"

    # Remaining expected goals
    mu_H: float = 0.0
    mu_A: float = 0.0

    # T value
    T_end: float = 98.0

    # Trigger event (if any)
    trigger_event: str | None = None
    trigger_detail: str | None = None


# ---------------------------------------------------------------------------
# Model parameters for replay (simplified LiveModelInstance)
# ---------------------------------------------------------------------------

@dataclass
class ReplayModelParams:
    """Parameters needed for replay (subset of LiveModelInstance)."""

    a_H: float = -3.5
    a_A: float = -3.5
    b: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float64))
    gamma_H: np.ndarray = field(
        default_factory=lambda: np.zeros(4, dtype=np.float64)
    )
    gamma_A: np.ndarray = field(
        default_factory=lambda: np.zeros(4, dtype=np.float64)
    )
    delta_H: np.ndarray = field(
        default_factory=lambda: np.zeros(5, dtype=np.float64)
    )
    delta_A: np.ndarray = field(
        default_factory=lambda: np.zeros(5, dtype=np.float64)
    )
    Q_diag: np.ndarray = field(
        default_factory=lambda: np.zeros(4, dtype=np.float64)
    )
    Q_off_normalized: np.ndarray = field(
        default_factory=lambda: np.zeros((4, 4), dtype=np.float64)
    )
    basis_bounds: np.ndarray = field(
        default_factory=lambda: np.array(
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0], dtype=np.float64
        )
    )
    T_exp: float = 98.0
    delta_significant: bool = False
    match_id: str = ""
    N_MC: int = 50_000


# ---------------------------------------------------------------------------
# Replay Engine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """Replays a historical match through Phase 3 using stored events.

    This is the primary debugging tool for the Phase 3 pipeline.
    Feed it a list of NormalizedEvents ordered by timestamp,
    and it will:
      1. Process each event through the state machine + event handlers
      2. Recompute pricing after each event and at regular intervals
      3. Record a Snapshot at each step

    Usage:
        params = ReplayModelParams(a_H=-3.2, a_A=-3.4, ...)
        events = [NormalizedEvent(...), ...]
        engine = ReplayEngine(params)
        snapshots = engine.replay(events)
    """

    def __init__(self, params: ReplayModelParams):
        self.params = params
        self.state = EngineState(engine_phase=FIRST_HALF)
        self.stoppage = StoppageTimeManager(T_exp=params.T_exp)
        self.snapshots: list[Snapshot] = []
        self._seed_counter = 0

    def replay(self, events: list[NormalizedEvent]) -> list[Snapshot]:
        """Replay a full match from a list of stored events.

        Args:
            events: Time-ordered list of NormalizedEvents.

        Returns:
            List of Snapshots recorded during replay.
        """
        self.snapshots = []

        # Record initial state
        self._record_snapshot(0.0, trigger="kickoff")

        for event in events:
            minute = event.minute or 0.0

            # Update stoppage time if minute is available
            if event.minute is not None and event.period:
                if event.source == "live_score":
                    self.stoppage.update_from_live_score(
                        event.minute, event.period
                    )
                else:
                    self.stoppage.update_from_live_odds(
                        event.minute, event.period
                    )

            # Dispatch event based on source
            if event.source == "live_odds":
                dispatch_live_odds_event(self.state, event)
            elif event.source == "live_score":
                dispatch_live_score_event(self.state, event)

            # Check ob_freeze and cooldown release
            check_ob_freeze_release(self.state)
            check_cooldown_release(self.state)

            # Compute pricing
            self._compute_pricing(minute)

            # Record snapshot
            trigger = f"{event.source}:{event.type}"
            detail = None
            if event.team:
                detail = event.team
            if event.score:
                detail = f"{detail or ''} score={event.score}"
            if event.var_cancelled:
                detail = f"{detail or ''} VAR_CANCELLED"

            self._record_snapshot(minute, trigger=trigger, detail=detail)

        return self.snapshots

    def replay_with_ticks(
        self,
        events: list[NormalizedEvent],
        tick_interval: float = 1.0,
        match_duration: float = 98.0,
    ) -> list[Snapshot]:
        """Replay with regular tick intervals between events.

        Simulates the real engine's 1-second tick loop by
        inserting pricing recalculations between events.

        Args:
            events: Time-ordered NormalizedEvents.
            tick_interval: Minutes between ticks (default 1.0).
            match_duration: Total match duration in minutes.

        Returns:
            List of Snapshots.
        """
        self.snapshots = []
        self._record_snapshot(0.0, trigger="kickoff")

        event_idx = 0
        t = 0.0

        while t <= match_duration:
            # Process all events at or before current time
            while (event_idx < len(events)
                   and events[event_idx].minute is not None
                   and events[event_idx].minute <= t):
                event = events[event_idx]

                if event.period:
                    if event.source == "live_score":
                        self.stoppage.update_from_live_score(
                            event.minute, event.period
                        )
                    else:
                        self.stoppage.update_from_live_odds(
                            event.minute, event.period
                        )

                if event.source == "live_odds":
                    dispatch_live_odds_event(self.state, event)
                elif event.source == "live_score":
                    dispatch_live_score_event(self.state, event)

                check_ob_freeze_release(self.state)
                check_cooldown_release(self.state)
                self._compute_pricing(event.minute)

                trigger = f"{event.source}:{event.type}"
                self._record_snapshot(event.minute, trigger=trigger)
                event_idx += 1

            # Regular tick
            if self.state.engine_phase in (FIRST_HALF, SECOND_HALF):
                check_ob_freeze_release(self.state)
                check_cooldown_release(self.state)
                self._compute_pricing(t)
                self._record_snapshot(t, trigger="tick")

            # Check if match is finished
            if self.state.engine_phase == FINISHED:
                break

            t += tick_interval

        return self.snapshots

    def _compute_pricing(self, minute: float) -> None:
        """Compute P_true at current state."""
        T_end = self.stoppage.current_T
        remaining = max(0.0, T_end - minute)

        if remaining <= 0:
            return

        p = self.params
        state = self.state

        # Use analytical or MC
        if (state.X == 0 and state.delta_S == 0
                and not p.delta_significant):
            # Integrate lambda over remaining time bins for accurate mu
            mu_H, mu_A = self._integrate_mu(minute, T_end)
            result = price_analytical(mu_H, mu_A, state.score)
            self._last_mu = (mu_H, mu_A)
            self._last_pricing = result
        else:
            # MC simulation (handles multi-bin integration internally)
            self._seed_counter += 1
            seed = hash((p.match_id, minute, state.score[0],
                         state.score[1], state.X,
                         self._seed_counter)) % (2**31)

            final_scores = mc_simulate_remaining(
                minute, T_end,
                state.score[0], state.score[1],
                state.X, state.delta_S,
                p.a_H, p.a_A, p.b,
                p.gamma_H, p.gamma_A,
                p.delta_H, p.delta_A,
                p.Q_diag, p.Q_off_normalized,
                p.basis_bounds, p.N_MC, seed,
            )

            mu_H = float(np.mean(final_scores[:, 0])) - state.score[0]
            mu_A = float(np.mean(final_scores[:, 1])) - state.score[1]
            result = price_from_mc(final_scores, state.score)
            self._last_mu = (mu_H, mu_A)
            self._last_pricing = result

    def _integrate_mu(self, t_now: float, T_end: float) -> tuple[float, float]:
        """Integrate lambda over remaining time bins for accurate mu.

        mu = sum over bins of lambda_bin * duration_in_bin
        where lambda_bin = exp(a + b[bi] + gamma[X] + delta[di]).
        """
        p = self.params
        state = self.state
        di = max(0, min(4, state.delta_S + 2))
        bounds = p.basis_bounds

        mu_H = 0.0
        mu_A = 0.0

        for k in range(6):
            bin_start = max(t_now, bounds[k])
            bin_end = min(T_end, bounds[k + 1])

            if bin_start >= bin_end:
                continue

            dt = bin_end - bin_start
            lam_H = np.exp(
                p.a_H + p.b[k] + p.gamma_H[state.X] + p.delta_H[di]
            )
            lam_A = np.exp(
                p.a_A + p.b[k] + p.gamma_A[state.X] + p.delta_A[di]
            )
            mu_H += lam_H * dt
            mu_A += lam_A * dt

        # Handle time beyond last bin boundary (stoppage time)
        if T_end > bounds[6]:
            extra_start = max(t_now, bounds[6])
            extra_dt = T_end - extra_start
            if extra_dt > 0:
                lam_H = np.exp(
                    p.a_H + p.b[5] + p.gamma_H[state.X] + p.delta_H[di]
                )
                lam_A = np.exp(
                    p.a_A + p.b[5] + p.gamma_A[state.X] + p.delta_A[di]
                )
                mu_H += lam_H * extra_dt
                mu_A += lam_A * extra_dt

        return mu_H, mu_A

    def _get_basis_index(self, minute: float) -> int:
        """Get time bin index for current minute."""
        bounds = self.params.basis_bounds
        for k in range(6):
            if minute >= bounds[k] and minute < bounds[k + 1]:
                return k
        return 5  # last bin

    def _record_snapshot(
        self,
        minute: float,
        trigger: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Record current state as a Snapshot."""
        pricing = getattr(self, "_last_pricing", PricingResult())
        mu = getattr(self, "_last_mu", (0.0, 0.0))

        snap = Snapshot(
            match_minute=minute,
            wall_time=time.time(),
            score=self.state.score,
            delta_S=self.state.delta_S,
            X=self.state.X,
            engine_phase=self.state.engine_phase,
            event_state=self.state.event_state,
            cooldown=self.state.cooldown,
            ob_freeze=self.state.ob_freeze,
            order_allowed=self.state.order_allowed,
            P_true=pricing.P_true,
            sigma_MC=pricing.sigma_MC,
            pricing_mode=pricing.pricing_mode,
            mu_H=mu[0],
            mu_A=mu[1],
            T_end=self.stoppage.current_T,
            trigger_event=trigger,
            trigger_detail=detail,
        )
        self.snapshots.append(snap)


# ---------------------------------------------------------------------------
# Event builder helpers (for constructing replay event lists)
# ---------------------------------------------------------------------------

def make_goal_event(
    minute: float,
    team: str,
    score: tuple[int, int],
    period: str = "1st Half",
) -> list[NormalizedEvent]:
    """Create a preliminary + confirmed goal event pair.

    Returns two events: preliminary (live_odds) then confirmed (live_score).
    """
    ts = minute * 60.0  # approximate timestamp

    preliminary = NormalizedEvent(
        type="goal_detected",
        source="live_odds",
        confidence="preliminary",
        timestamp=ts,
        score=score,
        minute=minute,
        period=period,
    )

    confirmed = NormalizedEvent(
        type="goal_confirmed",
        source="live_score",
        confidence="confirmed",
        timestamp=ts + 5.0,  # 5s delay
        score=score,
        team=team,
        minute=minute,
        period=period,
    )

    return [preliminary, confirmed]


def make_red_card_event(
    minute: float,
    team: str,
    period: str = "1st Half",
) -> NormalizedEvent:
    """Create a confirmed red card event."""
    return NormalizedEvent(
        type="red_card",
        source="live_score",
        confidence="confirmed",
        timestamp=minute * 60.0,
        team=team,
        minute=minute,
        period=period,
    )


def make_halftime_event(minute: float = 45.0) -> list[NormalizedEvent]:
    """Create halftime entry events (preliminary + confirmed)."""
    ts = minute * 60.0

    preliminary = NormalizedEvent(
        type="period_change",
        source="live_odds",
        confidence="preliminary",
        timestamp=ts,
        period="Paused",
        minute=minute,
    )

    confirmed = NormalizedEvent(
        type="period_change",
        source="live_score",
        confidence="confirmed",
        timestamp=ts + 5.0,
        period="Halftime",
        minute=minute,
    )

    return [preliminary, confirmed]


def make_second_half_event(minute: float = 45.0) -> NormalizedEvent:
    """Create second half start event."""
    return NormalizedEvent(
        type="period_change",
        source="live_odds",
        confidence="preliminary",
        timestamp=minute * 60.0,
        period="2nd Half",
        minute=minute,
    )


def make_match_finished_event(minute: float = 90.0) -> NormalizedEvent:
    """Create match finished event."""
    return NormalizedEvent(
        type="match_finished",
        source="live_score",
        confidence="confirmed",
        timestamp=minute * 60.0,
        minute=minute,
    )


def make_var_cancelled_event(
    minute: float,
    score: tuple[int, int],
    period: str = "1st Half",
) -> NormalizedEvent:
    """Create a VAR-cancelled goal confirmation."""
    return NormalizedEvent(
        type="goal_confirmed",
        source="live_score",
        confidence="confirmed",
        timestamp=minute * 60.0 + 5.0,
        score=score,
        var_cancelled=True,
        minute=minute,
        period=period,
    )
