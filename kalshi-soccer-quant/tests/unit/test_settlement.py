"""Tests for Step 4.6: Settlement (v2 Fix #8).

Verifies directional settlement P&L, fee-on-profit-only,
and that Buy No settlement is NOT inverted.

These tests validate v2 fix #8 — the most critical accounting fix.
v1 BUG: Buy No P&L completely inverted, contaminating all metrics.

Reference: implementation_roadmap.md -> Step 4.6 tests
"""

from __future__ import annotations

import pytest

from src.analytics.metrics import (
    PostAnalysisSummary,
    SettledPosition,
    analyze_alignment_effect,
    analyze_bet365_divergence,
    analyze_directional_cons,
    compute_edge_realization,
    compute_match_pnl,
    compute_post_analysis,
    compute_realized_pnl,
    settle_position,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FEE_RATE = 0.07


@pytest.fixture
def buy_yes_win():
    """Buy Yes, Yes wins (settlement=1.00)."""
    return SettledPosition(
        direction="BUY_YES", entry_price=0.45, quantity=10,
        market_ticker="OVER25", EV_at_entry=0.05,
    )


@pytest.fixture
def buy_yes_lose():
    """Buy Yes, Yes loses (settlement=0.00)."""
    return SettledPosition(
        direction="BUY_YES", entry_price=0.45, quantity=10,
        market_ticker="OVER25", EV_at_entry=0.05,
    )


@pytest.fixture
def buy_no_win():
    """Buy No, No wins = Yes loses (settlement=0.00)."""
    return SettledPosition(
        direction="BUY_NO", entry_price=0.40, quantity=10,
        market_ticker="HOME_WIN", EV_at_entry=0.04,
    )


@pytest.fixture
def buy_no_lose():
    """Buy No, No loses = Yes wins (settlement=1.00)."""
    return SettledPosition(
        direction="BUY_NO", entry_price=0.40, quantity=10,
        market_ticker="HOME_WIN", EV_at_entry=0.04,
    )


# ---------------------------------------------------------------------------
# Roadmap Tests — Settlement P&L
# ---------------------------------------------------------------------------

class TestBuyYesWinIsProfit:
    """test_buy_yes_win_is_profit: (1.00 - 0.45) * qty > 0"""

    def test_buy_yes_win_is_profit(self):
        pnl, fee = compute_realized_pnl("BUY_YES", 0.45, 1.00, 10, FEE_RATE)
        # gross = (1.00 - 0.45) * 10 = 5.50
        # fee = 0.07 * 5.50 = 0.385
        # net = 5.50 - 0.385 = 5.115
        assert pnl > 0
        assert pnl == pytest.approx(5.50 - 0.07 * 5.50)

    def test_buy_yes_win_gross_pnl(self):
        pnl, fee = compute_realized_pnl("BUY_YES", 0.45, 1.00, 10, 0.0)
        assert pnl == pytest.approx(5.50)


class TestBuyYesLoseIsLoss:
    """test_buy_yes_lose_is_loss: (0.00 - 0.45) * qty < 0"""

    def test_buy_yes_lose_is_loss(self):
        pnl, fee = compute_realized_pnl("BUY_YES", 0.45, 0.00, 10, FEE_RATE)
        # gross = (0.00 - 0.45) * 10 = -4.50
        # fee = 0 (negative pnl)
        # net = -4.50
        assert pnl < 0
        assert pnl == pytest.approx(-4.50)
        assert fee == 0.0


class TestBuyNoWinIsProfit:
    """test_buy_no_win_is_profit: (0.40 - 0.00) * qty > 0  [v2 fix]"""

    def test_buy_no_win_is_profit(self):
        pnl, fee = compute_realized_pnl("BUY_NO", 0.40, 0.00, 10, FEE_RATE)
        # gross = (0.40 - 0.00) * 10 = 4.00
        # fee = 0.07 * 4.00 = 0.28
        # net = 4.00 - 0.28 = 3.72
        assert pnl > 0
        assert pnl == pytest.approx(4.00 - 0.07 * 4.00)

    def test_buy_no_win_v1_would_be_negative(self):
        """v1 BUG: (settlement - entry) * qty = (0.00 - 0.40) * 10 = -4.00."""
        # v1 formula: (0.00 - 0.40) * 10 = -4.00 (WRONG - shows loss)
        v1_result = (0.00 - 0.40) * 10
        assert v1_result < 0  # v1 is wrong

        # v2 formula: (0.40 - 0.00) * 10 = 4.00 (CORRECT - shows profit)
        pnl, _ = compute_realized_pnl("BUY_NO", 0.40, 0.00, 10, 0.0)
        assert pnl > 0  # v2 is correct


class TestBuyNoLoseIsLoss:
    """test_buy_no_lose_is_loss: (0.40 - 1.00) * qty < 0  [v2 fix]"""

    def test_buy_no_lose_is_loss(self):
        pnl, fee = compute_realized_pnl("BUY_NO", 0.40, 1.00, 10, FEE_RATE)
        # gross = (0.40 - 1.00) * 10 = -6.00
        # fee = 0 (negative pnl)
        # net = -6.00
        assert pnl < 0
        assert pnl == pytest.approx(-6.00)
        assert fee == 0.0

    def test_buy_no_lose_v1_would_be_positive(self):
        """v1 BUG: (settlement - entry) * qty = (1.00 - 0.40) * 10 = +6.00."""
        v1_result = (1.00 - 0.40) * 10
        assert v1_result > 0  # v1 is wrong

        pnl, _ = compute_realized_pnl("BUY_NO", 0.40, 1.00, 10, 0.0)
        assert pnl < 0  # v2 is correct


class TestFeeOnlyOnPositive:
    """test_fee_only_on_positive"""

    def test_fee_on_profit(self):
        """Fee deducted when gross P&L is positive."""
        pnl, fee = compute_realized_pnl("BUY_YES", 0.45, 1.00, 10, 0.07)
        assert fee > 0
        assert fee == pytest.approx(0.07 * 5.50)

    def test_no_fee_on_loss(self):
        """No fee when gross P&L is negative."""
        pnl, fee = compute_realized_pnl("BUY_YES", 0.45, 0.00, 10, 0.07)
        assert fee == 0.0

    def test_no_fee_on_zero_pnl(self):
        """No fee when gross P&L is zero."""
        pnl, fee = compute_realized_pnl("BUY_YES", 0.50, 0.50, 10, 0.07)
        assert fee == 0.0
        assert pnl == 0.0

    def test_buy_no_fee_on_profit(self):
        """Buy No: fee only on profit (No wins)."""
        pnl, fee = compute_realized_pnl("BUY_NO", 0.40, 0.00, 10, 0.07)
        assert fee > 0  # profit, so fee charged
        _, fee_loss = compute_realized_pnl("BUY_NO", 0.40, 1.00, 10, 0.07)
        assert fee_loss == 0.0  # loss, no fee


class TestBuyNoSettlementNotInverted:
    """test_buy_no_settlement_not_inverted — explicitly verify no sign flip."""

    def test_buy_no_settlement_not_inverted(self):
        """The 4-case validation table from the spec."""
        # Case 1: Buy Yes, win -> positive
        pnl1, _ = compute_realized_pnl("BUY_YES", 0.45, 1.00, 1, 0.0)
        assert pnl1 == pytest.approx(0.55)

        # Case 2: Buy Yes, lose -> negative
        pnl2, _ = compute_realized_pnl("BUY_YES", 0.45, 0.00, 1, 0.0)
        assert pnl2 == pytest.approx(-0.45)

        # Case 3: Buy No, No wins (Yes=0) -> positive
        pnl3, _ = compute_realized_pnl("BUY_NO", 0.40, 0.00, 1, 0.0)
        assert pnl3 == pytest.approx(0.40)

        # Case 4: Buy No, No loses (Yes=1) -> negative
        pnl4, _ = compute_realized_pnl("BUY_NO", 0.40, 1.00, 1, 0.0)
        assert pnl4 == pytest.approx(-0.60)

    def test_buy_no_signs_are_opposite_of_v1(self):
        """v2 Buy No results are opposite sign to v1 formula."""
        entry = 0.40
        qty = 10

        # No wins (settlement=0.00)
        v2_win, _ = compute_realized_pnl("BUY_NO", entry, 0.00, qty, 0.0)
        v1_win = (0.00 - entry) * qty  # v1 formula
        assert v2_win > 0 and v1_win < 0  # opposite signs

        # No loses (settlement=1.00)
        v2_lose, _ = compute_realized_pnl("BUY_NO", entry, 1.00, qty, 0.0)
        v1_lose = (1.00 - entry) * qty  # v1 formula
        assert v2_lose < 0 and v1_lose > 0  # opposite signs


# ---------------------------------------------------------------------------
# settle_position helper
# ---------------------------------------------------------------------------

class TestSettlePosition:
    """Test the settle_position wrapper."""

    def test_settle_populates_fields(self, buy_yes_win):
        settled = settle_position(buy_yes_win, 1.00, FEE_RATE)
        assert settled.settlement_price == 1.00
        assert settled.realized_pnl > 0
        assert settled.fee_paid > 0

    def test_settle_buy_no(self, buy_no_win):
        settled = settle_position(buy_no_win, 0.00, FEE_RATE)
        assert settled.realized_pnl > 0  # No wins


# ---------------------------------------------------------------------------
# Post-Analysis Metrics
# ---------------------------------------------------------------------------

class TestMatchPnl:
    """Metric 1: Match-level P&L."""

    def test_match_pnl_sum(self):
        positions = [
            SettledPosition(direction="BUY_YES", entry_price=0.45,
                            quantity=10, realized_pnl=5.0),
            SettledPosition(direction="BUY_NO", entry_price=0.40,
                            quantity=10, realized_pnl=-3.0),
        ]
        assert compute_match_pnl(positions) == pytest.approx(2.0)

    def test_empty(self):
        assert compute_match_pnl([]) == 0.0


class TestAlignmentEffect:
    """Metric 7: Market alignment value."""

    def test_aligned_vs_divergent(self):
        positions = [
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=2.0,
                            alignment_status="ALIGNED"),
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=1.0,
                            alignment_status="ALIGNED"),
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=-1.0,
                            alignment_status="DIVERGENT"),
        ]
        result = analyze_alignment_effect(positions)
        assert result["aligned_avg_return"] == pytest.approx(1.5)
        assert result["divergent_avg_return"] == pytest.approx(-1.0)
        assert result["alignment_value"] == pytest.approx(2.5)
        assert result["aligned_count"] == 2
        assert result["divergent_count"] == 1


