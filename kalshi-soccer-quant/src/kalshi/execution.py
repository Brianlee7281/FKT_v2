"""Step 4.5: Order Execution — Paper & Live Execution Layers.

Paper mode simulates realistic fills with VWAP + slippage + partial fills.
Live mode submits limit orders via the Kalshi REST client.

v2 fix #6: Paper fills use VWAP (not best ask) + slippage + partial fill.
v1 BUG: Full instant fill at best ask -> optimistic bias.

Reference: phase4.md -> Step 4.5
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.common.logging import get_logger
from src.kalshi.orderbook import OrderBookSync
from src.trading.step_4_2_edge_detection import Signal

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Fill result data classes
# ---------------------------------------------------------------------------

@dataclass
class PaperFill:
    """Result of a paper (simulated) fill."""

    price: float  # effective fill price (VWAP + slippage)
    quantity: int  # contracts filled
    target_quantity: int  # contracts requested
    timestamp: float = 0.0
    is_paper: bool = True
    slippage: float = 0.0  # fill_price - best_ask/bid
    partial: bool = False  # True if filled < requested
    direction: str = ""
    market_ticker: str = ""
    order_id: str = ""


@dataclass
class TradeLog:
    """Full trade record for post-analysis."""

    timestamp: float = 0.0
    match_id: str = ""
    market_ticker: str = ""
    direction: str = ""  # BUY_YES | BUY_NO
    order_type: str = ""  # ENTRY | EXIT_EDGE_DECAY | EXIT_EDGE_REVERSAL
    # | EXIT_EXPIRY_EVAL | EXIT_BET365_DIVERGENCE | RAPID_ENTRY
    quantity_ordered: int = 0
    quantity_filled: int = 0
    limit_price: float = 0.0
    fill_price: float = 0.0
    P_true_at_order: float = 0.0
    P_true_cons_at_order: float = 0.0
    P_kalshi_at_order: float = 0.0  # VWAP effective price
    P_kalshi_best_at_order: float = 0.0  # best ask/bid
    P_bet365_at_order: float = 0.0
    EV_adj: float = 0.0
    sigma_MC: float = 0.0
    pricing_mode: str = ""
    f_kelly: float = 0.0
    K_frac: float = 0.0
    alignment_status: str = ""
    kelly_multiplier: float = 0.0
    cooldown_active: bool = False
    ob_freeze_active: bool = False
    event_state: str = ""
    engine_phase: str = ""
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0
    is_paper: bool = True
    paper_slippage: float = 0.0


# ---------------------------------------------------------------------------
# Paper Execution Layer (v2)
# ---------------------------------------------------------------------------

class PaperExecutionLayer:
    """Simulates order execution with realistic fills.

    v2 improvements over v1:
      - VWAP-based fill price (not best ask)
      - Configurable slippage (default 1 tick = 1 cent)
      - Partial fill when order exceeds available depth

    Attributes:
        slippage_ticks: Number of ticks (cents) of slippage to add.
        trade_log: List of all executed trades.
    """

    def __init__(self, slippage_ticks: int = 1):
        self.slippage_ticks = slippage_ticks
        self.trade_log: list[TradeLog] = []

    def execute_order(
        self,
        signal: Signal,
        amount: float,
        ob_sync: OrderBookSync,
        urgent: bool = False,
    ) -> PaperFill | None:
        """Simulate order execution with VWAP + slippage + partial fill.

        Args:
            signal: Signal from Step 4.2.
            amount: Dollar amount to invest (after risk limits).
            ob_sync: Current order book state.
            urgent: If True, adds extra slippage tick.

        Returns:
            PaperFill with simulated execution, or None if cannot fill.
        """
        if signal.P_kalshi <= 0 or amount <= 0:
            return None

        target_qty = int(amount / signal.P_kalshi)
        if target_qty < 1:
            return None

        # VWAP effective price — try full qty, fall back to available depth
        if signal.direction == "BUY_YES":
            best_price = ob_sync.kalshi_best_ask
            total_depth = ob_sync.total_ask_depth()
            fill_qty = min(target_qty, total_depth)
            P_effective = ob_sync.compute_vwap_buy(fill_qty) if fill_qty > 0 else None
        elif signal.direction == "BUY_NO":
            best_price = ob_sync.kalshi_best_bid
            total_depth = ob_sync.total_bid_depth()
            fill_qty = min(target_qty, total_depth)
            P_effective = ob_sync.compute_vwap_sell(fill_qty) if fill_qty > 0 else None
        else:
            return None

        if P_effective is None or best_price is None:
            return None

        # Add slippage
        slippage = self.slippage_ticks * 0.01
        if urgent:
            slippage += 0.01  # extra tick for urgent orders

        if signal.direction == "BUY_YES":
            fill_price = P_effective + slippage
        else:
            # For sells, slippage works against us (lower price)
            fill_price = P_effective - slippage

        # Partial fill: count available depth at or better than fill price
        if signal.direction == "BUY_YES":
            available_depth = sum(
                qty for price, qty in ob_sync.kalshi_depth_ask
                if price <= fill_price
            )
        else:
            available_depth = sum(
                qty for price, qty in ob_sync.kalshi_depth_bid
                if price >= fill_price
            )

        filled_qty = min(target_qty, available_depth)
        if filled_qty < 1:
            return None

        # Recompute VWAP for actual filled_qty if it differs from initial
        # fill_qty (avoids price mismatch when slippage excludes levels)
        if filled_qty != fill_qty:
            if signal.direction == "BUY_YES":
                P_effective = ob_sync.compute_vwap_buy(filled_qty) or P_effective
            else:
                P_effective = ob_sync.compute_vwap_sell(filled_qty) or P_effective
            if signal.direction == "BUY_YES":
                fill_price = P_effective + slippage
            else:
                fill_price = P_effective - slippage

        order_id = f"paper-{uuid.uuid4().hex[:8]}"
        actual_slippage = fill_price - best_price if signal.direction == "BUY_YES" \
            else best_price - fill_price

        fill = PaperFill(
            price=fill_price,
            quantity=filled_qty,
            target_quantity=target_qty,
            timestamp=time.time(),
            is_paper=True,
            slippage=actual_slippage,
            partial=(filled_qty < target_qty),
            direction=signal.direction,
            market_ticker=signal.market_ticker,
            order_id=order_id,
        )

        log.info(
            "paper_fill",
            order_id=order_id,
            direction=signal.direction,
            ticker=signal.market_ticker,
            target_qty=target_qty,
            filled_qty=filled_qty,
            fill_price=fill_price,
            slippage=actual_slippage,
            partial=fill.partial,
        )

        return fill

    def record_trade(
        self,
        fill: PaperFill,
        signal: Signal,
        order_type: str,
        match_id: str,
        P_true: float,
        sigma_MC: float,
        P_bet365: float | None,
        pricing_mode: str,
        f_kelly: float,
        K_frac: float,
        engine_state: dict | None = None,
        bankroll_before: float = 0.0,
        bankroll_after: float = 0.0,
    ) -> TradeLog:
        """Record a completed trade for post-analysis.

        Args:
            fill: PaperFill result.
            signal: Signal that triggered the trade.
            order_type: ENTRY, EXIT_EDGE_DECAY, etc.
            match_id: Match identifier.
            P_true: Model probability at order time.
            sigma_MC: MC standard error at order time.
            P_bet365: bet365 implied probability at order time.
            pricing_mode: "analytical" or "monte_carlo".
            f_kelly: Kelly fraction used.
            K_frac: Fractional Kelly parameter.
            engine_state: Optional dict with cooldown/ob_freeze/event_state/phase.
            bankroll_before: Bankroll before this trade.
            bankroll_after: Bankroll after this trade.

        Returns:
            TradeLog entry.
        """
        es = engine_state or {}
        best_price = 0.0
        if signal.direction == "BUY_YES":
            best_price = fill.price - fill.slippage
        else:
            best_price = fill.price + fill.slippage

        entry = TradeLog(
            timestamp=fill.timestamp,
            match_id=match_id,
            market_ticker=signal.market_ticker,
            direction=signal.direction,
            order_type=order_type,
            quantity_ordered=fill.target_quantity,
            quantity_filled=fill.quantity,
            limit_price=fill.price,
            fill_price=fill.price,
            P_true_at_order=P_true,
            P_true_cons_at_order=signal.P_cons,
            P_kalshi_at_order=signal.P_kalshi,
            P_kalshi_best_at_order=best_price,
            P_bet365_at_order=P_bet365 or 0.0,
            EV_adj=signal.EV,
            sigma_MC=sigma_MC,
            pricing_mode=pricing_mode,
            f_kelly=f_kelly,
            K_frac=K_frac,
            alignment_status=signal.alignment_status,
            kelly_multiplier=signal.kelly_multiplier,
            cooldown_active=es.get("cooldown", False),
            ob_freeze_active=es.get("ob_freeze", False),
            event_state=es.get("event_state", ""),
            engine_phase=es.get("engine_phase", ""),
            bankroll_before=bankroll_before,
            bankroll_after=bankroll_after,
            is_paper=True,
            paper_slippage=fill.slippage,
        )

        self.trade_log.append(entry)
        return entry

    def get_trade_log(self) -> list[TradeLog]:
        """Return all recorded trades."""
        return list(self.trade_log)

    def clear_trade_log(self) -> None:
        """Clear trade log (e.g., end of session)."""
        self.trade_log.clear()
