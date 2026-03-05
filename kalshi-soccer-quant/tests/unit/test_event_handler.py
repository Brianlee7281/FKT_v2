"""Tests for Step 3.2: State Machine + Event Handlers.

Verifies the two-stage event processing pipeline:
  Preliminary (Live Odds) → Confirmed (Live Score)

Reference: implementation_roadmap.md → Step 3.2 tests
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.common.types import NormalizedEvent
from src.engine.event_handler import (
    dispatch_live_odds_event,
    dispatch_live_score_event,
    handle_confirmed_goal,
    handle_confirmed_red_card,
    handle_live_score_failure,
    handle_odds_spike,
    handle_preliminary_goal,
    handle_score_rollback,
)
from src.engine.state_machine import (
    FIRST_HALF,
    FINISHED,
    HALFTIME,
    IDLE,
    PRELIMINARY_DETECTED,
    SECOND_HALF,
    EngineState,
    check_cooldown_release,
    check_ob_freeze_release,
    commit_goal,
    commit_red_card,
    record_stable_tick,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> EngineState:
    """Create an EngineState with sensible defaults."""
    defaults = {
        "engine_phase": FIRST_HALF,
        "score": (0, 0),
        "delta_S": 0,
        "X": 0,
        "event_state": IDLE,
    }
    defaults.update(kwargs)
    return EngineState(**defaults)


def _make_event(
    type: str = "goal_detected",
    source: str = "live_odds",
    confidence: str = "preliminary",
    score: tuple[int, int] | None = None,
    team: str | None = None,
    var_cancelled: bool = False,
    delta: float | None = None,
    period: str | None = None,
    **kwargs,
) -> NormalizedEvent:
    return NormalizedEvent(
        type=type,
        source=source,
        confidence=confidence,
        timestamp=time.time(),
        score=score,
        team=team,
        var_cancelled=var_cancelled,
        delta=delta,
        period=period,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tests per roadmap
# ---------------------------------------------------------------------------

class TestPreliminarySetsObFreeze:
    """test_preliminary_sets_ob_freeze"""

    def test_preliminary_goal_sets_ob_freeze(self):
        state = _make_state()
        event = _make_event(type="goal_detected", score=(1, 0))

        handle_preliminary_goal(state, event)

        assert state.ob_freeze is True
        assert state.event_state == PRELIMINARY_DETECTED

    def test_preliminary_caches_scoring_team(self):
        state = _make_state()
        event = _make_event(type="goal_detected", score=(1, 0))

        handle_preliminary_goal(state, event)

        assert state.preliminary_cache["scoring_team"] == "localteam"
        assert state.preliminary_cache["delta_S"] == 1

    def test_away_goal_preliminary(self):
        state = _make_state()
        event = _make_event(type="goal_detected", score=(0, 1))

        handle_preliminary_goal(state, event)

        assert state.preliminary_cache["scoring_team"] == "visitorteam"
        assert state.preliminary_cache["delta_S"] == -1


class TestConfirmedGoalUpdatesSAndDeltaS:
    """test_confirmed_goal_updates_S_and_deltaS"""

    def test_home_goal_updates_score(self):
        state = _make_state()
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", score=(1, 0), team="localteam",
        )

        handle_confirmed_goal(state, event)

        assert state.score == (1, 0)
        assert state.delta_S == 1

    def test_away_goal_updates_score(self):
        state = _make_state()
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", score=(0, 1), team="visitorteam",
        )

        handle_confirmed_goal(state, event)

        assert state.score == (0, 1)
        assert state.delta_S == -1

    def test_second_goal_increments(self):
        state = _make_state(score=(1, 0), delta_S=1)
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", score=(2, 0), team="localteam",
        )

        handle_confirmed_goal(state, event)

        assert state.score == (2, 0)
        assert state.delta_S == 2


class TestConfirmedGoalAppliesDelta:
    """test_confirmed_goal_applies_delta_H_and_A"""

    def test_delta_S_changes_on_goal(self):
        """ΔS should update, which drives δ_H(ΔS) and δ_A(ΔS) in intensity."""
        state = _make_state(score=(1, 1), delta_S=0)
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="localteam",
        )

        handle_confirmed_goal(state, event)

        # Home scores: ΔS goes from 0 to +1
        assert state.delta_S == 1

    def test_away_goal_negative_delta(self):
        state = _make_state(score=(0, 0), delta_S=0)
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="visitorteam",
        )

        handle_confirmed_goal(state, event)
        assert state.delta_S == -1


class TestVarCancelledRollsBackState:
    """test_var_cancelled_rolls_back_state"""

    def test_var_cancelled_resets_to_idle(self):
        state = _make_state(event_state=PRELIMINARY_DETECTED, ob_freeze=True)
        state.preliminary_cache = {"score": (1, 0), "delta_S": 1}

        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", var_cancelled=True,
        )

        handle_confirmed_goal(state, event)

        assert state.event_state == IDLE
        assert state.ob_freeze is False
        assert state.score == (0, 0)  # Score NOT committed
        assert state.preliminary_cache == {}

    def test_score_rollback_from_preliminary(self):
        state = _make_state(event_state=PRELIMINARY_DETECTED, ob_freeze=True)
        state.preliminary_cache = {"score": (1, 0)}

        event = _make_event(type="score_rollback", score=(0, 0))

        handle_score_rollback(state, event)

        assert state.event_state == IDLE
        assert state.ob_freeze is False


class TestRedCardTransitionsX:
    """test_red_card_transitions_X_correctly"""

    def test_home_red_0_to_1(self):
        state = _make_state(X=0)
        commit_red_card(state, "localteam")
        assert state.X == 1

    def test_home_red_2_to_3(self):
        state = _make_state(X=2)
        commit_red_card(state, "localteam")
        assert state.X == 3

    def test_away_red_0_to_2(self):
        state = _make_state(X=0)
        commit_red_card(state, "visitorteam")
        assert state.X == 2

    def test_away_red_1_to_3(self):
        state = _make_state(X=1)
        commit_red_card(state, "visitorteam")
        assert state.X == 3

    def test_no_transition_from_absorbing(self):
        """State 3 is absorbing — red card shouldn't change it."""
        state = _make_state(X=3)
        commit_red_card(state, "localteam")
        assert state.X == 3


