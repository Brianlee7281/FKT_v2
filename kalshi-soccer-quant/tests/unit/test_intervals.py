"""Tests for Step 1.1 — Interval Splitting.

Reference: implementation_roadmap.md → Step 1.1 verification tests.
"""

from __future__ import annotations

import pytest

from src.calibration.step_1_1_intervals import build_intervals, HALFTIME_BREAK


# ---------------------------------------------------------------------------
# Helpers — build minimal match dicts
# ---------------------------------------------------------------------------

def _make_match(
    match_id: str = "test_001",
    home_goals: list[dict] | None = None,
    away_goals: list[dict] | None = None,
    home_redcards: list[dict] | None = None,
    away_redcards: list[dict] | None = None,
    added_time_1: int | str = 0,
    added_time_2: int | str = 0,
    ht_score_h: int = 0,
    ht_score_a: int = 0,
    ft_score_h: int = 0,
    ft_score_a: int = 0,
) -> dict:
    """Build a minimal Goalserve-shaped match dict for testing."""

    def _goals_section(goals: list[dict] | None) -> dict:
        if not goals:
            return {}
        return {"goals": {"player": goals}}

    def _redcards_section(cards: list[dict] | None) -> dict:
        if not cards:
            return {}
        return {"redcards": {"player": cards}}

    local_summary = {**_goals_section(home_goals), **_redcards_section(home_redcards)}
    visitor_summary = {**_goals_section(away_goals), **_redcards_section(away_redcards)}

    return {
        "id": match_id,
        "matchinfo": {
            "time": {
                "addedTime_period1": str(added_time_1),
                "addedTime_period2": str(added_time_2),
            },
        },
        "localteam": {"ht_score": str(ht_score_h), "ft_score": str(ft_score_h)},
        "visitorteam": {"ht_score": str(ht_score_a), "ft_score": str(ft_score_a)},
        "summary": {
            "localteam": local_summary,
            "visitorteam": visitor_summary,
        },
    }


def _goal(minute: int | str, name: str = "Player",
          penalty: str = "False", owngoal: str = "False",
          var_cancelled: str = "False", extra_min: str = "") -> dict:
    return {
        "id": "1",
        "minute": str(minute),
        "extra_min": extra_min,
        "name": name,
        "penalty": penalty,
        "owngoal": owngoal,
        "var_cancelled": var_cancelled,
    }


