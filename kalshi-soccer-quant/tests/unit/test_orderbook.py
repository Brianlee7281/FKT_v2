"""Tests for Step 4.1: OrderBookSync — VWAP, liquidity filter, bet365 reference.

Verifies VWAP calculations match manual computation on realistic order books,
liquidity filtering, bet365 overround removal, and book update mechanics.

Reference: implementation_roadmap.md -> Step 4.1 tests
"""

from __future__ import annotations

import pytest

from src.kalshi.orderbook import OrderBookSync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_book():
    """Simple 3-level order book."""
    ob = OrderBookSync()
    ob.update_book(
        bids=[(0.55, 10), (0.53, 20), (0.50, 30)],
        asks=[(0.57, 10), (0.59, 20), (0.62, 15)],
    )
    return ob


@pytest.fixture
def thin_book():
    """Thin book below Q_min threshold."""
    ob = OrderBookSync(q_min=20)
    ob.update_book(
        bids=[(0.50, 5)],
        asks=[(0.55, 10)],
    )
    return ob


# ---------------------------------------------------------------------------
# VWAP Buy Tests
# ---------------------------------------------------------------------------

class TestVWAPBuy:
    """test_vwap_buy — consumes ask levels ascending."""

    def test_single_level_exact(self, simple_book):
        """Buy exactly the qty at best ask -> VWAP = best ask."""
        vwap = simple_book.compute_vwap_buy(10)
        assert vwap == pytest.approx(0.57)

    def test_single_level_partial(self, simple_book):
        """Buy less than best ask qty -> still VWAP = best ask."""
        vwap = simple_book.compute_vwap_buy(5)
        assert vwap == pytest.approx(0.57)

    def test_two_levels(self, simple_book):
        """Buy 20 = 10@0.57 + 10@0.59 -> manual VWAP."""
        # cost = 10*0.57 + 10*0.59 = 5.70 + 5.90 = 11.60
        # VWAP = 11.60 / 20 = 0.58
        vwap = simple_book.compute_vwap_buy(20)
        assert vwap == pytest.approx(0.58)

    def test_three_levels(self, simple_book):
        """Buy 40 = 10@0.57 + 20@0.59 + 10@0.62 -> manual VWAP."""
        # cost = 10*0.57 + 20*0.59 + 10*0.62 = 5.70 + 11.80 + 6.20 = 23.70
        # VWAP = 23.70 / 40 = 0.5925
        vwap = simple_book.compute_vwap_buy(40)
        assert vwap == pytest.approx(0.5925)

    def test_full_depth(self, simple_book):
        """Buy all 45 contracts -> consumes all levels."""
        # cost = 10*0.57 + 20*0.59 + 15*0.62 = 5.70 + 11.80 + 9.30 = 26.80
        # VWAP = 26.80 / 45
        vwap = simple_book.compute_vwap_buy(45)
        assert vwap == pytest.approx(26.80 / 45)

    def test_insufficient_depth(self, simple_book):
        """Request more than total depth -> returns None."""
        vwap = simple_book.compute_vwap_buy(50)
        assert vwap is None

    def test_empty_book(self):
        """Empty ask book -> returns None."""
        ob = OrderBookSync()
        assert ob.compute_vwap_buy(10) is None

    def test_zero_qty(self, simple_book):
        """Zero target qty -> returns None."""
        assert simple_book.compute_vwap_buy(0) is None

    def test_vwap_worse_than_best_ask(self, simple_book):
        """VWAP for multi-level fill is always >= best ask."""
        for qty in [1, 10, 20, 30, 40, 45]:
            vwap = simple_book.compute_vwap_buy(qty)
            if vwap is not None:
                assert vwap >= simple_book.kalshi_best_ask


# ---------------------------------------------------------------------------
# VWAP Sell Tests
# ---------------------------------------------------------------------------

