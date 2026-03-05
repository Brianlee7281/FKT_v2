"""Step 4.2: Fee-Adjusted Edge Detection — 2-Pass VWAP Signal Generation.

Compares model P_true with market P_kalshi, verifies positive expected
value after fees/slippage using a 2-pass VWAP approach, and classifies
edge reliability with bet365 market alignment.

The 2-pass approach resolves the circular dependency:
    EV -> Kelly -> qty -> VWAP -> EV
by computing a rough qty with best ask/bid (Pass 1), then recomputing
final EV with VWAP at that qty (Pass 2).

Reference: phase4.md -> Step 4.2
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.kalshi.orderbook import OrderBookSync


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """Output of the signal generation pipeline."""

    direction: str = "HOLD"  # BUY_YES, BUY_NO, HOLD
    EV: float = 0.0  # Final EV after VWAP
    P_cons: float = 0.0  # Directional conservative P
    P_kalshi: float = 0.0  # VWAP effective price
    rough_qty: int = 0  # Rough quantity from Pass 1
    alignment_status: str = "UNAVAILABLE"  # ALIGNED, DIVERGENT, UNAVAILABLE
    kelly_multiplier: float = 0.6  # 0.8, 0.5, 0.6
    market_ticker: str = ""


@dataclass
class MarketAlignment:
    """Result of bet365 market alignment check."""

    status: str  # ALIGNED, DIVERGENT, UNAVAILABLE
    kelly_multiplier: float  # ALIGNED->0.8, DIVERGENT->0.5, UNAVAILABLE->0.6


# ---------------------------------------------------------------------------
# Conservative P adjustment
# ---------------------------------------------------------------------------

def compute_conservative_P(
    P_true: float, sigma_MC: float, direction: str, z: float = 1.645
) -> float:
    """Directional conservative adjustment of P_true.

    Buy Yes: higher P is favorable -> use lower bound (conservatively reduce).
    Buy No:  lower P is favorable -> use upper bound (conservatively increase).

    Args:
        P_true: Model-estimated true event probability.
        sigma_MC: Monte Carlo standard error (0 for analytical mode).
        direction: "BUY_YES" or "BUY_NO".
        z: Z-score for confidence level (default 1.645 = 90%).

    Returns:
        Conservative probability estimate.
    """
    if direction == "BUY_YES":
        return P_true - z * sigma_MC
    elif direction == "BUY_NO":
        return P_true + z * sigma_MC
    return P_true


# ---------------------------------------------------------------------------
# EV formulas
# ---------------------------------------------------------------------------

def compute_ev_buy_yes(P_cons: float, P_kalshi: float, c: float) -> float:
    """Fee-adjusted EV for Buy Yes.

    EV = P_cons * (1-c) * (1-P_kalshi) - (1-P_cons) * P_kalshi

    Args:
        P_cons: Conservative probability (lower bound).
        P_kalshi: Market price (ask or VWAP).
        c: Fee rate (e.g., 0.07).

    Returns:
        Expected value per contract.
    """
    return P_cons * (1 - c) * (1 - P_kalshi) - (1 - P_cons) * P_kalshi


def compute_ev_buy_no(P_cons: float, P_kalshi: float, c: float) -> float:
    """Fee-adjusted EV for Buy No.

    EV = (1-P_cons) * (1-c) * P_kalshi - P_cons * (1-P_kalshi)

    All values in Yes-probability space. No (1-P) conversion needed.

    Args:
        P_cons: Conservative probability (upper bound).
        P_kalshi: Market price (bid or VWAP).
        c: Fee rate.

    Returns:
        Expected value per contract.
    """
    return (1 - P_cons) * (1 - c) * P_kalshi - P_cons * (1 - P_kalshi)


# ---------------------------------------------------------------------------
# Rough Kelly for Pass 1 quantity estimate
# ---------------------------------------------------------------------------

def _rough_kelly(
    direction: str,
    P_cons: float,
    P_kalshi: float,
    c: float,
    K_frac: float,
    EV: float,
) -> float:
    """Rough Kelly fraction for Pass 1 quantity estimation.

    Args:
        direction: BUY_YES or BUY_NO.
        P_cons: Conservative probability.
        P_kalshi: Best ask/bid price.
        c: Fee rate.
        K_frac: Kelly fraction (e.g., 0.25).
        EV: Expected value from Pass 1.

    Returns:
        Fractional Kelly investment fraction (0-1).
    """
    if direction == "BUY_YES":
        W = (1 - c) * (1 - P_kalshi)
        L = P_kalshi
    else:  # BUY_NO
        W = (1 - c) * P_kalshi
        L = 1 - P_kalshi

    if W * L <= 0 or EV <= 0:
        return 0.0

    f_kelly = EV / (W * L)
    return K_frac * f_kelly


# ---------------------------------------------------------------------------
# Market alignment check
# ---------------------------------------------------------------------------

def check_market_alignment(
    P_true_cons: float,
    P_kalshi: float,
    P_bet365: float | None,
    direction: str,
) -> MarketAlignment:
    """Check directional alignment between model and bet365.

    This is NOT independent validation — both are derived from the same
    Goalserve feed. It captures the interpretation gap between model (MMPP)
    and market (trader+algo). Even when aligned, use 0.8 (not 1.0).

    Args:
        P_true_cons: Conservative model probability.
        P_kalshi: Kalshi market price.
        P_bet365: bet365 implied probability (or None).
        direction: BUY_YES or BUY_NO.

    Returns:
        MarketAlignment with status and kelly_multiplier.
    """
    if P_bet365 is None:
        return MarketAlignment(status="UNAVAILABLE", kelly_multiplier=0.6)

    if direction == "BUY_YES":
        model_says_high = P_true_cons > P_kalshi
        bet365_says_high = P_bet365 > P_kalshi
        aligned = model_says_high and bet365_says_high
    elif direction == "BUY_NO":
        model_says_low = P_true_cons < P_kalshi
        bet365_says_low = P_bet365 < P_kalshi
        aligned = model_says_low and bet365_says_low
    else:
        return MarketAlignment(status="UNAVAILABLE", kelly_multiplier=0.6)

    if aligned:
        return MarketAlignment(status="ALIGNED", kelly_multiplier=0.8)
    return MarketAlignment(status="DIVERGENT", kelly_multiplier=0.5)


# ---------------------------------------------------------------------------
# 2-Pass VWAP Signal Generation
# ---------------------------------------------------------------------------

def compute_signal_with_vwap(
    P_true: float,
    sigma_MC: float,
    ob_sync: OrderBookSync,
    c: float,
    z: float,
    K_frac: float,
    bankroll: float,
    market_ticker: str,
    theta_entry: float = 0.02,
) -> Signal:
    """2-pass VWAP signal generation.

    Pass 1: Estimate rough quantity using best ask/bid.
    Pass 2: Recompute final EV using VWAP at rough quantity.

    Args:
        P_true: Model true probability.
        sigma_MC: Monte Carlo standard error.
        ob_sync: OrderBookSync with current book state.
        c: Fee rate.
        z: Z-score for conservative adjustment.
        K_frac: Kelly fraction.
        bankroll: Current bankroll in dollars.
        market_ticker: Market identifier.
        theta_entry: Minimum EV threshold (default 0.02).

    Returns:
        Signal with direction, EV, P_cons, P_kalshi (VWAP), rough_qty.
    """
    P_best_ask = ob_sync.kalshi_best_ask
    P_best_bid = ob_sync.kalshi_best_bid

    if P_best_ask is None or P_best_bid is None:
        return Signal(direction="HOLD", market_ticker=market_ticker)

    # === Pass 1: rough evaluation with best ask/bid ===

    P_cons_yes = compute_conservative_P(P_true, sigma_MC, "BUY_YES", z)
    P_cons_no = compute_conservative_P(P_true, sigma_MC, "BUY_NO", z)

    rough_EV_yes = compute_ev_buy_yes(P_cons_yes, P_best_ask, c)
    rough_EV_no = compute_ev_buy_no(P_cons_no, P_best_bid, c)

    # Direction selection (higher EV wins)
    if rough_EV_yes > rough_EV_no and rough_EV_yes > theta_entry:
        direction = "BUY_YES"
        rough_P_kalshi = P_best_ask
        P_cons = P_cons_yes
        rough_EV = rough_EV_yes
    elif rough_EV_no > theta_entry:
        direction = "BUY_NO"
        rough_P_kalshi = P_best_bid
        P_cons = P_cons_no
        rough_EV = rough_EV_no
    else:
        return Signal(direction="HOLD", market_ticker=market_ticker)

    # Rough quantity
    rough_f = _rough_kelly(direction, P_cons, rough_P_kalshi, c, K_frac, rough_EV)
    rough_qty = int(rough_f * bankroll / rough_P_kalshi) if rough_P_kalshi > 0 else 0
    if rough_qty < 1:
        return Signal(direction="HOLD", market_ticker=market_ticker)

    # === Pass 2: final EV with VWAP ===

    if direction == "BUY_YES":
        P_effective = ob_sync.compute_vwap_buy(rough_qty)
    else:
        P_effective = ob_sync.compute_vwap_sell(rough_qty)

    if P_effective is None:
        return Signal(direction="HOLD", market_ticker=market_ticker)

    # Final EV with VWAP price
    if direction == "BUY_YES":
        final_EV = compute_ev_buy_yes(P_cons, P_effective, c)
    else:
        final_EV = compute_ev_buy_no(P_cons, P_effective, c)

    if final_EV <= theta_entry:
        return Signal(direction="HOLD", market_ticker=market_ticker)

    return Signal(
        direction=direction,
        EV=final_EV,
        P_cons=P_cons,
        P_kalshi=P_effective,
        rough_qty=rough_qty,
        market_ticker=market_ticker,
    )


# ---------------------------------------------------------------------------
# Full signal generation (2-pass VWAP + alignment)
# ---------------------------------------------------------------------------

def generate_signal(
    P_true: float,
    sigma_MC: float,
    ob_sync: OrderBookSync,
    P_bet365: float | None,
    c: float,
    z: float,
    K_frac: float,
    bankroll: float,
    market_ticker: str,
    theta_entry: float = 0.02,
) -> Signal:
    """Complete signal generation: 2-pass VWAP + market alignment check.

    Args:
        P_true: Model true probability.
        sigma_MC: Monte Carlo standard error.
        ob_sync: OrderBookSync with current book.
        P_bet365: bet365 implied probability (or None).
        c: Fee rate.
        z: Z-score for conservative adjustment.
        K_frac: Kelly fraction.
        bankroll: Current bankroll.
        market_ticker: Market identifier.
        theta_entry: Minimum EV threshold.

    Returns:
        Signal with direction, EV, alignment status, kelly_multiplier.
    """
    base_signal = compute_signal_with_vwap(
        P_true, sigma_MC, ob_sync, c, z, K_frac,
        bankroll, market_ticker, theta_entry,
    )

    if base_signal.direction == "HOLD":
        return base_signal

    alignment = check_market_alignment(
        base_signal.P_cons, base_signal.P_kalshi,
        P_bet365, base_signal.direction,
    )

    return Signal(
        direction=base_signal.direction,
        EV=base_signal.EV,
        P_cons=base_signal.P_cons,
        P_kalshi=base_signal.P_kalshi,
        rough_qty=base_signal.rough_qty,
        alignment_status=alignment.status,
        kelly_multiplier=alignment.kelly_multiplier,
        market_ticker=market_ticker,
    )
