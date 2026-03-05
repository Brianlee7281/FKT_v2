"""Tests for Step 4.5: Execution (Paper v2).

Verifies VWAP-based fill pricing, slippage, partial fills,
and that paper P&L is worse than naive best-ask assumption.

Reference: implementation_roadmap.md -> Step 4.5 tests
"""

from __future__ import annotations

import pytest

from src.kalshi.execution import PaperExecutionLayer, PaperFill, TradeLog
from src.kalshi.orderbook import OrderBookSync
from src.trading.step_4_2_edge_detection import Signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def deep_book():
    """3-level order book with good depth."""
    ob = OrderBookSync(q_min=5)
    ob.update_book(
        bids=[(0.55, 50), (0.53, 100), (0.50, 200)],
        asks=[(0.57, 50), (0.59, 100), (0.62, 200)],
    )
    return ob


@pytest.fixture
def thin_book():
    """Thin book for partial fill testing."""
    ob = OrderBookSync(q_min=1)
    ob.update_book(
        bids=[(0.55, 10), (0.50, 5)],
        asks=[(0.57, 10), (0.65, 5)],
    )
    return ob


@pytest.fixture
def buy_yes_signal():
    """Buy Yes signal."""
    return Signal(
        direction="BUY_YES",
        EV=0.05,
        P_cons=0.65,
        P_kalshi=0.57,
        rough_qty=10,
        alignment_status="ALIGNED",
        kelly_multiplier=0.8,
        market_ticker="OVER25",
    )


@pytest.fixture
def buy_no_signal():
    """Buy No signal."""
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


@pytest.fixture
def executor():
    """Paper executor with default 1-tick slippage."""
    return PaperExecutionLayer(slippage_ticks=1)


# ---------------------------------------------------------------------------
# Roadmap Tests
# ---------------------------------------------------------------------------

class TestPaperUsesVWAPNotBestAsk:
    """test_paper_uses_vwap_not_best_ask"""

    def test_paper_uses_vwap_not_best_ask(self, executor, deep_book, buy_yes_signal):
        """Fill price should be based on VWAP, not just best ask.

        For 100 contracts: 50@0.57 + 50@0.59 -> VWAP = 0.58
        Plus 1 tick slippage = 0.59
        NOT best_ask + slippage = 0.57 + 0.01 = 0.58
        """
        # Need qty > 50 to cross into second ask level,
        # and slippage must not exclude the second level
        amount = 100 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)

        assert fill is not None
        # VWAP for 100 = (50*0.57 + 50*0.59) / 100 = 0.58
        # + 1 tick = 0.59
        expected_vwap = (50 * 0.57 + 50 * 0.59) / 100
        expected_fill = expected_vwap + 0.01
        assert fill.price == pytest.approx(expected_fill, abs=0.001)
        # Must NOT be best_ask + slippage
        naive_fill = 0.57 + 0.01
        assert fill.price != pytest.approx(naive_fill, abs=0.001)

    def test_single_level_fill_equals_best_plus_slippage(
        self, executor, deep_book, buy_yes_signal
    ):
        """For qty fitting in top level, VWAP = best ask."""
        # 5 contracts at best ask -> VWAP = 0.57, fill = 0.58
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)
        assert fill is not None
        assert fill.price == pytest.approx(0.57 + 0.01)


class TestPaperAdds1TickSlippage:
    """test_paper_adds_1tick_slippage"""

    def test_paper_adds_1tick_slippage(self, executor, deep_book, buy_yes_signal):
        """Default slippage is 1 tick (0.01)."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)
        assert fill is not None
        # slippage = fill_price - best_ask = 0.58 - 0.57 = 0.01
        assert fill.slippage == pytest.approx(0.01)

    def test_urgent_adds_extra_tick(self, executor, deep_book, buy_yes_signal):
        """Urgent orders add 1 extra tick of slippage."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(
            buy_yes_signal, amount, deep_book, urgent=True
        )
        assert fill is not None
        # slippage = 2 ticks = 0.02
        assert fill.slippage == pytest.approx(0.02)

    def test_zero_slippage_executor(self, deep_book, buy_yes_signal):
        """Executor with 0 slippage ticks -> fill at VWAP exactly."""
        ex = PaperExecutionLayer(slippage_ticks=0)
        amount = 5 * buy_yes_signal.P_kalshi
        fill = ex.execute_order(buy_yes_signal, amount, deep_book)
        assert fill is not None
        assert fill.price == pytest.approx(0.57)  # best ask = VWAP for 5 units
        assert fill.slippage == pytest.approx(0.0)

    def test_buy_no_slippage_works_against(self, executor, deep_book, buy_no_signal):
        """Buy No slippage reduces fill price (worse for seller)."""
        amount = 5 * buy_no_signal.P_kalshi
        fill = executor.execute_order(buy_no_signal, amount, deep_book)
        assert fill is not None
        # For sell: fill = VWAP - slippage = 0.55 - 0.01 = 0.54
        assert fill.price == pytest.approx(0.55 - 0.01)
        # slippage = best_bid - fill_price = 0.55 - 0.54 = 0.01
        assert fill.slippage == pytest.approx(0.01)