class TestRedCardAppliesGamma:
    """test_red_card_applies_both_gamma_H_and_A"""

    def test_confirmed_red_card_updates_X_and_cooldown(self):
        """Red card confirmation should update X and trigger cooldown."""
        state = _make_state(X=0)
        event = _make_event(
            type="red_card", source="live_score",
            confidence="confirmed", team="localteam",
        )

        handle_confirmed_red_card(state, event)

        # X transitioned (gamma^H and gamma^A now indexed by new X)
        assert state.X == 1
        assert state.cooldown is True
        assert state.event_state == IDLE

    def test_away_red_card(self):
        state = _make_state(X=0)
        event = _make_event(
            type="red_card", source="live_score",
            confidence="confirmed", team="visitorteam",
        )

        handle_confirmed_red_card(state, event)

        assert state.X == 2
        assert state.cooldown is True


class TestCooldownBlocksOrderAllowed:
    """test_cooldown_blocks_order_allowed"""

    def test_cooldown_blocks_orders(self):
        state = _make_state(cooldown=True)
        assert state.order_allowed is False

    def test_ob_freeze_blocks_orders(self):
        state = _make_state(ob_freeze=True)
        assert state.order_allowed is False

    def test_preliminary_blocks_orders(self):
        state = _make_state(event_state=PRELIMINARY_DETECTED)
        assert state.order_allowed is False

    def test_halftime_blocks_orders(self):
        state = _make_state(engine_phase=HALFTIME)
        assert state.order_allowed is False

    def test_normal_state_allows_orders(self):
        state = _make_state(
            engine_phase=FIRST_HALF,
            cooldown=False,
            ob_freeze=False,
            event_state=IDLE,
        )
        assert state.order_allowed is True

    def test_confirmed_goal_enters_cooldown(self):
        state = _make_state()
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="localteam",
        )

        handle_confirmed_goal(state, event)

        assert state.cooldown is True
        assert state.order_allowed is False


class TestFalseAlarmAfterTimeout:
    """test_false_alarm_after_timeout"""

    def test_ob_freeze_releases_after_timeout(self):
        state = _make_state(ob_freeze=True)
        state._ob_freeze_start = time.time() - 11  # 11 seconds ago

        check_ob_freeze_release(state)

        assert state.ob_freeze is False

    def test_ob_freeze_releases_after_3_stable_ticks(self):
        state = _make_state(ob_freeze=True)
        state._ob_freeze_start = time.time()

        # Simulate 3 stable ticks
        record_stable_tick(state)
        record_stable_tick(state)
        record_stable_tick(state)

        check_ob_freeze_release(state)

        assert state.ob_freeze is False

    def test_ob_freeze_not_released_with_2_stable_ticks(self):
        state = _make_state(ob_freeze=True)
        state._ob_freeze_start = time.time()  # Recent — no timeout

        record_stable_tick(state)
        record_stable_tick(state)

        check_ob_freeze_release(state)

        assert state.ob_freeze is True  # Not yet released

    def test_cooldown_releases_ob_freeze(self):
        state = _make_state(ob_freeze=True, cooldown=True)
        state._ob_freeze_start = time.time()

        check_ob_freeze_release(state)

        assert state.ob_freeze is False


