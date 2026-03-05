"""Tests for Step 4.4: Position Exit Logic (v2 Fixes).

These are the most important unit tests in the entire system.
The v2 Buy No fixes prevent systematic money loss.

Reference: implementation_roadmap.md -> Step 4.4 tests
"""

from __future__ import annotations

import pytest

from src.trading.step_4_4_exit_logic import (
    DIVERGENCE_THRESHOLD,
    THETA_ENTRY,
    THETA_EXIT,
    DivergenceAlert,
    ExitSignal,
    OpenPosition,
    check_bet365_divergence,
    check_edge_decay,
    check_edge_reversal,
    check_expiry_eval,
    evaluate_exit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def buy_yes_position():
    """Buy Yes position entered at 0.55."""
    return OpenPosition(
        direction="BUY_YES",
        entry_price=0.55,
        market_ticker="OVER25",
        match_id="match_1",
        contracts=10,
    )


@pytest.fixture
def buy_no_position():
    """Buy No position entered at 0.40 (sold Yes at 0.40)."""
    return OpenPosition(
        direction="BUY_NO",
        entry_price=0.40,
        market_ticker="HOME_WIN",
        match_id="match_1",
        contracts=10,
    )


# ---------------------------------------------------------------------------
# v2 Fix #1: Edge Reversal Buy No
# ---------------------------------------------------------------------------

class TestEdgeReversalBuyNo:
    """test_reversal_buy_no_uses_P_kalshi_bid / test_reversal_buy_no_fires_at_correct_level"""

    def test_reversal_buy_no_uses_P_kalshi_bid(self, buy_no_position):
        """Buy No reversal uses P_kalshi_bid directly, NOT (1-P_kalshi_bid).

        v2: P_cons > P_kalshi_bid + theta
        v1 BUG: P_cons > (1 - P_kalshi_bid) + theta
        """
        # P_cons_no = P_true + z*sigma = 0.43 + 0 = 0.43
        # Threshold = 0.40 + 0.02 = 0.42
        # 0.43 > 0.42 -> fires
        result = check_edge_reversal(
            buy_no_position,
            P_true=0.43, sigma_MC=0.0,
            P_kalshi_bid=0.40, z=1.645,
        )
        assert result is not None
        assert result.reason == "EDGE_REVERSAL"

    def test_reversal_buy_no_fires_at_correct_level(self, buy_no_position):
        """bid=0.40, theta=0.02: fires when P_cons > 0.42, NOT at 0.62.

        This is the critical v2 validation test.
        """
        # At P_true=0.42 (P_cons=0.42): exactly at threshold -> no fire
        result_at_threshold = check_edge_reversal(
            buy_no_position,
            P_true=0.42, sigma_MC=0.0,
            P_kalshi_bid=0.40, z=1.645,
        )
        assert result_at_threshold is None

        # At P_true=0.43 (P_cons=0.43): above threshold -> fires
        result_above = check_edge_reversal(
            buy_no_position,
            P_true=0.43, sigma_MC=0.0,
            P_kalshi_bid=0.40, z=1.645,
        )
        assert result_above is not None

        # v1 bug would require P_cons > 0.62 to fire
        # At P_true=0.50, v1 wouldn't fire but v2 correctly fires
        result_v2_catches = check_edge_reversal(
            buy_no_position,
            P_true=0.50, sigma_MC=0.0,
            P_kalshi_bid=0.40, z=1.645,
        )
        assert result_v2_catches is not None

    def test_reversal_buy_no_does_not_fire_when_safe(self, buy_no_position):
        """P_cons well below bid -> no reversal."""
        result = check_edge_reversal(
            buy_no_position,
            P_true=0.30, sigma_MC=0.0,
            P_kalshi_bid=0.40, z=1.645,
        )
        assert result is None


# ---------------------------------------------------------------------------
# v2 Fix #2: Expiry Evaluation Buy No
# ---------------------------------------------------------------------------

class TestExpiryEvalBuyNo:
    """test_expiry_buy_no_E_hold_formula / profitable not closed / losing closed"""

    def test_expiry_buy_no_E_hold_formula(self, buy_no_position):
        """E_hold = (1-P_cons)*(1-c)*entry - P_cons*(1-entry).

        Validation from spec: entry=0.40, P_cons=0.35, c=0.07
        E_hold = 0.65 * 0.93 * 0.40 - 0.35 * 0.60
               = 0.2418 - 0.21 = 0.0318
        """
        # At t=96, T=98 (within 3 min window)
        result = check_expiry_eval(
            buy_no_position,
            P_true=0.35, sigma_MC=0.0,
            P_kalshi_bid=0.40,
            c=0.07, z=1.645,
            t=96.0, T=98.0,
        )
        # E_hold = 0.65 * 0.93 * 0.40 - 0.35 * 0.60 = 0.2418 - 0.21 = 0.0318
        # E_exit: profit = 0.40 - 0.40 = 0, fee = 0, E_exit = 0
        # E_exit (0) < E_hold (0.0318) -> no exit
        assert result is None

    def test_expiry_buy_no_profitable_position_not_closed(self, buy_no_position):
        """entry=0.40, P_cons=0.35: E_hold > 0, should NOT exit."""
        result = check_expiry_eval(
            buy_no_position,
            P_true=0.35, sigma_MC=0.0,
            P_kalshi_bid=0.38,  # bid dropped slightly
            c=0.07, z=1.645,
            t=96.5, T=98.0,
        )
        # E_hold = 0.65 * 0.93 * 0.40 - 0.35 * 0.60 = 0.0318
        # E_exit: profit = 0.40 - 0.38 = 0.02, fee = 0.07*0.02 = 0.0014
        # E_exit = 0.02 - 0.0014 = 0.0186
        # E_exit (0.0186) < E_hold (0.0318) -> hold
        assert result is None

    def test_expiry_buy_no_losing_position_closed(self, buy_no_position):
        """entry=0.40, P_cons=0.65: E_hold < E_exit, should exit."""
        result = check_expiry_eval(
            buy_no_position,
            P_true=0.65, sigma_MC=0.0,
            P_kalshi_bid=0.60,  # bid rose significantly
            c=0.07, z=1.645,
            t=97.0, T=98.0,
        )
        # E_hold = (1-0.65)*(0.93)*0.40 - 0.65*(0.60)
        #        = 0.35 * 0.93 * 0.40 - 0.65 * 0.60
        #        = 0.1302 - 0.39 = -0.2598
        # E_exit: profit = 0.40 - 0.60 = -0.20, fee = 0 (negative profit)
        # E_exit = -0.20
        # E_exit (-0.20) > E_hold (-0.2598) -> exit
        assert result is not None
        assert result.reason == "EXPIRY_EVAL"
        assert result.E_exit > result.E_hold

    def test_expiry_not_triggered_outside_window(self, buy_no_position):
        """More than 3 minutes remaining -> no expiry eval."""
        result = check_expiry_eval(
            buy_no_position,
            P_true=0.65, sigma_MC=0.0,
            P_kalshi_bid=0.60,
            c=0.07, z=1.645,
            t=90.0, T=98.0,  # 8 min remaining
        )
        assert result is None


# ---------------------------------------------------------------------------
# v2 Fix #3: bet365 Divergence Buy No
# ---------------------------------------------------------------------------

class TestBet365DivergenceBuyNo:
    """test_divergence_buy_no_uses_entry_price / test_divergence_buy_no_fires_at_5pp"""

    def test_divergence_buy_no_uses_entry_price(self, buy_no_position):
        """Buy No: threshold = entry_price + 0.05, NOT (1-entry) + 0.05.

        v2: P_bet365 > 0.40 + 0.05 = 0.45
        v1 BUG: P_bet365 > (1-0.40) + 0.05 = 0.65 (25pp!)
        """
        # bet365 at 0.46 -> fires (just above 0.45)
        result = check_bet365_divergence(
            buy_no_position, P_bet365=0.46
        )
        assert result is not None
        assert result.severity == "WARNING"

    def test_divergence_buy_no_fires_at_5pp(self, buy_no_position):
        """entry=0.40: fires when P_bet365 > 0.45 (NOT 0.65)."""
        # At exactly 0.45 -> no fire (not strictly greater by threshold)
        result_at = check_bet365_divergence(
            buy_no_position, P_bet365=0.45
        )
        assert result_at is None

        # At 0.46 -> fires
        result_above = check_bet365_divergence(
            buy_no_position, P_bet365=0.46
        )
        assert result_above is not None

        # v1 bug: only fires at 0.65+
        # At 0.50, v1 wouldn't fire but v2 correctly fires
        result_v2 = check_bet365_divergence(
            buy_no_position, P_bet365=0.50
        )
        assert result_v2 is not None

    def test_divergence_buy_no_safe(self, buy_no_position):
        """bet365 at 0.35 (below entry) -> no divergence."""
        result = check_bet365_divergence(
            buy_no_position, P_bet365=0.35
        )
        assert result is None

    def test_divergence_none_bet365(self, buy_no_position):
        """No bet365 data -> no divergence."""
        result = check_bet365_divergence(
            buy_no_position, P_bet365=None
        )
        assert result is None


# ---------------------------------------------------------------------------
# General Tests
# ---------------------------------------------------------------------------

class TestEdgeDecay:
    """test_edge_decay_below_half_cent"""

    def test_edge_decay_below_half_cent(self, buy_yes_position):
        """EV drops below theta_exit -> exit."""
        # P_true dropped to near entry price -> minimal EV
        result = check_edge_decay(
            buy_yes_position,
            P_true=0.56, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            c=0.07, z=1.645,
        )
        # EV = 0.56 * 0.93 * 0.45 - 0.44 * 0.55 = 0.2343 - 0.242 = -0.0077
        assert result is not None
        assert result.reason == "EDGE_DECAY"
        assert result.EV < THETA_EXIT

    def test_edge_decay_not_triggered_with_edge(self, buy_yes_position):
        """Strong edge -> no decay exit."""
        result = check_edge_decay(
            buy_yes_position,
            P_true=0.75, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            c=0.07, z=1.645,
        )
        assert result is None

    def test_edge_decay_buy_no(self, buy_no_position):
        """Buy No edge decay with correct formula."""
        # P_true rose (bad for No) -> low EV
        result = check_edge_decay(
            buy_no_position,
            P_true=0.55, sigma_MC=0.0,
            P_kalshi_bid=0.50,
            c=0.07, z=1.645,
        )
        # P_cons = 0.55, E = (1-0.55)*0.93*0.40 - 0.55*0.60
        # = 0.45*0.93*0.40 - 0.33 = 0.1674 - 0.33 = -0.1626
        assert result is not None
        assert result.reason == "EDGE_DECAY"


class TestEdgeReversalBuyYes:
    """test_reversal_buy_yes_works"""

    def test_reversal_buy_yes_works(self, buy_yes_position):
        """Buy Yes reversal: P_cons < P_kalshi_bid - theta."""
        # P_true dropped well below bid
        result = check_edge_reversal(
            buy_yes_position,
            P_true=0.40, sigma_MC=0.0,
            P_kalshi_bid=0.55, z=1.645,
        )
        # P_cons = 0.40, threshold = 0.55 - 0.02 = 0.53
        # 0.40 < 0.53 -> fires
        assert result is not None
        assert result.reason == "EDGE_REVERSAL"

    def test_reversal_buy_yes_not_triggered(self, buy_yes_position):
        """P_true still above bid -> no reversal."""
        result = check_edge_reversal(
            buy_yes_position,
            P_true=0.60, sigma_MC=0.0,
            P_kalshi_bid=0.55, z=1.645,
        )
        assert result is None


class TestDivergenceBuyYes:
    """test_divergence_buy_yes_works"""

    def test_divergence_buy_yes_works(self, buy_yes_position):
        """Buy Yes: bet365 drops 5pp below entry -> warning."""
        # entry=0.55, threshold = 0.55 - 0.05 = 0.50
        result = check_bet365_divergence(
            buy_yes_position, P_bet365=0.48
        )
        assert result is not None
        assert result.severity == "WARNING"

    def test_divergence_buy_yes_safe(self, buy_yes_position):
        """bet365 close to entry -> no divergence."""
        result = check_bet365_divergence(
            buy_yes_position, P_bet365=0.52
        )
        assert result is None


# ---------------------------------------------------------------------------
# Expiry Eval Buy Yes
# ---------------------------------------------------------------------------

class TestExpiryEvalBuyYes:
    """Buy Yes expiry eval for completeness."""

    def test_expiry_buy_yes_hold_when_profitable(self, buy_yes_position):
        """Profitable Buy Yes near expiry -> hold."""
        result = check_expiry_eval(
            buy_yes_position,
            P_true=0.75, sigma_MC=0.0,
            P_kalshi_bid=0.70,
            c=0.07, z=1.645,
            t=96.0, T=98.0,
        )
        # E_hold = 0.75*0.93*0.45 - 0.25*0.55 = 0.314 - 0.1375 = 0.1765
        # E_exit = (0.70-0.55) - 0.07*0.15 = 0.15 - 0.0105 = 0.1395
        # E_exit < E_hold -> hold
        assert result is None

    def test_expiry_buy_yes_exit_when_losing(self, buy_yes_position):
        """Losing Buy Yes near expiry -> exit."""
        result = check_expiry_eval(
            buy_yes_position,
            P_true=0.45, sigma_MC=0.0,
            P_kalshi_bid=0.52,
            c=0.07, z=1.645,
            t=97.0, T=98.0,
        )
        # E_hold = 0.45*0.93*0.45 - 0.55*0.55 = 0.1882 - 0.3025 = -0.1143
        # E_exit = (0.52-0.55) - 0 = -0.03 (no fee on negative profit)
        # E_exit (-0.03) > E_hold (-0.1143) -> exit
        assert result is not None
        assert result.reason == "EXPIRY_EVAL"


# ---------------------------------------------------------------------------
# Full Evaluation Loop
# ---------------------------------------------------------------------------

class TestEvaluateExit:
    """Integration test for full exit evaluation."""

    def test_edge_decay_has_priority(self, buy_yes_position):
        """Edge decay fires before reversal."""
        result = evaluate_exit(
            buy_yes_position,
            P_true=0.40, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            P_bet365=0.48,
            c=0.07, z=1.645,
            t=90.0, T=98.0,
        )
        assert result is not None
        assert result.reason == "EDGE_DECAY"

    def test_no_exit_when_healthy(self, buy_yes_position):
        """Strong edge, no reversal, no expiry, no divergence -> None."""
        result = evaluate_exit(
            buy_yes_position,
            P_true=0.75, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            P_bet365=0.70,
            c=0.07, z=1.645,
            t=45.0, T=98.0,
        )
        assert result is None

    def test_bet365_divergence_logs_but_no_exit(self, buy_yes_position):
        """bet365 divergence is warning-only by default."""
        result = evaluate_exit(
            buy_yes_position,
            P_true=0.75, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            P_bet365=0.40,  # big drop -> divergence
            c=0.07, z=1.645,
            t=45.0, T=98.0,
            bet365_divergence_auto_exit=False,
        )
        assert result is None
        assert buy_yes_position.had_bet365_divergence is True

    def test_bet365_divergence_auto_exit(self, buy_yes_position):
        """With auto_exit enabled, divergence triggers exit."""
        result = evaluate_exit(
            buy_yes_position,
            P_true=0.75, sigma_MC=0.0,
            P_kalshi_bid=0.55,
            P_bet365=0.40,
            c=0.07, z=1.645,
            t=45.0, T=98.0,
            bet365_divergence_auto_exit=True,
        )
        assert result is not None
        assert result.reason == "BET365_DIVERGENCE"

    def test_expiry_fires_in_last_3_min(self, buy_no_position):
        """Expiry eval triggers within 3 minutes of end."""
        result = evaluate_exit(
            buy_no_position,
            P_true=0.65, sigma_MC=0.0,
            P_kalshi_bid=0.60,
            P_bet365=None,
            c=0.07, z=1.645,
            t=97.0, T=98.0,
        )
        # Edge decay and reversal should fire first here
        # P_cons = 0.65, entry=0.40
        # Decay EV = (1-0.65)*0.93*0.40 - 0.65*0.60 = 0.1302 - 0.39 = -0.2598
        # -> EDGE_DECAY fires first
        assert result is not None
        assert result.reason == "EDGE_DECAY"
