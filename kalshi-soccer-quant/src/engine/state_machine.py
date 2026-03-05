"""Engine State Machine — Phase and Event State Management.

Manages two orthogonal state machines:
  1. Engine Phase: FIRST_HALF → HALFTIME → SECOND_HALF → FINISHED
  2. Event State: IDLE → PRELIMINARY_DETECTED → CONFIRMED/FALSE_ALARM → IDLE

Plus ob_freeze release logic (3-tick stabilization / 10s timeout).

Reference: phase3.md → Step 3.1 → State Machine
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from src.common.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Engine phases
WAITING_FOR_KICKOFF = "WAITING_FOR_KICKOFF"
FIRST_HALF = "FIRST_HALF"
HALFTIME = "HALFTIME"
SECOND_HALF = "SECOND_HALF"
FINISHED = "FINISHED"

# Event states
IDLE = "IDLE"
PRELIMINARY_DETECTED = "PRELIMINARY_DETECTED"
CONFIRMED = "CONFIRMED"
FALSE_ALARM = "FALSE_ALARM"
VAR_CANCELLED = "VAR_CANCELLED"

# Cooldown duration (seconds)
COOLDOWN_DURATION = 15

# ob_freeze release parameters
OB_FREEZE_STABLE_TICKS = 3
OB_FREEZE_TIMEOUT = 10.0  # seconds


# ---------------------------------------------------------------------------
# Engine State
# ---------------------------------------------------------------------------

@dataclass
class EngineState:
    """Mutable state for the live trading engine.

    Holds the current match state and control flags that event handlers
    modify during the match.
    """

    # Engine phase
    engine_phase: str = WAITING_FOR_KICKOFF

    # Match time (effective play minutes, halftime excluded)
    current_time: float = 0.0

    # Score state
    score: tuple[int, int] = (0, 0)     # (S_H, S_A)
    delta_S: int = 0                     # S_H - S_A

    # Markov state X ∈ {0, 1, 2, 3}
    X: int = 0

    # Event state machine
    event_state: str = IDLE

    # Control flags
    cooldown: bool = False
    ob_freeze: bool = False

    # ob_freeze tracking
    _ob_freeze_start: float = 0.0
    _ob_stable_ticks: int = 0

    # Cooldown tracking
    _cooldown_start: float = 0.0

    # Preliminary cache (for precomputed μ reuse)
    preliminary_cache: dict = field(default_factory=dict)

    @property
    def order_allowed(self) -> bool:
        """Orders are allowed only when all safety conditions pass."""
        return (
            not self.cooldown
            and not self.ob_freeze
            and self.event_state == IDLE
            and self.engine_phase in (FIRST_HALF, SECOND_HALF)
        )

    @property
    def pricing_active(self) -> bool:
        """Pricing runs during active play only."""
        return self.engine_phase in (FIRST_HALF, SECOND_HALF)


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

def transition_to_first_half(state: EngineState) -> None:
    """Kickoff detected — start first half."""
    state.engine_phase = FIRST_HALF
    state.current_time = 0.0
    log.info("engine_phase_transition", to=FIRST_HALF)


def transition_to_halftime(state: EngineState) -> None:
    """Halftime detected — freeze pricing and orders."""
    state.engine_phase = HALFTIME
    log.info("engine_phase_transition", to=HALFTIME)


def transition_to_second_half(state: EngineState) -> None:
    """Second half kickoff detected."""
    state.engine_phase = SECOND_HALF
    log.info("engine_phase_transition", to=SECOND_HALF)


def transition_to_finished(state: EngineState) -> None:
    """Full time — match ended."""
    state.engine_phase = FINISHED
    log.info("engine_phase_transition", to=FINISHED)


# ---------------------------------------------------------------------------
# Event state transitions
# ---------------------------------------------------------------------------

def set_preliminary(state: EngineState) -> None:
    """Score change detected (Live Odds) — enter preliminary state."""
    state.event_state = PRELIMINARY_DETECTED
    state.ob_freeze = True
    state._ob_freeze_start = time.time()
    state._ob_stable_ticks = 0


def set_confirmed(state: EngineState) -> None:
    """Event confirmed (Live Score) — commit state and enter cooldown."""
    state.event_state = IDLE
    state.ob_freeze = False
    state.cooldown = True
    state._cooldown_start = time.time()
    state.preliminary_cache = {}


def set_false_alarm(state: EngineState) -> None:
    """No confirmation received — return to idle."""
    state.event_state = IDLE
    state.ob_freeze = False
    state.preliminary_cache = {}
    log.info("event_false_alarm")


def set_var_cancelled(state: EngineState) -> None:
    """Goal VAR-cancelled — rollback to idle."""
    state.event_state = IDLE
    state.ob_freeze = False
    state.preliminary_cache = {}
    log.info("event_var_cancelled")


# ---------------------------------------------------------------------------
# Cooldown management
# ---------------------------------------------------------------------------

async def start_cooldown(state: EngineState, duration: int = COOLDOWN_DURATION) -> None:
    """Start cooldown timer — releases after duration seconds (async variant)."""
    state.cooldown = True
    state._cooldown_start = time.time()
    await asyncio.sleep(duration)
    state.cooldown = False
    log.info("cooldown_expired", duration=duration)


def check_cooldown_release(state: EngineState) -> None:
    """Check and release cooldown if duration has elapsed.

    Called every tick. This is the synchronous alternative to start_cooldown
    for use in non-async contexts (replay, tick loops).
    """
    if not state.cooldown:
        return

    elapsed = time.time() - state._cooldown_start
    if elapsed >= COOLDOWN_DURATION:
        state.cooldown = False
        log.info("cooldown_expired", duration=COOLDOWN_DURATION)


# ---------------------------------------------------------------------------
# ob_freeze release check
# ---------------------------------------------------------------------------

def check_ob_freeze_release(state: EngineState) -> None:
    """Check and release ob_freeze if conditions are met.

    Called every tick. Release if any condition is met:
      1. Cooldown has taken over (event was confirmed)
      2. 3 consecutive stable ticks (no further odds movement)
      3. 10-second timeout (false-positive protection)
    """
    if not state.ob_freeze:
        return

    # Condition 1: explained by event (cooldown takes over)
    if state.cooldown:
        state.ob_freeze = False
        return

    # Condition 2: 3-tick stabilization
    if state._ob_stable_ticks >= OB_FREEZE_STABLE_TICKS:
        state.ob_freeze = False
        state._ob_stable_ticks = 0
        state.event_state = IDLE
        state.preliminary_cache = {}
        log.info("ob_freeze_released", reason="3_tick_stabilization")
        return

    # Condition 3: 10-second timeout
    elapsed = time.time() - state._ob_freeze_start
    if elapsed >= OB_FREEZE_TIMEOUT:
        state.ob_freeze = False
        state.event_state = IDLE
        state.preliminary_cache = {}
        log.info("ob_freeze_released", reason="10s_timeout")


def record_stable_tick(state: EngineState) -> None:
    """Record a tick where odds did not move significantly."""
    if state.ob_freeze:
        state._ob_stable_ticks += 1


def record_unstable_tick(state: EngineState) -> None:
    """Reset stable tick counter on significant odds movement."""
    state._ob_stable_ticks = 0


# ---------------------------------------------------------------------------
# Score / Markov state updates
# ---------------------------------------------------------------------------

def commit_goal(state: EngineState, team: str) -> None:
    """Commit a confirmed goal to the state.

    Args:
        team: "localteam" or "visitorteam"
    """
    if team == "localteam":
        state.score = (state.score[0] + 1, state.score[1])
    else:
        state.score = (state.score[0], state.score[1] + 1)
    state.delta_S = state.score[0] - state.score[1]


def commit_red_card(state: EngineState, team: str) -> None:
    """Commit a confirmed red card — transition Markov state X.

    State transitions:
      localteam red:   0→1 (11v11→10v11), 2→3 (11v10→10v10)
      visitorteam red: 0→2 (11v11→11v10), 1→3 (10v11→10v10)
    """
    if team == "localteam":
        if state.X == 0:
            state.X = 1
        elif state.X == 2:
            state.X = 3
    else:  # visitorteam
        if state.X == 0:
            state.X = 2
        elif state.X == 1:
            state.X = 3