class TestPaperPartialFill:
    """test_paper_partial_fill_when_depth_low"""

    def test_paper_partial_fill_when_depth_low(self, executor, thin_book):
        """When order exceeds depth, get partial fill."""
        signal = Signal(
            direction="BUY_YES",
            EV=0.05, P_cons=0.70, P_kalshi=0.57,
            rough_qty=20, market_ticker="TEST",
        )
        # thin_book asks: 10@0.57, 5@0.65 = 15 total depth
        # target_qty = int(20*0.57/0.57) = 20 > 15 -> partial fill
        amount = 20 * signal.P_kalshi
        fill = executor.execute_order(signal, amount, thin_book)

        assert fill is not None
        assert fill.partial is True
        assert fill.quantity < 20  # less than requested
        assert fill.quantity > 0  # but got some fill

    def test_full_fill_not_marked_partial(self, executor, deep_book, buy_yes_signal):
        """Full fill is not marked partial."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)
        assert fill is not None
        assert fill.partial is False
        assert fill.quantity == fill.target_quantity

    def test_no_depth_returns_none(self, executor):
        """Empty book -> None."""
        ob = OrderBookSync()
        signal = Signal(
            direction="BUY_YES", EV=0.05, P_cons=0.70,
            P_kalshi=0.57, rough_qty=10, market_ticker="TEST",
        )
        fill = executor.execute_order(signal, 10.0, ob)
        assert fill is None


class TestPaperPNLWorseThanNaive:
    """test_paper_pnl_worse_than_naive_best_ask — must be true."""

    def test_paper_pnl_worse_than_naive_best_ask(self, executor, deep_book):
        """Paper fill price is always worse than naive best-ask assumption.

        For Buy Yes: fill > best_ask (higher cost = worse)
        For Buy No:  fill < best_bid (lower revenue = worse)
        """
        # Buy Yes: multi-level fill
        yes_signal = Signal(
            direction="BUY_YES", EV=0.05, P_cons=0.70,
            P_kalshi=0.57, rough_qty=30, market_ticker="TEST",
        )
        amount = 30 * yes_signal.P_kalshi
        fill_yes = executor.execute_order(yes_signal, amount, deep_book)

        assert fill_yes is not None
        # Paper fill price > best ask (worse for buyer)
        assert fill_yes.price > deep_book.kalshi_best_ask

        # Buy No: multi-level fill
        no_signal = Signal(
            direction="BUY_NO", EV=0.04, P_cons=0.30,
            P_kalshi=0.55, rough_qty=30, market_ticker="TEST",
        )
        amount = 30 * no_signal.P_kalshi
        fill_no = executor.execute_order(no_signal, amount, deep_book)

        assert fill_no is not None
        # Paper fill price < best bid (worse for seller)
        assert fill_no.price < deep_book.kalshi_best_bid

    def test_slippage_always_positive_for_buyer(self, executor, deep_book):
        """Slippage is always positive (cost to trader)."""
        signal = Signal(
            direction="BUY_YES", EV=0.05, P_cons=0.70,
            P_kalshi=0.57, rough_qty=5, market_ticker="TEST",
        )
        amount = 5 * signal.P_kalshi
        fill = executor.execute_order(signal, amount, deep_book)
        assert fill is not None
        assert fill.slippage >= 0


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestExecutionEdgeCases:
    """Edge case tests for execution layer."""

    def test_zero_amount_returns_none(self, executor, deep_book, buy_yes_signal):
        """Zero amount -> None."""
        assert executor.execute_order(buy_yes_signal, 0.0, deep_book) is None

    def test_negative_amount_returns_none(self, executor, deep_book, buy_yes_signal):
        """Negative amount -> None."""
        assert executor.execute_order(buy_yes_signal, -10.0, deep_book) is None

    def test_hold_signal_returns_none(self, executor, deep_book):
        """HOLD signal -> None."""
        signal = Signal(direction="HOLD")
        assert executor.execute_order(signal, 10.0, deep_book) is None

    def test_fill_has_order_id(self, executor, deep_book, buy_yes_signal):
        """Each fill gets a unique paper order ID."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill1 = executor.execute_order(buy_yes_signal, amount, deep_book)
        fill2 = executor.execute_order(buy_yes_signal, amount, deep_book)
        assert fill1 is not None and fill2 is not None
        assert fill1.order_id != fill2.order_id
        assert fill1.order_id.startswith("paper-")


# ---------------------------------------------------------------------------
# Trade Log Tests
# ---------------------------------------------------------------------------

class TestTradeLog:
    """Trade log recording and retrieval."""

    def test_record_trade(self, executor, deep_book, buy_yes_signal):
        """Recording a trade adds it to the log."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)
        assert fill is not None

        entry = executor.record_trade(
            fill=fill,
            signal=buy_yes_signal,
            order_type="ENTRY",
            match_id="match_1",
            P_true=0.70,
            sigma_MC=0.0,
            P_bet365=0.68,
            pricing_mode="analytical",
            f_kelly=0.02,
            K_frac=0.25,
            bankroll_before=5000.0,
            bankroll_after=4997.15,
        )

        assert len(executor.get_trade_log()) == 1
        assert entry.direction == "BUY_YES"
        assert entry.market_ticker == "OVER25"
        assert entry.is_paper is True
        assert entry.match_id == "match_1"

    def test_clear_trade_log(self, executor, deep_book, buy_yes_signal):
        """Clearing trade log removes all entries."""
        amount = 5 * buy_yes_signal.P_kalshi
        fill = executor.execute_order(buy_yes_signal, amount, deep_book)
        executor.record_trade(
            fill=fill, signal=buy_yes_signal, order_type="ENTRY",
            match_id="m1", P_true=0.7, sigma_MC=0.0, P_bet365=None,
            pricing_mode="analytical", f_kelly=0.02, K_frac=0.25,
        )
        assert len(executor.get_trade_log()) == 1
        executor.clear_trade_log()
        assert len(executor.get_trade_log()) == 0