class TestVWAPSell:
    """test_vwap_sell — consumes bid levels descending."""

    def test_single_level_exact(self, simple_book):
        """Sell exactly best bid qty -> VWAP = best bid."""
        vwap = simple_book.compute_vwap_sell(10)
        assert vwap == pytest.approx(0.55)

    def test_two_levels(self, simple_book):
        """Sell 25 = 10@0.55 + 15@0.53 -> manual VWAP."""
        # revenue = 10*0.55 + 15*0.53 = 5.50 + 7.95 = 13.45
        # VWAP = 13.45 / 25 = 0.538
        vwap = simple_book.compute_vwap_sell(25)
        assert vwap == pytest.approx(13.45 / 25)

    def test_three_levels(self, simple_book):
        """Sell 50 = 10@0.55 + 20@0.53 + 20@0.50 -> manual VWAP."""
        # revenue = 10*0.55 + 20*0.53 + 20*0.50 = 5.50 + 10.60 + 10.00 = 26.10
        # VWAP = 26.10 / 50 = 0.522
        vwap = simple_book.compute_vwap_sell(50)
        assert vwap == pytest.approx(26.10 / 50)

    def test_insufficient_depth(self, simple_book):
        """Request more than total bid depth -> None."""
        vwap = simple_book.compute_vwap_sell(100)
        assert vwap is None

    def test_vwap_worse_than_best_bid(self, simple_book):
        """VWAP for multi-level fill is always <= best bid."""
        for qty in [1, 10, 25, 50, 60]:
            vwap = simple_book.compute_vwap_sell(qty)
            if vwap is not None:
                assert vwap <= simple_book.kalshi_best_bid


# ---------------------------------------------------------------------------
# Book Update Tests
# ---------------------------------------------------------------------------

class TestUpdateBook:
    """test_update_book — sorting, filtering, best bid/ask."""

    def test_asks_sorted_ascending(self):
        """Asks are sorted low to high after update."""
        ob = OrderBookSync()
        ob.update_book(
            bids=[(0.50, 10)],
            asks=[(0.60, 5), (0.55, 10), (0.58, 8)],
        )
        prices = [p for p, _ in ob.kalshi_depth_ask]
        assert prices == [0.55, 0.58, 0.60]

    def test_bids_sorted_descending(self):
        """Bids are sorted high to low after update."""
        ob = OrderBookSync()
        ob.update_book(
            bids=[(0.45, 10), (0.50, 20), (0.48, 15)],
            asks=[(0.55, 10)],
        )
        prices = [p for p, _ in ob.kalshi_depth_bid]
        assert prices == [0.50, 0.48, 0.45]

    def test_best_bid_ask(self, simple_book):
        """Best bid/ask reflect top of sorted book."""
        assert simple_book.kalshi_best_bid == 0.55
        assert simple_book.kalshi_best_ask == 0.57

    def test_zero_qty_levels_filtered(self):
        """Levels with qty=0 are removed."""
        ob = OrderBookSync()
        ob.update_book(
            bids=[(0.50, 10), (0.48, 0)],
            asks=[(0.55, 0), (0.57, 10)],
        )
        assert len(ob.kalshi_depth_bid) == 1
        assert len(ob.kalshi_depth_ask) == 1

    def test_empty_book_none_best(self):
        """Empty book -> None for best bid/ask."""
        ob = OrderBookSync()
        ob.update_book(bids=[], asks=[])
        assert ob.kalshi_best_bid is None
        assert ob.kalshi_best_ask is None


# ---------------------------------------------------------------------------
# Liquidity Filter Tests
# ---------------------------------------------------------------------------

class TestLiquidityFilter:
    """test_liquidity_filter — Q_min threshold."""

    def test_sufficient_liquidity(self, simple_book):
        """45 ask contracts >= default Q_min=20."""
        assert simple_book.liquidity_ok() is True

    def test_insufficient_liquidity(self, thin_book):
        """10 ask contracts < Q_min=20."""
        assert thin_book.liquidity_ok() is False

    def test_custom_q_min(self):
        """Custom Q_min respected."""
        ob = OrderBookSync(q_min=5)
        ob.update_book(bids=[], asks=[(0.55, 5)])
        assert ob.liquidity_ok() is True

    def test_total_depths(self, simple_book):
        """Total ask/bid depth calculations."""
        assert simple_book.total_ask_depth() == 45  # 10+20+15
        assert simple_book.total_bid_depth() == 60  # 10+20+30


