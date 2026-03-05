"""Tests for Step 3.5: Real-Time Stoppage-Time Handling.

Verifies the StoppageTimeManager dual-source cross-validation
and three-phase T adjustment logic.

Reference: phase3.md → Step 3.5
"""

from __future__ import annotations

import pytest

from src.engine.step_3_5_stoppage import StoppageTimeManager


# ---------------------------------------------------------------------------
# Phase A: Regular time — T unchanged
# ---------------------------------------------------------------------------

class TestRegularTime:
    """During regular time, T should remain at T_exp."""

    def test_first_half_regular(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        T = mgr.update_from_live_odds(30.0, "1st Half")
        assert T == 98.0

    def test_second_half_regular(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        T = mgr.update_from_live_odds(70.0, "2nd Half")
        assert T == 98.0

    def test_early_second_half(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        T = mgr.update_from_live_score(50.0, "2nd Half")
        assert T == 98.0


# ---------------------------------------------------------------------------
# Phase B: First-half stoppage — T unchanged, flag set
# ---------------------------------------------------------------------------

class TestFirstHalfStoppage:
    """First-half stoppage keeps T unchanged (finalized by halftime event)."""

    def test_first_half_stoppage_keeps_T(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        T = mgr.update_from_live_odds(46.0, "1st Half")
        assert T == 98.0
        assert mgr.first_half_stoppage is True

    def test_first_half_stoppage_47_min(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        T = mgr.update_from_live_odds(47.5, "1st Half")
        assert T == 98.0

    def test_first_half_stoppage_flag_set_once(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(46.0, "1st Half")
        assert mgr.first_half_stoppage is True
        mgr.update_from_live_odds(47.0, "1st Half")
        assert mgr.first_half_stoppage is True


# ---------------------------------------------------------------------------
# Phase C: Second-half stoppage — T rolling
# ---------------------------------------------------------------------------

class TestSecondHalfStoppage:
    """Second-half stoppage applies rolling T = minute + horizon."""

    def test_second_half_stoppage_rolling(self):
        mgr = StoppageTimeManager(T_exp=98.0, rolling_horizon=1.5)
        T = mgr.update_from_live_odds(91.0, "2nd Half")
        # 91 + 1.5 = 92.5, but T_exp=98 is higher, so T stays 98
        assert T == 98.0

    def test_second_half_stoppage_exceeds_T_exp(self):
        mgr = StoppageTimeManager(T_exp=95.0, rolling_horizon=1.5)
        T = mgr.update_from_live_odds(94.0, "2nd Half")
        # 94 + 1.5 = 95.5 > 95.0 → T updated to 95.5
        assert T == 95.5

    def test_stoppage_rolling_increases_monotonically(self):
        mgr = StoppageTimeManager(T_exp=95.0, rolling_horizon=1.5)
        T1 = mgr.update_from_live_odds(94.0, "2nd Half")
        T2 = mgr.update_from_live_odds(95.0, "2nd Half")
        T3 = mgr.update_from_live_odds(96.0, "2nd Half")
        assert T1 <= T2 <= T3

    def test_second_half_stoppage_flag(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(91.0, "2nd Half")
        assert mgr.second_half_stoppage is True

    def test_current_T_property(self):
        mgr = StoppageTimeManager(T_exp=95.0, rolling_horizon=1.5)
        mgr.update_from_live_odds(95.0, "2nd Half")
        # 95 + 1.5 = 96.5 > 95 → T updated
        assert mgr.current_T == 96.5


# ---------------------------------------------------------------------------
# Dual-source cross-validation
# ---------------------------------------------------------------------------

class TestDualSourceCrossValidation:
    """Cross-validation between Live Odds and Live Score minutes."""

    def test_no_warning_when_minutes_close(self):
        """No warning when sources agree (within 2 min)."""
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(60.0, "2nd Half")
        # Live Score 1 minute behind — acceptable
        T = mgr.update_from_live_score(59.0, "2nd Half")
        assert T == 98.0

    def test_warning_on_minute_mismatch(self):
        """Sources diverging by 2+ minutes should trigger warning."""
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(60.0, "2nd Half")
        # Live Score 3 minutes behind — should warn
        mgr.update_from_live_score(57.0, "2nd Half")
        # Just verify no crash — warning is logged

    def test_live_score_updates_T(self):
        mgr = StoppageTimeManager(T_exp=95.0, rolling_horizon=1.5)
        T = mgr.update_from_live_score(94.0, "2nd Half")
        # 94 + 1.5 = 95.5 > 95 → T updated
        assert T == 95.5


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:
    """Manager reset for new match."""

    def test_reset_clears_state(self):
        mgr = StoppageTimeManager(T_exp=95.0)
        mgr.update_from_live_odds(91.0, "2nd Half")
        assert mgr.second_half_stoppage is True

        mgr.reset(T_exp=98.0)
        assert mgr.T_exp == 98.0
        assert mgr.first_half_stoppage is False
        assert mgr.second_half_stoppage is False
        assert mgr._lo_minute is None
        assert mgr._ls_minute is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case handling."""

    def test_exactly_45_is_regular(self):
        """Minute 45.0 exactly is not stoppage (> 45 required)."""
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(45.0, "1st Half")
        assert mgr.first_half_stoppage is False

    def test_exactly_90_is_regular(self):
        """Minute 90.0 exactly is not stoppage (> 90 required)."""
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(90.0, "2nd Half")
        assert mgr.second_half_stoppage is False

    def test_period_1st_alias(self):
        mgr = StoppageTimeManager(T_exp=98.0)
        mgr.update_from_live_odds(46.0, "1st")
        assert mgr.first_half_stoppage is True

    def test_period_2nd_alias(self):
        mgr = StoppageTimeManager(T_exp=95.0, rolling_horizon=1.5)
        T = mgr.update_from_live_odds(94.0, "2nd")
        assert mgr.second_half_stoppage is True
        assert T == 95.5

    def test_custom_rolling_horizon(self):
        mgr = StoppageTimeManager(T_exp=92.0, rolling_horizon=2.0)
        T = mgr.update_from_live_odds(91.0, "2nd Half")
        # 91 + 2.0 = 93.0 > 92.0 → T updated
        assert T == 93.0