class TestDirectionalCons:
    """Metric 8: Directional P_cons analysis."""

    def test_yes_vs_no_counts(self):
        positions = [
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=1.0, EV_at_entry=0.05),
            SettledPosition(direction="BUY_NO", entry_price=0.50,
                            quantity=10, realized_pnl=-0.5, EV_at_entry=0.04),
        ]
        result = analyze_directional_cons(positions)
        assert result["yes_count"] == 1
        assert result["no_count"] == 1


class TestBet365DivergenceAnalysis:
    """Metric 11: bet365 divergence analysis."""

    def test_divergence_effectiveness(self):
        positions = [
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=-2.0,
                            had_bet365_divergence=True),
            SettledPosition(direction="BUY_YES", entry_price=0.50,
                            quantity=10, realized_pnl=3.0,
                            had_bet365_divergence=False),
        ]
        result = analyze_bet365_divergence(positions)
        assert result["divergence_avg_pnl"] < 0
        assert result["no_divergence_avg_pnl"] > 0
        assert result["divergence_count"] == 1


class TestPostAnalysisSummary:
    """Full post-analysis integration."""

    def test_full_summary(self):
        p1 = SettledPosition(
            direction="BUY_YES", entry_price=0.45, quantity=10,
            EV_at_entry=0.05, alignment_status="ALIGNED",
        )
        settle_position(p1, 1.00, 0.07)

        p2 = SettledPosition(
            direction="BUY_NO", entry_price=0.40, quantity=10,
            EV_at_entry=0.04, alignment_status="ALIGNED",
        )
        settle_position(p2, 0.00, 0.07)

        summary = compute_post_analysis([p1, p2])
        assert summary.total_trades == 2
        assert summary.total_pnl > 0  # both won
        assert summary.win_rate == 1.0
        assert summary.max_drawdown == 0.0  # no drawdown (both positive)

    def test_empty_summary(self):
        summary = compute_post_analysis([])
        assert summary.total_trades == 0
        assert summary.total_pnl == 0.0
