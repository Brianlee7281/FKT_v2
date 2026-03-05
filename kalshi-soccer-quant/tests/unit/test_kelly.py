"""Tests for Step 4.3: Kelly + Risk Limits.

Verifies directional W/L payoffs, fractional Kelly, alignment multiplier,
3-layer risk limits, and match cap pro-rata scaling.

Reference: implementation_roadmap.md -> Step 4.3 tests
"""

from __future__ import annotations

import pytest

from src.trading.step_4_2_edge_detection import Signal
from src.trading.step_4_3_position_sizing import (
    apply_match_cap_pro_rata,
    compute_contracts,
    compute_kelly,
    compute_kelly_W_L,
)
from src.trading.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def buy_yes_signal():
    """Buy Yes signal with typical values."""
    return Signal(
        direction="BUY_YES",
        EV=0.05,
        P_cons=0.65,
        P_kalshi=0.55,
        rough_qty=10,
        alignment_status="ALIGNED",
        kelly_multiplier=0.8,
        market_ticker="OVER25",
    )


@pytest.fixture
def buy_no_signal():
    """Buy No signal with typical values."""
    return Signal(
        direction="BUY_NO",
        EV=0.04,
        P_cons=0.35,
        P_kalshi=0.55,
        rough_qty=8,
        alignment_status="ALIGNED",
        kelly_multiplier=0.8,
        market_ticker="HOME_WIN",
    )


# ---------------------------------------------------------------------------
# W/L Payoff Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestWLPayoffs:
    """test_buy_yes_W_L_correct / test_buy_no_W / test_buy_no_L"""

    def test_buy_yes_W_L_correct(self):
        """Buy Yes: W = (1-c)*(1-P_kalshi), L = P_kalshi."""
        P_kalshi = 0.55
        c = 0.07
        W, L = compute_kelly_W_L("BUY_YES", P_kalshi, c)
        assert W == pytest.approx((1 - c) * (1 - P_kalshi))
        assert L == pytest.approx(P_kalshi)

    def test_buy_no_W_is_Pkalshi_times_1minusc(self):
        """Buy No: W = (1-c)*P_kalshi."""
        P_kalshi = 0.55
        c = 0.07
        W, L = compute_kelly_W_L("BUY_NO", P_kalshi, c)
        assert W == pytest.approx((1 - c) * P_kalshi)

    def test_buy_no_L_is_1_minus_Pkalshi(self):
        """Buy No: L = 1 - P_kalshi."""
        P_kalshi = 0.55
        c = 0.07
        W, L = compute_kelly_W_L("BUY_NO", P_kalshi, c)
        assert L == pytest.approx(1 - P_kalshi)

    def test_hold_returns_zero(self):
        """HOLD direction -> W=0, L=0."""
        W, L = compute_kelly_W_L("HOLD", 0.55, 0.07)
        assert W == 0.0
        assert L == 0.0

    def test_buy_yes_W_L_numerical(self):
        """Verify exact numerical values: P=0.60, c=0.07."""
        W, L = compute_kelly_W_L("BUY_YES", 0.60, 0.07)
        assert W == pytest.approx(0.93 * 0.40)  # 0.372
        assert L == pytest.approx(0.60)

    def test_buy_no_W_L_numerical(self):
        """Verify exact numerical values: P=0.60, c=0.07."""
        W, L = compute_kelly_W_L("BUY_NO", 0.60, 0.07)
        assert W == pytest.approx(0.93 * 0.60)  # 0.558
        assert L == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# Kelly Fraction Tests
# ---------------------------------------------------------------------------

