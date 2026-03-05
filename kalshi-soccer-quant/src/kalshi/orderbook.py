"""OrderBookSync — Live order-book state + bet365 alignment reference.

Maintains real-time Kalshi order-book state (bid/ask + depth),
computes VWAP effective prices for arbitrary order sizes,
and tracks bet365 implied probabilities as a market alignment reference.

Reference: phase4.md -> Step 4.1
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.common.logging import get_logger

log = get_logger(__name__)

# Default minimum depth to allow trading (contracts)
DEFAULT_Q_MIN = 20


@dataclass
class OrderBookSync:
    """Kalshi order-book state with VWAP and bet365 reference.

    Attributes:
        kalshi_best_bid: Best bid price (Yes sell price), 0-1 scale.
        kalshi_best_ask: Best ask price (Yes buy price), 0-1 scale.
        kalshi_depth_ask: Ask levels sorted ascending by price [(price, qty), ...].
        kalshi_depth_bid: Bid levels sorted descending by price [(price, qty), ...].
        bet365_implied: bet365 implied probabilities by market key.
        q_min: Minimum total ask depth to pass liquidity filter.
    """

    kalshi_best_bid: float | None = None
    kalshi_best_ask: float | None = None
    kalshi_depth_ask: list[tuple[float, int]] = field(default_factory=list)
    kalshi_depth_bid: list[tuple[float, int]] = field(default_factory=list)
    bet365_implied: dict[str, float] = field(default_factory=dict)
    q_min: int = DEFAULT_Q_MIN

    # -------------------------------------------------------------------
    # VWAP computation
    # -------------------------------------------------------------------

    def compute_vwap_buy(self, target_qty: int) -> float | None:
        """Effective buy price (VWAP) for target_qty contracts.

        Consumes ask levels from low to high price.
        Returns None if depth is insufficient.
        """
        if not self.kalshi_depth_ask or target_qty <= 0:
            return None

        filled = 0
        cost = 0.0
        for price, qty in self.kalshi_depth_ask:
            take = min(qty, target_qty - filled)
            cost += price * take
            filled += take
            if filled >= target_qty:
                break

        if filled < target_qty:
            return None  # insufficient depth

        return cost / filled

    def compute_vwap_sell(self, target_qty: int) -> float | None:
        """Effective sell price (VWAP) for target_qty contracts.

        Consumes bid levels from high to low price.
        Returns None if depth is insufficient.
        """
        if not self.kalshi_depth_bid or target_qty <= 0:
            return None

        filled = 0
        revenue = 0.0
        for price, qty in self.kalshi_depth_bid:
            take = min(qty, target_qty - filled)
            revenue += price * take
            filled += take
            if filled >= target_qty:
                break

        if filled < target_qty:
            return None  # insufficient depth

        return revenue / filled

    # -------------------------------------------------------------------
    # Liquidity filter
    # -------------------------------------------------------------------

    def liquidity_ok(self) -> bool:
        """Check if total ask depth meets minimum threshold."""
        total_ask_depth = sum(qty for _, qty in self.kalshi_depth_ask)
        return total_ask_depth >= self.q_min

    def total_ask_depth(self) -> int:
        """Total contracts available on the ask side."""
        return sum(qty for _, qty in self.kalshi_depth_ask)

    def total_bid_depth(self) -> int:
        """Total contracts available on the bid side."""
        return sum(qty for _, qty in self.kalshi_depth_bid)

    # -------------------------------------------------------------------
    # Order book updates
    # -------------------------------------------------------------------

    def update_book(
        self,
        bids: list[tuple[float, int]],
        asks: list[tuple[float, int]],
    ) -> None:
        """Update full order book from Kalshi WebSocket snapshot/delta.

        Args:
            bids: List of (price, qty) — will be sorted descending.
            asks: List of (price, qty) — will be sorted ascending.
        """
        self.kalshi_depth_bid = sorted(
            [(p, q) for p, q in bids if q > 0],
            key=lambda x: x[0],
            reverse=True,
        )
        self.kalshi_depth_ask = sorted(
            [(p, q) for p, q in asks if q > 0],
            key=lambda x: x[0],
        )

        self.kalshi_best_bid = (
            self.kalshi_depth_bid[0][0] if self.kalshi_depth_bid else None
        )
        self.kalshi_best_ask = (
            self.kalshi_depth_ask[0][0] if self.kalshi_depth_ask else None
        )

    # -------------------------------------------------------------------
    # bet365 reference price
    # -------------------------------------------------------------------

    def update_bet365(self, live_odds_markets: dict) -> None:
        """Convert Goalserve Live Odds bet365 European odds to implied probs.

        Applies overround removal (normalize to sum=1).

        Args:
            live_odds_markets: Raw market dict from Goalserve Live Odds WS.
                Expected key "1777" for full-time 1X2.
        """
        ft = live_odds_markets.get("1777", {})
        participants = ft.get("participants", {})

        home_odds: float | None = None
        draw_odds: float | None = None
        away_odds: float | None = None

        for _pid, p in participants.items():
            name = p.get("short_name", "") or p.get("name", "")
            try:
                odds = float(p["value_eu"])
            except (KeyError, ValueError, TypeError):
                continue

            if odds <= 1.0:
                continue  # invalid odds

            if "Home" in name or name == "1":
                home_odds = odds
            elif name in ("X", "Draw"):
                draw_odds = odds
            elif "Away" in name or name == "2":
                away_odds = odds

        if home_odds and draw_odds and away_odds:
            raw_sum = 1 / home_odds + 1 / draw_odds + 1 / away_odds
            self.bet365_implied["home_win"] = (1 / home_odds) / raw_sum
            self.bet365_implied["draw"] = (1 / draw_odds) / raw_sum
            self.bet365_implied["away_win"] = (1 / away_odds) / raw_sum
            log.debug(
                "bet365_updated",
                home=self.bet365_implied["home_win"],
                draw=self.bet365_implied["draw"],
                away=self.bet365_implied["away_win"],
            )

    # -------------------------------------------------------------------
    # Depth profile (for logging / monitoring)
    # -------------------------------------------------------------------

    def depth_profile(self) -> dict:
        """Return depth profile for monitoring."""
        return {
            "best_bid": self.kalshi_best_bid,
            "best_ask": self.kalshi_best_ask,
            "ask_levels": len(self.kalshi_depth_ask),
            "bid_levels": len(self.kalshi_depth_bid),
            "total_ask_depth": self.total_ask_depth(),
            "total_bid_depth": self.total_bid_depth(),
            "spread": (
                (self.kalshi_best_ask - self.kalshi_best_bid)
                if self.kalshi_best_ask is not None
                and self.kalshi_best_bid is not None
                else None
            ),
            "liquidity_ok": self.liquidity_ok(),
        }
