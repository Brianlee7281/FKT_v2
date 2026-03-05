"""Tests for Step 4.2: Fee-Adjusted Edge Detection (2-Pass VWAP).

Verifies conservative P adjustment, EV formulas, 2-pass VWAP mechanics,
market alignment check, and signal generation.

Reference: implementation_roadmap.md -> Step 4.2 tests
"""

from __future__ import annotations

import pytest

from src.kalshi.orderbook import OrderBookSync
from src.trading.step_4_2_edge_detection import (
    MarketAlignment,
    Signal,
    check_market_alignment,
    compute_conservative_P,
    compute_ev_buy_no,
    compute_ev_buy_yes,
    compute_signal_with_vwap,
    generate_signal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def deep_book():
    """Order book with enough depth for VWAP testing."""
    ob = OrderBookSync(q_min=5)
    ob.update_book(
        bids=[
            (0.55, 50),
            (0.53, 100),
            (0.50, 200),
        ],
        asks=[
            (0.57, 50),
            (0.59, 100),
            (0.62, 200),
        ],
    )
    return ob


@pytest.fixture
def thin_book():
    """Order book with minimal depth — VWAP will differ from best."""
    ob = OrderBookSync(q_min=5)
    ob.update_book(
        bids=[(0.55, 5), (0.50, 5)],
        asks=[(0.57, 5), (0.65, 5)],
    )
    return ob


# ---------------------------------------------------------------------------
# P_cons Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestConservativeP:
    """test_buy_yes_P_cons_is_lower_bound / test_buy_no_P_cons_is_upper_bound"""

    def test_buy_yes_P_cons_is_lower_bound(self):
        """Buy Yes: P_cons = P_true - z*sigma (lower bound)."""
        P_true = 0.60
        sigma = 0.02
        z = 1.645
        P_cons = compute_conservative_P(P_true, sigma, "BUY_YES", z)
        expected = P_true - z * sigma
        assert P_cons == pytest.approx(expected)
        assert P_cons < P_true

    def test_buy_no_P_cons_is_upper_bound(self):
        """Buy No: P_cons = P_true + z*sigma (upper bound)."""
        P_true = 0.40
        sigma = 0.02
        z = 1.645
        P_cons = compute_conservative_P(P_true, sigma, "BUY_NO", z)
        expected = P_true + z * sigma
        assert P_cons == pytest.approx(expected)
        assert P_cons > P_true

    def test_zero_sigma_no_adjustment(self):
        """With sigma=0 (analytical mode), P_cons = P_true."""
        P_true = 0.55
        assert compute_conservative_P(P_true, 0.0, "BUY_YES") == P_true
        assert compute_conservative_P(P_true, 0.0, "BUY_NO") == P_true

    def test_hold_direction_passthrough(self):
        """Non-directional -> P_true returned unchanged."""
        assert compute_conservative_P(0.5, 0.05, "HOLD") == 0.5


# ---------------------------------------------------------------------------
# EV Formula Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestEVFormulas:
    """test_buy_no_EV_formula and Buy Yes EV correctness."""

    def test_buy_yes_ev_formula(self):
        """EV_yes = P_cons*(1-c)*(1-P) - (1-P_cons)*P"""
        P_cons = 0.65
        P_kalshi = 0.55
        c = 0.07
        ev = compute_ev_buy_yes(P_cons, P_kalshi, c)
        expected = P_cons * (1 - c) * (1 - P_kalshi) - (1 - P_cons) * P_kalshi
        assert ev == pytest.approx(expected)

    def test_buy_no_ev_formula(self):
        """EV_no = (1-P_cons)*(1-c)*P - P_cons*(1-P)"""
        P_cons = 0.35
        P_kalshi = 0.55
        c = 0.07
        ev = compute_ev_buy_no(P_cons, P_kalshi, c)
        expected = (1 - P_cons) * (1 - c) * P_kalshi - P_cons * (1 - P_kalshi)
        assert ev == pytest.approx(expected)

    def test_buy_yes_positive_ev_when_model_higher(self):
        """When P_true >> P_kalshi, Buy Yes EV is positive."""
        ev = compute_ev_buy_yes(0.70, 0.50, 0.07)
        assert ev > 0

    def test_buy_no_positive_ev_when_model_lower(self):
        """When P_true << P_kalshi, Buy No EV is positive."""
        ev = compute_ev_buy_no(0.30, 0.50, 0.07)
        assert ev > 0

    def test_no_edge_when_fair(self):
        """At fair price with fees, EV should be negative."""
        # With fees, fair price has negative EV for both sides
        ev_yes = compute_ev_buy_yes(0.50, 0.50, 0.07)
        ev_no = compute_ev_buy_no(0.50, 0.50, 0.07)
        assert ev_yes < 0
        assert ev_no < 0

    def test_zero_fee_symmetry(self):
        """With c=0, EV_yes(P, ask) + EV_no(P, ask) should be zero
        when P_cons is the same for both (i.e., at P_true)."""
        P = 0.60
        ask = 0.55
        # This isn't exactly symmetric because the formulas differ,
        # but with c=0 and same P_kalshi:
        # EV_yes = P*(1-ask) - (1-P)*ask = P - ask
        # EV_no = (1-P)*ask - P*(1-ask) = ask - P
        ev_yes = compute_ev_buy_yes(P, ask, 0.0)
        ev_no = compute_ev_buy_no(P, ask, 0.0)
        assert ev_yes == pytest.approx(-(ev_no))


# ---------------------------------------------------------------------------
# Market Alignment Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestMarketAlignment:
    """test_alignment_ALIGNED_multiplier_0_8 / test_alignment_DIVERGENT_multiplier_0_5"""

    def test_alignment_ALIGNED_multiplier_0_8(self):
        """Aligned: model and bet365 agree -> multiplier 0.8 (NOT 1.0)."""
        # Buy Yes: both model and bet365 say price is too low
        alignment = check_market_alignment(
            P_true_cons=0.65,  # model says high
            P_kalshi=0.55,     # market price
            P_bet365=0.60,     # bet365 also says high
            direction="BUY_YES",
        )
        assert alignment.status == "ALIGNED"
        assert alignment.kelly_multiplier == 0.8

    def test_alignment_DIVERGENT_multiplier_0_5(self):
        """Divergent: model and bet365 disagree -> multiplier 0.5."""
        # Buy Yes: model says high, bet365 says low
        alignment = check_market_alignment(
            P_true_cons=0.65,
            P_kalshi=0.55,
            P_bet365=0.50,  # bet365 says NOT high (below Kalshi)
            direction="BUY_YES",
        )
        assert alignment.status == "DIVERGENT"
        assert alignment.kelly_multiplier == 0.5

    def test_alignment_UNAVAILABLE_multiplier_0_6(self):
        """No bet365 data -> UNAVAILABLE, multiplier 0.6."""
        alignment = check_market_alignment(
            P_true_cons=0.65,
            P_kalshi=0.55,
            P_bet365=None,
            direction="BUY_YES",
        )
        assert alignment.status == "UNAVAILABLE"
        assert alignment.kelly_multiplier == 0.6

    def test_buy_no_aligned(self):
        """Buy No aligned: model and bet365 both say price is too high."""
        alignment = check_market_alignment(
            P_true_cons=0.35,  # model says low (good for No)
            P_kalshi=0.55,     # market says high
            P_bet365=0.45,     # bet365 also says lower than Kalshi
            direction="BUY_NO",
        )
        assert alignment.status == "ALIGNED"
        assert alignment.kelly_multiplier == 0.8

    def test_buy_no_divergent(self):
        """Buy No divergent: model says low but bet365 says high."""
        alignment = check_market_alignment(
            P_true_cons=0.35,
            P_kalshi=0.55,
            P_bet365=0.60,  # bet365 says higher than Kalshi
            direction="BUY_NO",
        )
        assert alignment.status == "DIVERGENT"
        assert alignment.kelly_multiplier == 0.5


# ---------------------------------------------------------------------------
# 2-Pass VWAP Tests (per roadmap)
# ---------------------------------------------------------------------------

class TestVWAPPass2:
    """test_vwap_pass2_reduces_EV / test_hold_when_vwap_kills_edge"""

    def test_vwap_pass2_reduces_EV(self, thin_book):
        """VWAP effective price is worse than best ask -> final_EV <= rough_EV.

        With a thin book, the VWAP for any multi-contract buy will be
        worse (higher) than best ask, reducing EV.
        """
        # Strong edge: P_true=0.80 vs ask=0.57
        signal = compute_signal_with_vwap(
            P_true=0.80, sigma_MC=0.0, ob_sync=thin_book,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="TEST",
            theta_entry=0.02,
        )
        if signal.direction == "BUY_YES":
            # Final EV uses VWAP (>= best ask)
            best_ask_ev = compute_ev_buy_yes(signal.P_cons, thin_book.kalshi_best_ask, 0.07)
            assert signal.EV <= best_ask_ev + 1e-10

    def test_hold_when_vwap_kills_edge(self):
        """Edge present at best ask but disappears after VWAP -> HOLD."""
        ob = OrderBookSync(q_min=1)
        # Small edge at 0.57 but next level at 0.90 (terrible)
        ob.update_book(
            bids=[(0.55, 100)],
            asks=[(0.57, 2), (0.90, 100)],
        )
        # Moderate edge: P_true=0.65 vs ask=0.57 -> EV ~0.04
        # But VWAP for any qty > 2 will include 0.90 level
        signal = compute_signal_with_vwap(
            P_true=0.65, sigma_MC=0.0, ob_sync=ob,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=5000.0, market_ticker="TEST",
            theta_entry=0.02,
        )
        # The rough qty should be > 2, pulling VWAP toward 0.90
        # which destroys the edge
        assert signal.direction == "HOLD"

    def test_signal_passes_when_deep_book(self, deep_book):
        """With deep book, VWAP ~ best ask -> signal passes."""
        signal = compute_signal_with_vwap(
            P_true=0.75, sigma_MC=0.0, ob_sync=deep_book,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="TEST",
            theta_entry=0.02,
        )
        assert signal.direction == "BUY_YES"
        assert signal.EV > 0.02
        assert signal.P_kalshi >= deep_book.kalshi_best_ask

    def test_hold_when_no_book(self):
        """Empty book -> HOLD."""
        ob = OrderBookSync()
        signal = compute_signal_with_vwap(
            P_true=0.80, sigma_MC=0.0, ob_sync=ob,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="TEST",
        )
        assert signal.direction == "HOLD"

    def test_buy_no_signal(self, deep_book):
        """Low P_true -> Buy No direction."""
        # Use smaller bankroll so rough_qty fits within bid depth (350)
        signal = compute_signal_with_vwap(
            P_true=0.25, sigma_MC=0.0, ob_sync=deep_book,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=200.0, market_ticker="TEST",
            theta_entry=0.02,
        )
        assert signal.direction == "BUY_NO"
        assert signal.EV > 0.02

    def test_hold_when_no_edge(self, deep_book):
        """Fair price with fees -> no edge -> HOLD."""
        # P_true ≈ mid-market (0.56), no meaningful edge
        signal = compute_signal_with_vwap(
            P_true=0.56, sigma_MC=0.0, ob_sync=deep_book,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="TEST",
            theta_entry=0.02,
        )
        assert signal.direction == "HOLD"


# ---------------------------------------------------------------------------
# Full Signal Generation Tests
# ---------------------------------------------------------------------------

class TestGenerateSignal:
    """Integration tests for generate_signal (VWAP + alignment)."""

    def test_full_signal_with_alignment(self, deep_book):
        """Full pipeline: signal + alignment check."""
        signal = generate_signal(
            P_true=0.75, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=0.70,  # aligned (also says high)
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="OVER25",
            theta_entry=0.02,
        )
        assert signal.direction == "BUY_YES"
        assert signal.alignment_status == "ALIGNED"
        assert signal.kelly_multiplier == 0.8

    def test_signal_divergent_alignment(self, deep_book):
        """Signal passes but bet365 disagrees -> DIVERGENT."""
        signal = generate_signal(
            P_true=0.75, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=0.50,  # bet365 says lower than Kalshi ask
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="OVER25",
            theta_entry=0.02,
        )
        assert signal.direction == "BUY_YES"
        assert signal.alignment_status == "DIVERGENT"
        assert signal.kelly_multiplier == 0.5

    def test_signal_no_bet365(self, deep_book):
        """No bet365 data -> UNAVAILABLE, multiplier 0.6."""
        signal = generate_signal(
            P_true=0.75, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=None,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="OVER25",
            theta_entry=0.02,
        )
        assert signal.direction == "BUY_YES"
        assert signal.alignment_status == "UNAVAILABLE"
        assert signal.kelly_multiplier == 0.6

    def test_hold_propagates(self, deep_book):
        """HOLD from VWAP pass -> no alignment check needed."""
        signal = generate_signal(
            P_true=0.56, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=0.60,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="OVER25",
            theta_entry=0.02,
        )
        assert signal.direction == "HOLD"

    def test_market_ticker_preserved(self, deep_book):
        """Market ticker passed through to output signal."""
        signal = generate_signal(
            P_true=0.75, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=0.70,
            c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="SOCCER-EPL-MCI-ARS-O25",
            theta_entry=0.02,
        )
        assert signal.market_ticker == "SOCCER-EPL-MCI-ARS-O25"


# ---------------------------------------------------------------------------
# MC Uncertainty Integration Tests
# ---------------------------------------------------------------------------

class TestMCUncertainty:
    """Tests verifying sigma_MC correctly adjusts direction selection."""

    def test_sigma_reduces_buy_yes_attractiveness(self, deep_book):
        """Higher sigma_MC makes Buy Yes less attractive (lower P_cons)."""
        sig_0 = generate_signal(
            P_true=0.65, sigma_MC=0.0, ob_sync=deep_book,
            P_bet365=None, c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="T",
        )
        sig_high = generate_signal(
            P_true=0.65, sigma_MC=0.05, ob_sync=deep_book,
            P_bet365=None, c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="T",
        )
        # With high sigma, P_cons_yes is much lower -> EV reduced or HOLD
        if sig_0.direction != "HOLD" and sig_high.direction != "HOLD":
            assert sig_high.EV <= sig_0.EV
        # At minimum, high sigma shouldn't produce a BETTER signal
        if sig_0.direction == "HOLD":
            assert sig_high.direction == "HOLD"

    def test_large_sigma_causes_hold(self, deep_book):
        """Very large sigma -> P_cons so extreme that no edge survives."""
        signal = generate_signal(
            P_true=0.60, sigma_MC=0.10, ob_sync=deep_book,
            P_bet365=None, c=0.07, z=1.645, K_frac=0.25,
            bankroll=1000.0, market_ticker="T",
        )
        # P_cons_yes = 0.60 - 1.645*0.10 = 0.4355 (below ask 0.57)
        # P_cons_no = 0.60 + 1.645*0.10 = 0.7645 (above bid 0.55)
        # Both EVs should be negative -> HOLD
        assert signal.direction == "HOLD"