class TestComputeKelly:
    """Kelly fraction computation with alignment multiplier."""

    def test_kelly_formula(self, buy_yes_signal):
        """f = K_frac * (EV / (W*L)) * kelly_multiplier."""
        c = 0.07
        K_frac = 0.25

        f = compute_kelly(buy_yes_signal, c, K_frac)

        W, L = compute_kelly_W_L("BUY_YES", buy_yes_signal.P_kalshi, c)
        f_kelly = buy_yes_signal.EV / (W * L)
        expected = K_frac * f_kelly * buy_yes_signal.kelly_multiplier
        assert f == pytest.approx(expected)

    def test_alignment_multiplier_applied(self, buy_yes_signal):
        """Alignment multiplier affects final fraction."""
        c = 0.07
        K_frac = 0.25

        # ALIGNED (0.8)
        f_aligned = compute_kelly(buy_yes_signal, c, K_frac)

        # DIVERGENT (0.5)
        buy_yes_signal.kelly_multiplier = 0.5
        f_divergent = compute_kelly(buy_yes_signal, c, K_frac)

        assert f_aligned > f_divergent
        assert f_aligned / f_divergent == pytest.approx(0.8 / 0.5)

    def test_buy_no_kelly(self, buy_no_signal):
        """Buy No Kelly uses correct W/L."""
        c = 0.07
        K_frac = 0.25

        f = compute_kelly(buy_no_signal, c, K_frac)
        assert f > 0

        W, L = compute_kelly_W_L("BUY_NO", buy_no_signal.P_kalshi, c)
        f_kelly = buy_no_signal.EV / (W * L)
        expected = K_frac * f_kelly * buy_no_signal.kelly_multiplier
        assert f == pytest.approx(expected)

    def test_hold_returns_zero(self):
        """HOLD signal -> f = 0."""
        sig = Signal(direction="HOLD")
        assert compute_kelly(sig, 0.07, 0.25) == 0.0

    def test_zero_EV_returns_zero(self, buy_yes_signal):
        """Zero EV -> f = 0."""
        buy_yes_signal.EV = 0.0
        assert compute_kelly(buy_yes_signal, 0.07, 0.25) == 0.0

    def test_negative_EV_returns_zero(self, buy_yes_signal):
        """Negative EV -> f = 0."""
        buy_yes_signal.EV = -0.05
        assert compute_kelly(buy_yes_signal, 0.07, 0.25) == 0.0


# ---------------------------------------------------------------------------
# Contract Computation Tests
# ---------------------------------------------------------------------------

class TestComputeContracts:
    """Converting f_invest to contract count."""

    def test_basic_computation(self):
        """10% of $1000 at $0.50 = 200 contracts."""
        assert compute_contracts(0.10, 1000.0, 0.50) == 200

    def test_floor_rounding(self):
        """Contracts are floored, not rounded."""
        # 3% of $5000 at $0.57 = 150/0.57 = 263.15 -> 263
        assert compute_contracts(0.03, 5000.0, 0.57) == 263

    def test_zero_price(self):
        """Zero price -> 0 contracts."""
        assert compute_contracts(0.10, 1000.0, 0.0) == 0

    def test_zero_fraction(self):
        """Zero fraction -> 0 contracts."""
        assert compute_contracts(0.0, 1000.0, 0.55) == 0


# ---------------------------------------------------------------------------
# Match Cap Pro-Rata Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestMatchCapProRata:
    """test_match_cap_pro_rata — scaling when multiple markets in same match."""

    def test_under_cap_no_scaling(self):
        """Under match cap -> no scaling."""
        f_invests = {"OVER25": 0.02, "HOME_WIN": 0.01}
        result = apply_match_cap_pro_rata(f_invests, f_match_cap=0.05)
        assert result["OVER25"] == pytest.approx(0.02)
        assert result["HOME_WIN"] == pytest.approx(0.01)

    def test_over_cap_scales_proportionally(self):
        """Over match cap -> scale proportionally."""
        f_invests = {"OVER25": 0.04, "HOME_WIN": 0.03, "BTTS": 0.03}
        # Total = 0.10, cap = 0.05, scale = 0.5
        result = apply_match_cap_pro_rata(f_invests, f_match_cap=0.05)
        assert result["OVER25"] == pytest.approx(0.02)
        assert result["HOME_WIN"] == pytest.approx(0.015)
        assert result["BTTS"] == pytest.approx(0.015)
        assert sum(result.values()) == pytest.approx(0.05)

    def test_exactly_at_cap(self):
        """Exactly at cap -> no scaling."""
        f_invests = {"OVER25": 0.025, "HOME_WIN": 0.025}
        result = apply_match_cap_pro_rata(f_invests, f_match_cap=0.05)
        assert result["OVER25"] == pytest.approx(0.025)

    def test_single_market_capped(self):
        """Single market exceeding cap."""
        f_invests = {"OVER25": 0.08}
        result = apply_match_cap_pro_rata(f_invests, f_match_cap=0.05)
        assert result["OVER25"] == pytest.approx(0.05)

    def test_empty_dict(self):
        """Empty dict -> empty dict."""
        result = apply_match_cap_pro_rata({}, f_match_cap=0.05)
        assert result == {}


