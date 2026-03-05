"""Tests for Step 1.2 — Q Matrix Estimation.

Reference: implementation_roadmap.md → Step 1.2 verification tests.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.common.types import IntervalRecord
from src.calibration.step_1_2_Q_matrix import (
    estimate_Q,
    compute_Q_off_normalized,
    N_STATES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iv(
    match_id: str = "m1",
    t_start: float = 0.0,
    t_end: float = 90.0,
    state_X: int = 0,
    delta_S: int = 0,
    is_halftime: bool = False,
    T_m: float = 90.0,
) -> IntervalRecord:
    """Create a minimal IntervalRecord for testing."""
    return IntervalRecord(
        match_id=match_id,
        t_start=t_start,
        t_end=t_end,
        state_X=state_X,
        delta_S=delta_S,
        home_goal_times=[],
        away_goal_times=[],
        goal_delta_before=[],
        goal_is_owngoal=[],
        T_m=T_m,
        is_halftime=is_halftime,
        alpha_1=0.0,
        alpha_2=0.0,
    )


def _make_clean_match(match_id: str = "m1") -> list[IntervalRecord]:
    """A 0-0 match with no red cards: [0,45) play, [45,60) HT, [60,105) play."""
    return [
        _iv(match_id=match_id, t_start=0, t_end=45, state_X=0),
        _iv(match_id=match_id, t_start=45, t_end=60, state_X=0, is_halftime=True),
        _iv(match_id=match_id, t_start=60, t_end=105, state_X=0),
    ]


def _make_home_red_match(match_id: str = "red1", red_minute: float = 30.0) -> list[IntervalRecord]:
    """Match with a home red card → state 0 → state 1."""
    return [
        _iv(match_id=match_id, t_start=0, t_end=red_minute, state_X=0),
        _iv(match_id=match_id, t_start=red_minute, t_end=45, state_X=1),
        _iv(match_id=match_id, t_start=45, t_end=60, state_X=1, is_halftime=True),
        _iv(match_id=match_id, t_start=60, t_end=105, state_X=1),
    ]


def _make_away_red_match(match_id: str = "red2", red_minute: float = 70.0) -> list[IntervalRecord]:
    """Match with an away red card → state 0 → state 2."""
    return [
        _iv(match_id=match_id, t_start=0, t_end=45, state_X=0),
        _iv(match_id=match_id, t_start=45, t_end=60, state_X=0, is_halftime=True),
        _iv(match_id=match_id, t_start=60, t_end=red_minute, state_X=0),
        _iv(match_id=match_id, t_start=red_minute, t_end=105, state_X=2),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoRedCardsQNearZero:
    """test_no_red_cards_Q_near_zero — all entries should be ≈ 0."""

    def test_off_diagonal_all_zero(self):
        """20 clean matches → no transitions → Q off-diagonal = 0."""
        intervals = []
        for i in range(20):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        # Off-diagonal should all be zero
        for i in range(N_STATES):
            for j in range(N_STATES):
                if i != j:
                    assert Q[i, j] == pytest.approx(0.0), \
                        f"Q[{i},{j}] should be 0 with no red cards"

    def test_diagonal_all_zero(self):
        """With no off-diagonal, diagonal should also be 0."""
        intervals = []
        for i in range(20):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        for i in range(N_STATES):
            assert Q[i, i] == pytest.approx(0.0)


class TestSingleRedCardCounted:
    """test_single_red_card_counted — one home red produces q_{0→1} > 0."""

    def test_q_01_positive(self):
        """1 red card match among 10 clean matches → q_{0→1} > 0."""
        intervals = _make_home_red_match(match_id="red_match")
        for i in range(10):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        assert Q[0, 1] > 0, "q_{0→1} should be positive with 1 home red card"

    def test_q_01_reasonable_magnitude(self):
        """Red card rate should be in reasonable range (~0.01-0.1 per 90 min)."""
        intervals = _make_home_red_match(match_id="red_match")
        for i in range(49):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        # 1 red card in 50 matches, total state-0 time ≈ 50*90 = 4500 min
        # q_{0→1} ≈ 1/4500 ≈ 0.000222 per minute
        # Per 90 min: ≈ 0.02
        rate_per_90 = Q[0, 1] * 90
        assert 0.001 < rate_per_90 < 0.5, \
            f"Red card rate per 90 min = {rate_per_90}, outside reasonable range"

    def test_away_red_q_02_positive(self):
        """Away red card → q_{0→2} > 0."""
        intervals = _make_away_red_match(match_id="away_red")
        for i in range(10):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        assert Q[0, 2] > 0, "q_{0→2} should be positive with 1 away red card"


class TestDiagonalSumZero:
    """test_diagonal_sum_zero — q_ii = -Σ_{j≠i} q_ij, so rows sum to 0."""

    def test_rows_sum_to_zero(self):
        """Every row of Q must sum to exactly 0."""
        intervals = _make_home_red_match(match_id="red1")
        intervals.extend(_make_away_red_match(match_id="red2"))
        for i in range(20):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        for i in range(N_STATES):
            row_sum = np.sum(Q[i, :])
            assert row_sum == pytest.approx(0.0, abs=1e-12), \
                f"Row {i} sum = {row_sum}, should be 0"

    def test_diagonal_is_negative(self):
        """Diagonal entries should be ≤ 0."""
        intervals = _make_home_red_match(match_id="red1")
        for i in range(10):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)

        for i in range(N_STATES):
            assert Q[i, i] <= 0.0, f"Q[{i},{i}] = {Q[i,i]} should be ≤ 0"


class TestQOffNormalizedRowsSumTo1:
    """test_Q_off_normalized_rows_sum_to_1."""

    def test_active_rows_sum_to_one(self):
        """Rows with transitions should have off-diagonal probabilities summing to 1."""
        intervals = _make_home_red_match(match_id="red1")
        intervals.extend(_make_away_red_match(match_id="red2"))
        for i in range(20):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)
        Q_off = compute_Q_off_normalized(Q)

        # State 0 has transitions to both 1 and 2
        row_0_sum = np.sum(Q_off[0, :])
        assert row_0_sum == pytest.approx(1.0, abs=1e-10), \
            f"Q_off row 0 sum = {row_0_sum}, should be 1.0"

    def test_inactive_rows_sum_to_zero(self):
        """Rows with no observed transitions → all zeros."""
        intervals = _make_home_red_match(match_id="red1")
        for i in range(10):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)
        Q_off = compute_Q_off_normalized(Q)

        # State 2, 3 have no transitions in this dataset
        assert np.sum(Q_off[2, :]) == pytest.approx(0.0)
        assert np.sum(Q_off[3, :]) == pytest.approx(0.0)

    def test_diagonal_always_zero(self):
        """Q_off_normalized diagonal should always be 0."""
        intervals = _make_home_red_match(match_id="red1")
        intervals.extend(_make_away_red_match(match_id="red2"))
        for i in range(10):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals)
        Q_off = compute_Q_off_normalized(Q)

        for i in range(N_STATES):
            assert Q_off[i, i] == pytest.approx(0.0)


class TestAdditivityState3:
    """test_additivity_state_3 — shrinkage toward additivity assumption."""

    def test_shrinkage_applies_additivity(self):
        """With full shrinkage (alpha=1.0), q_{1→3} = q_{0→2} and q_{2→3} = q_{0→1}."""
        # Create dataset with home and away reds in state 0
        intervals = []
        for i in range(5):
            intervals.extend(_make_home_red_match(match_id=f"home_red_{i}"))
        for i in range(3):
            intervals.extend(_make_away_red_match(match_id=f"away_red_{i}"))
        for i in range(50):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals, shrinkage_alpha=1.0)

        # q_{1→3} should equal q_{0→2} (away red rate applied to state 1→3)
        assert Q[1, 3] == pytest.approx(Q[0, 2], rel=0.01), \
            f"q_{{1→3}}={Q[1,3]} should ≈ q_{{0→2}}={Q[0,2]} under additivity"

        # q_{2→3} should equal q_{0→1} (home red rate applied to state 2→3)
        assert Q[2, 3] == pytest.approx(Q[0, 1], rel=0.01), \
            f"q_{{2→3}}={Q[2,3]} should ≈ q_{{0→1}}={Q[0,1]} under additivity"

    def test_no_shrinkage_preserves_empirical(self):
        """With alpha=0, state 3 transitions stay at empirical value (likely 0)."""
        intervals = []
        for i in range(5):
            intervals.extend(_make_home_red_match(match_id=f"home_red_{i}"))
        for i in range(50):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals, shrinkage_alpha=0.0)

        # No observed 1→3 transitions, so q_{1→3} = 0
        assert Q[1, 3] == pytest.approx(0.0)

    def test_rows_still_sum_to_zero_after_shrinkage(self):
        """Diagonal condition must hold even after shrinkage."""
        intervals = []
        for i in range(5):
            intervals.extend(_make_home_red_match(match_id=f"home_red_{i}"))
        for i in range(3):
            intervals.extend(_make_away_red_match(match_id=f"away_red_{i}"))
        for i in range(50):
            intervals.extend(_make_clean_match(match_id=f"clean_{i}"))

        Q = estimate_Q(intervals, shrinkage_alpha=0.5)

        for i in range(N_STATES):
            row_sum = np.sum(Q[i, :])
            assert row_sum == pytest.approx(0.0, abs=1e-12), \
                f"Row {i} sum = {row_sum} after shrinkage, should be 0"