def _redcard(minute: int | str, name: str = "Player", extra_min: str = "") -> dict:
    return {
        "id": "1",
        "minute": str(minute),
        "extra_min": extra_min,
        "name": name,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicMatchNoEvents:
    """test_basic_match_no_events — 0-0 draw, no red cards."""

    def test_produces_three_intervals(self):
        """First half + halftime + second half."""
        match = _make_match()
        intervals = build_intervals(match)

        # Should have: [0, 45) play, [45, 60) halftime, [60, 105) play
        non_ht = [iv for iv in intervals if not iv.is_halftime]
        ht = [iv for iv in intervals if iv.is_halftime]

        assert len(non_ht) == 2, f"Expected 2 play intervals, got {len(non_ht)}"
        assert len(ht) == 1, "Expected 1 halftime interval"

    def test_no_goals_recorded(self):
        match = _make_match()
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.home_goal_times == []
            assert iv.away_goal_times == []

    def test_state_is_11v11_throughout(self):
        match = _make_match()
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.state_X == 0

    def test_delta_S_is_zero_throughout(self):
        match = _make_match()
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.delta_S == 0


class TestSingleGoalCreatesTwoIntervals:
    """test_single_goal_creates_two_intervals — 1 home goal at 30'."""

    def test_first_half_split_into_two(self):
        match = _make_match(
            home_goals=[_goal(30)],
            ft_score_h=1,
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # First half: [0,30) and [30,45), Second half: [60,105)
        assert len(non_ht) == 3

    def test_pre_goal_interval_has_delta_S_zero(self):
        match = _make_match(home_goals=[_goal(30)], ft_score_h=1)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # First interval [0, 30) should have ΔS=0
        assert non_ht[0].delta_S == 0

    def test_post_goal_interval_has_delta_S_plus_one(self):
        match = _make_match(home_goals=[_goal(30)], ft_score_h=1)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Second interval [30, 45+α₁) should have ΔS=+1
        assert non_ht[1].delta_S == 1

    def test_goal_recorded_in_correct_interval(self):
        match = _make_match(home_goals=[_goal(30)], ft_score_h=1)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # The goal event is in the first interval (the one that ends at t=30)
        assert non_ht[0].home_goal_times == [30.0]
        assert non_ht[1].home_goal_times == []


class TestVarCancelledGoalExcluded:
    """test_var_cancelled_goal_excluded — VAR-cancelled goal doesn't split."""

    def test_var_cancelled_not_a_split_point(self):
        match = _make_match(
            home_goals=[_goal(30, var_cancelled="True")],
            ft_score_h=0,  # Score unchanged because VAR cancelled
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Same as no-event match: 2 play intervals
        assert len(non_ht) == 2

    def test_delta_S_unchanged(self):
        match = _make_match(
            home_goals=[_goal(30, var_cancelled="True")],
        )
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.delta_S == 0

    def test_mixed_var_and_real_goal(self):
        """One VAR-cancelled at 20', one real goal at 30'. Only real goal splits."""
        match = _make_match(
            home_goals=[
                _goal(20, var_cancelled="True"),
                _goal(30, var_cancelled="False"),
            ],
            ft_score_h=1,
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # 3 play intervals: [0,30), [30,45+α₁), [HT_end, T_m+HT]
        assert len(non_ht) == 3


class TestOwnGoalTeamInversion:
    """test_own_goal_team_inversion — own goal flips scoring team."""

    def test_own_goal_increments_opponent_score(self):
        """Home player own goal → ΔS decreases (away gets the point)."""
        match = _make_match(
            home_goals=[_goal(25, owngoal="True")],
            ft_score_a=1,  # Away benefits
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Own goal by home player → scoring_team = visitorteam → ΔS -= 1
        assert non_ht[0].delta_S == 0   # Before goal
        assert non_ht[1].delta_S == -1  # After goal

    def test_own_goal_marked_in_owngoal_list(self):
        match = _make_match(
            home_goals=[_goal(25, owngoal="True")],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # The goal event is in first interval, flagged as own goal
        assert non_ht[0].goal_is_owngoal == [True]

    def test_own_goal_recorded_as_away_goal(self):
        """Home player's own goal should appear in away_goal_times."""
        match = _make_match(
            home_goals=[_goal(25, owngoal="True")],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Scoring team is visitorteam, so it goes to away_goal_times
        assert non_ht[0].away_goal_times == [25.0]
        assert non_ht[0].home_goal_times == []


class TestRedCardStateTransition:
    """test_red_card_state_transition — red card changes Markov state."""

    def test_home_red_card_transitions_to_state_1(self):
        """Home red at 40' → state 0 → state 1 (10v11)."""
        match = _make_match(
            home_redcards=[_redcard(40)],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        assert non_ht[0].state_X == 0  # Before red
        assert non_ht[1].state_X == 1  # After home red: 10v11

    def test_away_red_card_transitions_to_state_2(self):
        """Away red at 65' → state 0 → state 2 (11v10)."""
        match = _make_match(
            away_redcards=[_redcard(65)],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Red at 65' falls in second half (after halftime_end at 60)
        post_red = [iv for iv in non_ht if iv.state_X == 2]
        assert len(post_red) >= 1

    def test_double_red_transitions_to_state_3(self):
        """Home red at 30', away red at 70' → state 0 → 1 → 3 (10v10)."""
        match = _make_match(
            home_redcards=[_redcard(30)],
            away_redcards=[_redcard(70)],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        states = [iv.state_X for iv in non_ht]
        # Should see 0 → 1 → 1 (persists through HT) → 3
        assert 0 in states
        assert 1 in states
        assert 3 in states

    def test_red_card_persists_through_halftime(self):
        """Red card in first half should persist into second half."""
        match = _make_match(
            home_redcards=[_redcard(30)],
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Second-half interval should still be state 1
        second_half = [iv for iv in non_ht if iv.t_start >= 45 + HALFTIME_BREAK]
        assert all(iv.state_X == 1 for iv in second_half)


class TestHalftimeExcludedFromIntegration:
    """test_halftime_excluded_from_integration."""

    def test_halftime_interval_flagged(self):
        match = _make_match()
        intervals = build_intervals(match)
        ht = [iv for iv in intervals if iv.is_halftime]

        assert len(ht) == 1
        assert ht[0].is_halftime is True

    def test_halftime_duration_is_break_length(self):
        match = _make_match(added_time_1=3)
        intervals = build_intervals(match)
        ht = [iv for iv in intervals if iv.is_halftime]

        assert len(ht) == 1
        expected_start = 45.0 + 3.0
        expected_end = expected_start + HALFTIME_BREAK
        assert ht[0].t_start == pytest.approx(expected_start)
        assert ht[0].t_end == pytest.approx(expected_end)

    def test_play_intervals_exclude_halftime_gap(self):
        """Play intervals should not overlap with halftime."""
        match = _make_match(added_time_1=2)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # First half ends at 47 (45+2), second half starts at 62 (47+15)
        first_half = non_ht[0]
        second_half = non_ht[1]

        assert first_half.t_end == pytest.approx(47.0)
        assert second_half.t_start == pytest.approx(62.0)


class TestAddedTimeTmCalculation:
    """test_added_time_T_m_calculation."""

    def test_T_m_basic(self):
        match = _make_match(added_time_1=3, added_time_2=5)
        intervals = build_intervals(match)

        for iv in intervals:
            assert iv.T_m == pytest.approx(98.0)  # 90 + 3 + 5

    def test_T_m_zero_stoppage(self):
        match = _make_match()
        intervals = build_intervals(match)

        for iv in intervals:
            assert iv.T_m == pytest.approx(90.0)

    def test_alpha_values_stored(self):
        match = _make_match(added_time_1=7, added_time_2=8)
        intervals = build_intervals(match)

        for iv in intervals:
            assert iv.alpha_1 == pytest.approx(7.0)
            assert iv.alpha_2 == pytest.approx(8.0)

    def test_match_end_accounts_for_stoppage(self):
        """Last play interval should end at T_m + HALFTIME_BREAK."""
        match = _make_match(added_time_1=3, added_time_2=5)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        last = non_ht[-1]
        # T_m = 98, match_end event at T_m + HT_BREAK = 113
        assert last.t_end == pytest.approx(98.0 + HALFTIME_BREAK)


class TestWorldCupFinal2022:
    """test_world_cup_final_2022 — complex match with 6 goals.

    Argentina 3–3 France (AET, pen)
    Goals: Messi 23' (pen), Di María 36', Mbappé 80' (pen), 81', Messi 108', Mbappé 118' (pen)
    Added time: α₁=7, α₂=8 → T_m = 105
    No red cards.
    """

    @pytest.fixture
    def match(self):
        return _make_match(
            match_id="wc_final_2022",
            home_goals=[
                _goal(23, name="Messi", penalty="True"),
                _goal(36, name="Di María"),
                _goal(108, name="Messi"),
            ],
            away_goals=[
                _goal(80, name="Mbappé", penalty="True"),
                _goal(81, name="Mbappé"),
                _goal(118, name="Mbappé", penalty="True"),
            ],
            added_time_1=7,
            added_time_2=8,
            ht_score_h=2, ht_score_a=0,
            ft_score_h=3, ft_score_a=3,
        )

    def test_total_play_intervals(self, match):
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Split points: t=23, t=36, HT, t=80, t=81, t=108, t=118, match_end
        # First half: [0,23), [23,36), [36,52)
        # Second half: [67,80), [80,81), [81,108), [108,118), [118,120)
        assert len(non_ht) == 8

    def test_delta_S_progression(self, match):
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        deltas = [iv.delta_S for iv in non_ht]
        # 0, +1, +2, +2, +1, 0, +1, 0
        assert deltas == [0, 1, 2, 2, 1, 0, 1, 0]

    def test_all_state_11v11(self, match):
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.state_X == 0

    def test_T_m_is_105(self, match):
        intervals = build_intervals(match)
        for iv in intervals:
            assert iv.T_m == pytest.approx(105.0)

    def test_goal_count(self, match):
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        total_home = sum(len(iv.home_goal_times) for iv in non_ht)
        total_away = sum(len(iv.away_goal_times) for iv in non_ht)

        assert total_home == 3
        assert total_away == 3


class TestDeltaSBeforeAtGoalTime:
    """test_delta_S_before_at_goal_time — causality: pre-goal ΔS is recorded."""

    def test_first_goal_uses_delta_zero(self):
        """First goal from 0-0 should record delta_before = 0."""
        match = _make_match(home_goals=[_goal(30)], ft_score_h=1)
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Goal in first interval
        assert non_ht[0].goal_delta_before == [0]

    def test_second_goal_uses_pre_goal_delta(self):
        """After 1-0, second home goal should record delta_before = +1."""
        match = _make_match(
            home_goals=[_goal(20), _goal(30)],
            ft_score_h=2,
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # First goal: delta_before=0, Second goal: delta_before=+1
        assert non_ht[0].goal_delta_before == [0]
        assert non_ht[1].goal_delta_before == [1]

    def test_away_goal_after_home_lead(self):
        """Home scores at 20' (ΔS=0→+1), away scores at 40' (ΔS should be +1 before)."""
        match = _make_match(
            home_goals=[_goal(20)],
            away_goals=[_goal(40)],
            ft_score_h=1, ft_score_a=1,
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # Home goal at 20: delta_before=0
        assert non_ht[0].goal_delta_before == [0]
        # Away goal at 40: delta_before=+1 (home was leading)
        assert non_ht[1].goal_delta_before == [1]

    def test_never_uses_post_goal_delta(self):
        """Verify no goal event uses the ΔS that results from itself."""
        match = _make_match(
            home_goals=[_goal(10), _goal(30)],
            away_goals=[_goal(20)],
            ft_score_h=2, ft_score_a=1,
        )
        intervals = build_intervals(match)
        non_ht = [iv for iv in intervals if not iv.is_halftime]

        # t=10 home goal: delta_before=0, then ΔS becomes +1
        # t=20 away goal: delta_before=+1, then ΔS becomes 0
        # t=30 home goal: delta_before=0, then ΔS becomes +1
        goals_with_delta = [
            (iv.home_goal_times + iv.away_goal_times, iv.goal_delta_before)
            for iv in non_ht
            if iv.goal_delta_before
        ]
        deltas = [d for _, deltas in goals_with_delta for d in deltas]
        assert deltas == [0, 1, 0]