# ---------------------------------------------------------------------------
# 3-Layer Risk Limits Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestRiskManager:
    """test_3_layer_limits_enforced"""

    def test_layer1_order_cap(self):
        """Single order capped at f_order_cap (3%)."""
        rm = RiskManager(f_order_cap=0.03)
        bankroll = 10000.0
        # Propose 10% -> capped to 3%
        amount = rm.apply_risk_limits(0.10, "match_1", bankroll)
        assert amount == pytest.approx(300.0)  # 3% of 10k

    def test_layer2_match_cap(self):
        """Per-match exposure capped at f_match_cap (5%)."""
        rm = RiskManager(f_order_cap=0.10, f_match_cap=0.05)
        bankroll = 10000.0
        # Already 400 in match -> remaining = 500-400 = 100
        rm.record_exposure("match_1", 400.0)
        amount = rm.apply_risk_limits(0.03, "match_1", bankroll)
        assert amount == pytest.approx(100.0)

    def test_layer3_total_cap(self):
        """Total portfolio capped at f_total_cap (20%)."""
        rm = RiskManager(f_order_cap=0.10, f_match_cap=0.10, f_total_cap=0.20)
        bankroll = 10000.0
        # Already 1800 total -> remaining = 2000-1800 = 200
        rm.record_exposure("match_1", 1000.0)
        rm.record_exposure("match_2", 800.0)
        amount = rm.apply_risk_limits(0.05, "match_3", bankroll)
        assert amount == pytest.approx(200.0)

    def test_all_layers_interact(self):
        """Most restrictive layer wins."""
        rm = RiskManager(f_order_cap=0.03, f_match_cap=0.05, f_total_cap=0.20)
        bankroll = 10000.0
        # Layer 1: 3% = 300
        # Layer 2: match has 0 -> remaining 500
        # Layer 3: total has 0 -> remaining 2000
        # Min = 300 (Layer 1)
        amount = rm.apply_risk_limits(0.10, "match_1", bankroll)
        assert amount == pytest.approx(300.0)

    def test_match_full_returns_zero(self):
        """Match at capacity -> 0."""
        rm = RiskManager(f_match_cap=0.05)
        bankroll = 10000.0
        rm.record_exposure("match_1", 500.0)
        amount = rm.apply_risk_limits(0.03, "match_1", bankroll)
        assert amount == 0.0

    def test_portfolio_full_returns_zero(self):
        """Portfolio at capacity -> 0."""
        rm = RiskManager(f_total_cap=0.20)
        bankroll = 10000.0
        rm.record_exposure("match_1", 2000.0)
        amount = rm.apply_risk_limits(0.03, "match_2", bankroll)
        assert amount == 0.0

    def test_record_and_remove_exposure(self):
        """Exposure tracking works correctly."""
        rm = RiskManager()
        rm.record_exposure("m1", 100.0)
        rm.record_exposure("m1", 50.0)
        assert rm.get_match_exposure("m1") == 150.0
        assert rm.get_total_exposure() == 150.0

        rm.remove_exposure("m1", 80.0)
        assert rm.get_match_exposure("m1") == 70.0

    def test_remove_cleans_up(self):
        """Removing all exposure for a match cleans the dict."""
        rm = RiskManager()
        rm.record_exposure("m1", 100.0)
        rm.remove_exposure("m1", 100.0)
        assert rm.get_match_exposure("m1") == 0.0
        assert "m1" not in rm.match_exposures

    def test_reset(self):
        """Reset clears all exposures."""
        rm = RiskManager()
        rm.record_exposure("m1", 100.0)
        rm.record_exposure("m2", 200.0)
        rm.reset()
        assert rm.get_total_exposure() == 0.0
        assert rm.match_exposures == {}

    def test_zero_bankroll(self):
        """Zero bankroll -> 0."""
        rm = RiskManager()
        assert rm.apply_risk_limits(0.05, "m1", 0.0) == 0.0

    def test_zero_fraction(self):
        """Zero fraction -> 0."""
        rm = RiskManager()
        assert rm.apply_risk_limits(0.0, "m1", 10000.0) == 0.0
