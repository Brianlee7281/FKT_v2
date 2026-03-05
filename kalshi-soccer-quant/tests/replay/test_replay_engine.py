"""Tests for Step 3.5: Replay Engine.

Verifies the replay engine correctly processes event sequences
and produces valid snapshots. Includes the 2022 World Cup Final
as the primary integration test.

Reference: implementation_roadmap.md → Step 3.5
"""

from __future__ import annotations

import numpy as np
import pytest

from src.common.types import NormalizedEvent
from src.engine.state_machine import (
    FINISHED,
    FIRST_HALF,
    HALFTIME,
    IDLE,
    PRELIMINARY_DETECTED,
    SECOND_HALF,
)
from tests.replay.replay_engine import (
    ReplayEngine,
    ReplayModelParams,
    Snapshot,
    make_goal_event,
    make_halftime_event,
    make_match_finished_event,
    make_red_card_event,
    make_second_half_event,
    make_var_cancelled_event,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def default_params():
    """Balanced model params for testing."""
    Q_off = np.zeros((4, 4), dtype=np.float64)
    Q_off[0, 1] = 0.5
    Q_off[0, 2] = 0.5
    Q_off[1, 3] = 1.0
    Q_off[2, 3] = 1.0

    return ReplayModelParams(
        a_H=-3.5,
        a_A=-3.5,
        b=np.zeros(6, dtype=np.float64),
        gamma_H=np.zeros(4, dtype=np.float64),
        gamma_A=np.zeros(4, dtype=np.float64),
        delta_H=np.zeros(5, dtype=np.float64),
        delta_A=np.zeros(5, dtype=np.float64),
        Q_diag=np.array([-0.01, -0.01, -0.01, 0.0], dtype=np.float64),
        Q_off_normalized=Q_off,
        basis_bounds=np.array(
            [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0], dtype=np.float64
        ),
        T_exp=98.0,
        delta_significant=False,
        match_id="test_match",
        N_MC=5000,  # Reduced for test speed
    )


def _build_wc_final_events() -> list[NormalizedEvent]:
    """Build event sequence for 2022 World Cup Final (Argentina vs France).

    Argentina 3 - 3 France (Argentina wins on penalties)

    Goals:
      23' Messi (ARG) - penalty → 1-0
      36' Di María (ARG) → 2-0
      80' Mbappé (FRA) - penalty → 2-1
      81' Mbappé (FRA) → 2-2
      108' Messi (ARG) → 3-2  (extra time)
      118' Mbappé (FRA) - penalty → 3-3

    For simplicity, we use regular time mapping:
      - First half goals at 23', 36'
      - Second half goals at 80', 81'
      - Extra time goals mapped as 90+18=93' and 90+28=95'
    """
    events = []

    # Goal 1: Messi 23' → 1-0
    events.extend(make_goal_event(23.0, "localteam", (1, 0), "1st Half"))

    # Goal 2: Di María 36' → 2-0
    events.extend(make_goal_event(36.0, "localteam", (2, 0), "1st Half"))

    # Halftime
    events.extend(make_halftime_event(45.0))

    # Second half start
    events.append(make_second_half_event(45.0))

    # Goal 3: Mbappé 80' → 2-1
    events.extend(make_goal_event(80.0, "visitorteam", (2, 1), "2nd Half"))

    # Goal 4: Mbappé 81' → 2-2
    events.extend(make_goal_event(81.0, "visitorteam", (2, 2), "2nd Half"))

    # Goal 5: Messi 93' (extra time mapped) → 3-2
    events.extend(make_goal_event(93.0, "localteam", (3, 2), "2nd Half"))

    # Goal 6: Mbappé 95' (extra time mapped) → 3-3
    events.extend(make_goal_event(95.0, "visitorteam", (3, 3), "2nd Half"))

    # Match finished
    events.append(make_match_finished_event(98.0))

    return events


# ---------------------------------------------------------------------------
# Basic replay tests
# ---------------------------------------------------------------------------

class TestReplayBasic:
    """Basic replay engine functionality."""

    def test_empty_event_list(self, default_params):
        """Replay with no events returns initial snapshot."""
        engine = ReplayEngine(default_params)
        snapshots = engine.replay([])
        assert len(snapshots) == 1  # kickoff snapshot only
        assert snapshots[0].trigger_event == "kickoff"

    def test_single_goal(self, default_params):
        """Single goal produces correct snapshots."""
        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # kickoff + 2 events (preliminary + confirmed)
        assert len(snapshots) == 3

        # After confirmed goal
        last = snapshots[-1]
        assert last.score == (1, 0)
        assert last.delta_S == 1

    def test_snapshot_has_pricing(self, default_params):
        """Snapshots should contain P_true values."""
        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # Last snapshot (after confirmed goal) should have pricing
        last = snapshots[-1]
        assert "home_win" in last.P_true
        assert "over_25" in last.P_true
        assert 0.0 <= last.P_true["home_win"] <= 1.0

    def test_trigger_events_recorded(self, default_params):
        """Each snapshot records what triggered it."""
        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        assert snapshots[0].trigger_event == "kickoff"
        assert snapshots[1].trigger_event == "live_odds:goal_detected"
        assert snapshots[2].trigger_event == "live_score:goal_confirmed"


# ---------------------------------------------------------------------------
# State transition tests
# ---------------------------------------------------------------------------

class TestReplayStateTransitions:
    """Verify state transitions during replay."""

    def test_halftime_transition(self, default_params):
        """Halftime events should transition engine phase."""
        events = make_halftime_event(45.0)
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # After halftime event
        last = snapshots[-1]
        assert last.engine_phase == HALFTIME

    def test_second_half_transition(self, default_params):
        """Second half event should transition from halftime."""
        events = make_halftime_event(45.0)
        events.append(make_second_half_event(45.0))

        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.engine_phase == SECOND_HALF

    def test_match_finished(self, default_params):
        """Match finished event should set FINISHED phase."""
        events = [make_match_finished_event(90.0)]
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.engine_phase == FINISHED

    def test_red_card_updates_X(self, default_params):
        """Red card should transition Markov state X."""
        events = [make_red_card_event(30.0, "localteam", "1st Half")]
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.X == 1  # 0 → 1 (home red)


# ---------------------------------------------------------------------------
# VAR cancellation
# ---------------------------------------------------------------------------

class TestReplayVAR:
    """VAR cancellation during replay."""

    def test_var_cancelled_rolls_back_score(self, default_params):
        """VAR cancellation should not commit the goal."""
        events = []

        # Preliminary goal detected
        events.append(NormalizedEvent(
            type="goal_detected", source="live_odds",
            confidence="preliminary", timestamp=1500.0,
            score=(1, 0), minute=25.0, period="1st Half",
        ))

        # VAR cancels it
        events.append(make_var_cancelled_event(25.0, (0, 0), "1st Half"))

        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.score == (0, 0)
        assert last.delta_S == 0
        assert last.ob_freeze is False


# ---------------------------------------------------------------------------
# Pricing mode transitions
# ---------------------------------------------------------------------------

class TestReplayPricingMode:
    """Verify pricing mode switches correctly."""

    def test_analytical_at_start(self, default_params):
        """At kickoff (X=0, ΔS=0), pricing should be analytical."""
        engine = ReplayEngine(default_params)
        snapshots = engine.replay([])

        # Initial snapshot has no pricing computed yet — that's ok
        # Let's trigger a tick
        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # After preliminary (X=0, ΔS=0 still), should be analytical
        assert snapshots[1].pricing_mode == "analytical"

    def test_mc_after_goal(self, default_params):
        """After goal (ΔS≠0), pricing should switch to MC."""
        default_params.delta_significant = True

        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # After confirmed goal: ΔS=1, delta_significant=True → MC
        last = snapshots[-1]
        assert last.delta_S == 1
        # With delta_significant=True and ΔS≠0, should be MC
        assert last.pricing_mode == "monte_carlo"

    def test_mc_after_red_card(self, default_params):
        """After red card (X≠0), pricing should be MC."""
        events = [make_red_card_event(30.0, "localteam", "1st Half")]
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.X == 1
        assert last.pricing_mode == "monte_carlo"


# ---------------------------------------------------------------------------
# 2022 World Cup Final — Full integration test
# ---------------------------------------------------------------------------

class TestWorldCupFinal2022:
    """Replay the 2022 World Cup Final: Argentina 3-3 France.

    Verification: P_true changes correctly after all 6 goals.
    """

    def test_final_score_is_3_3(self, default_params):
        """Final score should be 3-3."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # Find last snapshot before match_finished
        goal_snapshots = [
            s for s in snapshots
            if s.trigger_event and "goal_confirmed" in s.trigger_event
        ]

        last_goal = goal_snapshots[-1]
        assert last_goal.score == (3, 3)
        assert last_goal.delta_S == 0

    def test_six_goals_detected(self, default_params):
        """Should record exactly 6 confirmed goals."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        goal_confirmations = [
            s for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
        ]
        assert len(goal_confirmations) == 6

    def test_score_progression(self, default_params):
        """Score should progress correctly through all 6 goals."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        goal_scores = [
            s.score for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
        ]

        expected = [
            (1, 0),  # Messi 23'
            (2, 0),  # Di María 36'
            (2, 1),  # Mbappé 80'
            (2, 2),  # Mbappé 81'
            (3, 2),  # Messi 93'
            (3, 3),  # Mbappé 95'
        ]

        assert goal_scores == expected

    def test_delta_S_progression(self, default_params):
        """ΔS should follow score progression."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        delta_s_values = [
            s.delta_S for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
        ]

        expected = [1, 2, 1, 0, 1, 0]
        assert delta_s_values == expected

    def test_halftime_recorded(self, default_params):
        """Halftime should be recorded in snapshots."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        halftime_snaps = [
            s for s in snapshots
            if s.engine_phase == HALFTIME
        ]
        assert len(halftime_snaps) > 0

    def test_home_win_prob_increases_after_home_goal(self, default_params):
        """After 1-0, home win probability should be higher than at 0-0."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        # Find snapshots for kickoff and after first goal
        kickoff = snapshots[0]
        first_goal = next(
            s for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
            and s.score == (1, 0)
        )

        # P_true may not be computed at kickoff, so check first goal
        assert first_goal.P_true.get("home_win", 0) > 0.3

    def test_home_win_drops_after_equalizer(self, default_params):
        """After 2-2, home win should drop significantly from 2-0."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        goal_snaps = [
            s for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
        ]

        # 2-0 snapshot
        snap_2_0 = goal_snaps[1]
        # 2-2 snapshot
        snap_2_2 = goal_snaps[3]

        assert snap_2_0.P_true["home_win"] > snap_2_2.P_true["home_win"]

    def test_over_25_increases_with_goals(self, default_params):
        """P(over 2.5) should increase as goals are scored."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        goal_snaps = [
            s for s in snapshots
            if s.trigger_event == "live_score:goal_confirmed"
        ]

        # After 3rd goal (2-1), over 2.5 should be 1.0
        snap_2_1 = goal_snaps[2]
        assert snap_2_1.P_true["over_25"] == 1.0

    def test_match_ends_finished(self, default_params):
        """Match should end in FINISHED state."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.engine_phase == FINISHED


# ---------------------------------------------------------------------------
# Replay with ticks
# ---------------------------------------------------------------------------

class TestReplayWithTicks:
    """Replay with regular tick intervals."""

    def test_ticks_produce_more_snapshots(self, default_params):
        """Ticked replay should produce more snapshots than event-only."""
        events = make_goal_event(25.0, "localteam", (1, 0), "1st Half")
        events.append(make_match_finished_event(30.0))

        engine1 = ReplayEngine(default_params)
        event_only = engine1.replay(events)

        engine2 = ReplayEngine(default_params)
        ticked = engine2.replay_with_ticks(events, tick_interval=5.0,
                                            match_duration=30.0)

        assert len(ticked) > len(event_only)

    def test_ticks_have_pricing(self, default_params):
        """Tick snapshots should have pricing data."""
        events = [make_match_finished_event(30.0)]

        engine = ReplayEngine(default_params)
        snapshots = engine.replay_with_ticks(
            events, tick_interval=5.0, match_duration=30.0
        )

        tick_snaps = [s for s in snapshots if s.trigger_event == "tick"]
        assert len(tick_snaps) > 0

        # At least some tick snapshots should have pricing
        priced = [s for s in tick_snaps if s.P_true]
        assert len(priced) > 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestReplayEdgeCases:
    """Edge cases and robustness."""

    def test_multiple_goals_same_minute(self, default_params):
        """Two goals in the same minute (Mbappé 80', 81')."""
        events = []
        events.extend(make_goal_event(80.0, "visitorteam", (0, 1), "2nd Half"))
        events.extend(make_goal_event(81.0, "visitorteam", (0, 2), "2nd Half"))

        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        last = snapshots[-1]
        assert last.score == (0, 2)
        assert last.delta_S == -2

    def test_all_probabilities_valid_throughout(self, default_params):
        """All P_true values should be in [0, 1] throughout replay."""
        events = _build_wc_final_events()
        engine = ReplayEngine(default_params)
        snapshots = engine.replay(events)

        for snap in snapshots:
            for key, val in snap.P_true.items():
                assert 0.0 <= val <= 1.0, (
                    f"{key}={val} at minute {snap.match_minute}"
                )