# ---------------------------------------------------------------------------
# bet365 Reference Tests
# ---------------------------------------------------------------------------

class TestBet365Reference:
    """test_bet365_reference — overround removal."""

    def test_overround_removal(self):
        """European odds -> normalized implied probabilities."""
        ob = OrderBookSync()
        # Home 2.0, Draw 3.5, Away 4.0
        # Raw: 0.5 + 0.2857 + 0.25 = 1.0357 (3.57% overround)
        ob.update_bet365({
            "1777": {
                "participants": {
                    "1": {"short_name": "Home", "value_eu": "2.0"},
                    "2": {"short_name": "X", "value_eu": "3.5"},
                    "3": {"short_name": "Away", "value_eu": "4.0"},
                }
            }
        })

        assert "home_win" in ob.bet365_implied
        assert "draw" in ob.bet365_implied
        assert "away_win" in ob.bet365_implied

        # After normalization, must sum to 1.0
        total = sum(ob.bet365_implied.values())
        assert total == pytest.approx(1.0)

        # Home should be most likely (lowest odds)
        assert ob.bet365_implied["home_win"] > ob.bet365_implied["draw"]
        assert ob.bet365_implied["home_win"] > ob.bet365_implied["away_win"]

    def test_overround_exact_values(self):
        """Verify exact normalized values."""
        ob = OrderBookSync()
        # Home 2.0, Draw 3.0, Away 4.0
        # Raw: 0.5 + 0.333 + 0.25 = 1.0833
        ob.update_bet365({
            "1777": {
                "participants": {
                    "1": {"short_name": "Home", "value_eu": "2.0"},
                    "2": {"short_name": "X", "value_eu": "3.0"},
                    "3": {"short_name": "Away", "value_eu": "4.0"},
                }
            }
        })

        raw_sum = 1 / 2.0 + 1 / 3.0 + 1 / 4.0
        assert ob.bet365_implied["home_win"] == pytest.approx(
            (1 / 2.0) / raw_sum
        )
        assert ob.bet365_implied["draw"] == pytest.approx(
            (1 / 3.0) / raw_sum
        )
        assert ob.bet365_implied["away_win"] == pytest.approx(
            (1 / 4.0) / raw_sum
        )

    def test_missing_market(self):
        """No 1777 market -> bet365_implied stays empty."""
        ob = OrderBookSync()
        ob.update_bet365({"9999": {"participants": {}}})
        assert ob.bet365_implied == {}

    def test_partial_odds_ignored(self):
        """If one participant missing, no update."""
        ob = OrderBookSync()
        ob.update_bet365({
            "1777": {
                "participants": {
                    "1": {"short_name": "Home", "value_eu": "2.0"},
                    "2": {"short_name": "X", "value_eu": "3.5"},
                    # Missing away
                }
            }
        })
        assert ob.bet365_implied == {}

    def test_invalid_odds_skipped(self):
        """Non-numeric odds are skipped gracefully."""
        ob = OrderBookSync()
        ob.update_bet365({
            "1777": {
                "participants": {
                    "1": {"short_name": "Home", "value_eu": "abc"},
                    "2": {"short_name": "X", "value_eu": "3.5"},
                    "3": {"short_name": "Away", "value_eu": "4.0"},
                }
            }
        })
        assert ob.bet365_implied == {}


# ---------------------------------------------------------------------------
# Depth Profile Tests
# ---------------------------------------------------------------------------

class TestDepthProfile:
    """test_depth_profile — monitoring output."""

    def test_profile_fields(self, simple_book):
        """Depth profile contains all expected fields."""
        profile = simple_book.depth_profile()
        assert profile["best_bid"] == 0.55
        assert profile["best_ask"] == 0.57
        assert profile["spread"] == pytest.approx(0.02)
        assert profile["ask_levels"] == 3
        assert profile["bid_levels"] == 3
        assert profile["total_ask_depth"] == 45
        assert profile["total_bid_depth"] == 60
        assert profile["liquidity_ok"] is True

    def test_empty_profile(self):
        """Empty book has None spread."""
        ob = OrderBookSync()
        profile = ob.depth_profile()
        assert profile["spread"] is None
        assert profile["liquidity_ok"] is False
