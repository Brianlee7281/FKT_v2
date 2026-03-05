"""Step 4.6: Post-Match Settlement and Analysis — 11 Metrics.

Computes realized P&L with direction-specific settlement formulas
and produces post-analysis metrics for model health monitoring.

v2 fix #8: Directional settlement branch for Buy No.
v1 BUG: Qty * (Settlement - Entry) -> sign flips for Buy No,
         contaminating all downstream metrics.

Reference: phase4.md -> Step 4.6
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.common.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Position for settlement
# ---------------------------------------------------------------------------

@dataclass
class SettledPosition:
    """A position ready for settlement."""

    direction: str  # BUY_YES or BUY_NO
    entry_price: float  # entry price in Yes-probability space
    quantity: int  # number of contracts
    market_ticker: str = ""
    match_id: str = ""
    # Post-settlement fields
    settlement_price: float = 0.0  # 1.00 (Yes wins) or 0.00 (Yes loses)
    realized_pnl: float = 0.0
    fee_paid: float = 0.0
    # Trade metadata for analysis
    EV_at_entry: float = 0.0
    alignment_status: str = ""
    paper_slippage: float = 0.0
    P_kalshi_best_at_order: float = 0.0
    fill_price: float = 0.0
    had_bet365_divergence: bool = False


# ---------------------------------------------------------------------------
# Settlement P&L — v2 fix #8
# ---------------------------------------------------------------------------

def compute_realized_pnl(
    direction: str,
    entry_price: float,
    settlement_price: float,
    quantity: int,
    fee_rate: float,
) -> tuple[float, float]:
    """Direction-specific realized P&L.

    settlement_price: from Yes perspective (Yes win=1.00, Yes lose=0.00).

    Buy Yes: profit = (Settlement - Entry) * qty
    Buy No:  profit = (Entry - Settlement) * qty

    [v2 fix #8] Directional branch — v1 used single formula that
    inverts Buy No P&L.

    Args:
        direction: BUY_YES or BUY_NO.
        entry_price: Entry price in Yes-probability space.
        settlement_price: 1.0 (Yes wins) or 0.0 (Yes loses).
        quantity: Number of contracts.
        fee_rate: Fee rate (e.g., 0.07).

    Returns:
        (net_pnl, fee) — net P&L after fees, and fee amount.
    """
    if direction == "BUY_YES":
        gross_pnl = (settlement_price - entry_price) * quantity
    elif direction == "BUY_NO":
        gross_pnl = (entry_price - settlement_price) * quantity
    else:
        gross_pnl = 0.0

    # Fee applies only to profits
    fee = fee_rate * max(0.0, gross_pnl)
    net_pnl = gross_pnl - fee
    return net_pnl, fee


def settle_position(
    position: SettledPosition,
    settlement_price: float,
    fee_rate: float,
) -> SettledPosition:
    """Settle a position and compute realized P&L.

    Args:
        position: Position to settle.
        settlement_price: 1.0 or 0.0.
        fee_rate: Fee rate.

    Returns:
        Same position with settlement fields populated.
    """
    pnl, fee = compute_realized_pnl(
        position.direction,
        position.entry_price,
        settlement_price,
        position.quantity,
        fee_rate,
    )
    position.settlement_price = settlement_price
    position.realized_pnl = pnl
    position.fee_paid = fee
    return position


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _safe_mean(values: list[float]) -> float:
    """Mean that returns 0.0 for empty list."""
    return sum(values) / len(values) if values else 0.0


def _safe_divide(a: float, b: float) -> float:
    """Safe division returning 0.0 on zero denominator."""
    return a / b if b != 0 else 0.0


def _win_rate(positions: list[SettledPosition]) -> float:
    """Fraction of positions with positive P&L."""
    if not positions:
        return 0.0
    wins = sum(1 for p in positions if p.realized_pnl > 0)
    return wins / len(positions)


# ---------------------------------------------------------------------------
# Metric 1: Match-level P&L
# ---------------------------------------------------------------------------

def compute_match_pnl(positions: list[SettledPosition]) -> float:
    """Sum of realized P&L across all positions in a match."""
    return sum(p.realized_pnl for p in positions)


# ---------------------------------------------------------------------------
# Metric 3: Edge Realization
# ---------------------------------------------------------------------------

def compute_edge_realization(positions: list[SettledPosition]) -> float:
    """Actual average return / Expected average EV.

    Healthy range: 0.7-1.3. Below 0.5 is risk.
    """
    if not positions:
        return 0.0
    actual_avg = _safe_mean([p.realized_pnl / p.quantity for p in positions if p.quantity > 0])
    expected_avg = _safe_mean([p.EV_at_entry for p in positions])
    return _safe_divide(actual_avg, expected_avg)


# ---------------------------------------------------------------------------
# Metric 4: Slippage Performance
# ---------------------------------------------------------------------------

def compute_avg_slippage(positions: list[SettledPosition]) -> float:
    """Average slippage: fill_price - best_price at order time."""
    slippages = [p.paper_slippage for p in positions if p.paper_slippage != 0]
    return _safe_mean(slippages)


# ---------------------------------------------------------------------------
# Metric 7: Market Alignment Value
# ---------------------------------------------------------------------------

def analyze_alignment_effect(
    positions: list[SettledPosition],
) -> dict[str, float]:
    """Compare performance of ALIGNED vs DIVERGENT trades."""
    aligned = [p for p in positions if p.alignment_status == "ALIGNED"]
    divergent = [p for p in positions if p.alignment_status == "DIVERGENT"]

    aligned_avg = _safe_mean([p.realized_pnl for p in aligned])
    divergent_avg = _safe_mean([p.realized_pnl for p in divergent])

    return {
        "aligned_avg_return": aligned_avg,
        "divergent_avg_return": divergent_avg,
        "aligned_win_rate": _win_rate(aligned),
        "divergent_win_rate": _win_rate(divergent),
        "aligned_count": len(aligned),
        "divergent_count": len(divergent),
        "alignment_value": aligned_avg - divergent_avg,
    }


# ---------------------------------------------------------------------------
# Metric 8: Directional P_cons Analysis
# ---------------------------------------------------------------------------

def analyze_directional_cons(
    positions: list[SettledPosition],
) -> dict[str, float]:
    """Separate edge realization for Buy Yes vs Buy No."""
    yes = [p for p in positions if p.direction == "BUY_YES"]
    no = [p for p in positions if p.direction == "BUY_NO"]

    def _edge_real(plist: list[SettledPosition]) -> float:
        if not plist:
            return 0.0
        actual = _safe_mean([p.realized_pnl / p.quantity for p in plist if p.quantity > 0])
        expected = _safe_mean([p.EV_at_entry for p in plist])
        return _safe_divide(actual, expected)

    return {
        "yes_edge_realization": _edge_real(yes),
        "no_edge_realization": _edge_real(no),
        "yes_count": len(yes),
        "no_count": len(no),
        "yes_win_rate": _win_rate(yes),
        "no_win_rate": _win_rate(no),
    }


# ---------------------------------------------------------------------------
# Metric 11: bet365 Divergence Warning Effectiveness
# ---------------------------------------------------------------------------

def analyze_bet365_divergence(
    positions: list[SettledPosition],
) -> dict[str, float]:
    """Analyze whether bet365 divergence warnings predicted losses."""
    with_div = [p for p in positions if p.had_bet365_divergence]
    without_div = [p for p in positions if not p.had_bet365_divergence]

    return {
        "divergence_avg_pnl": _safe_mean([p.realized_pnl for p in with_div]),
        "no_divergence_avg_pnl": _safe_mean([p.realized_pnl for p in without_div]),
        "divergence_count": len(with_div),
        "divergence_win_rate": _win_rate(with_div),
        "should_auto_exit": (
            _safe_mean([p.realized_pnl for p in with_div]) < 0
            if len(with_div) >= 10
            else False
        ),
    }


# ---------------------------------------------------------------------------
# Full Post-Analysis Summary
# ---------------------------------------------------------------------------

@dataclass
class PostAnalysisSummary:
    """Complete post-match analysis output."""

    # Metric 1
    match_pnl: float = 0.0
    # Metric 3
    edge_realization: float = 0.0
    # Metric 4
    avg_slippage: float = 0.0
    # Metric 7
    alignment_effect: dict = field(default_factory=dict)
    # Metric 8
    directional_cons: dict = field(default_factory=dict)
    # Metric 11
    bet365_divergence: dict = field(default_factory=dict)
    # Summary stats
    total_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0


def compute_post_analysis(
    positions: list[SettledPosition],
) -> PostAnalysisSummary:
    """Compute all post-match analysis metrics.

    Args:
        positions: List of settled positions.

    Returns:
        PostAnalysisSummary with all 11 metrics (subset implemented).
    """
    if not positions:
        return PostAnalysisSummary()

    # Compute cumulative P&L for max drawdown
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in positions:
        cum_pnl += p.realized_pnl
        peak = max(peak, cum_pnl)
        dd = peak - cum_pnl
        max_dd = max(max_dd, dd)

    return PostAnalysisSummary(
        match_pnl=compute_match_pnl(positions),
        edge_realization=compute_edge_realization(positions),
        avg_slippage=compute_avg_slippage(positions),
        alignment_effect=analyze_alignment_effect(positions),
        directional_cons=analyze_directional_cons(positions),
        bet365_divergence=analyze_bet365_divergence(positions),
        total_trades=len(positions),
        total_pnl=sum(p.realized_pnl for p in positions),
        win_rate=_win_rate(positions),
        max_drawdown=max_dd,
    )