class TestPreliminaryCacheReused:
    """test_preliminary_cache_reused_on_confirm"""

    def test_cache_reused_when_delta_matches(self):
        state = _make_state()

        # Step 1: Preliminary goal detected
        prelim_event = _make_event(type="goal_detected", score=(1, 0))
        handle_preliminary_goal(state, prelim_event)

        assert state.preliminary_cache["delta_S"] == 1

        # Step 2: Confirmed goal — same delta_S
        confirm_event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="localteam",
        )

        # After confirmation, delta_S should be 1 (matching cache)
        handle_confirmed_goal(state, confirm_event)

        assert state.score == (1, 0)
        assert state.delta_S == 1
        # Cache cleared after use
        assert state.preliminary_cache == {}
        assert state.cooldown is True

    def test_cache_cleared_on_var_cancel(self):
        state = _make_state()

        prelim_event = _make_event(type="goal_detected", score=(1, 0))
        handle_preliminary_goal(state, prelim_event)
        assert state.preliminary_cache != {}

        # VAR cancels
        cancel_event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", var_cancelled=True,
        )
        handle_confirmed_goal(state, cancel_event)

        assert state.preliminary_cache == {}


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------

class TestDispatchers:
    def test_dispatch_live_odds_goal(self):
        state = _make_state()
        event = _make_event(type="goal_detected", score=(1, 0))
        dispatch_live_odds_event(state, event)
        assert state.event_state == PRELIMINARY_DETECTED

    def test_dispatch_live_odds_period_change(self):
        state = _make_state()
        event = _make_event(type="period_change", period="Paused")
        dispatch_live_odds_event(state, event)
        assert state.engine_phase == HALFTIME

    def test_dispatch_live_score_goal(self):
        state = _make_state()
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="localteam",
        )
        dispatch_live_score_event(state, event)
        assert state.score == (1, 0)

    def test_dispatch_live_score_red_card(self):
        state = _make_state(X=0)
        event = _make_event(
            type="red_card", source="live_score",
            confidence="confirmed", team="visitorteam",
        )
        dispatch_live_score_event(state, event)
        assert state.X == 2

    def test_dispatch_live_score_match_finished(self):
        state = _make_state()
        event = _make_event(type="match_finished", source="live_score")
        dispatch_live_score_event(state, event)
        assert state.engine_phase == FINISHED

    def test_dispatch_source_failure(self):
        state = _make_state(ob_freeze=False)
        event = _make_event(type="source_failure", source="live_score")
        dispatch_live_score_event(state, event)
        assert state.ob_freeze is True


# ---------------------------------------------------------------------------
# Cooldown release tests
# ---------------------------------------------------------------------------

class TestCooldownRelease:
    """test_cooldown_releases_after_duration"""

    def test_cooldown_releases_after_15s(self):
        """Cooldown should release after COOLDOWN_DURATION seconds."""
        state = _make_state(cooldown=True)
        state._cooldown_start = time.time() - 16  # 16 seconds ago

        check_cooldown_release(state)

        assert state.cooldown is False

    def test_cooldown_not_released_before_15s(self):
        """Cooldown should NOT release before duration elapses."""
        state = _make_state(cooldown=True)
        state._cooldown_start = time.time() - 5  # 5 seconds ago

        check_cooldown_release(state)

        assert state.cooldown is True

    def test_cooldown_release_restores_order_allowed(self):
        """After cooldown release, orders should be allowed again."""
        state = _make_state(cooldown=True)
        state._cooldown_start = time.time() - 16

        assert state.order_allowed is False
        check_cooldown_release(state)
        assert state.order_allowed is True

    def test_no_op_when_not_in_cooldown(self):
        """check_cooldown_release should be safe to call when not cooling."""
        state = _make_state(cooldown=False)
        check_cooldown_release(state)
        assert state.cooldown is False

    def test_confirmed_goal_sets_cooldown_start(self):
        """Confirmed goal should set _cooldown_start timestamp."""
        state = _make_state()
        event = _make_event(
            type="goal_confirmed", source="live_score",
            confidence="confirmed", team="localteam",
        )

        before = time.time()
        handle_confirmed_goal(state, event)

        assert state.cooldown is True
        assert state._cooldown_start >= before
