"""Step 4.3: Position Sizing — Fee-Adjusted Kelly Criterion.

Computes optimal investment fraction f* that maximizes long-run
geometric growth while keeping ruin probability at zero.

Uses direction-specific W/L payoffs with P_cons already adjusted
by direction from Step 4.2, plus market alignment multiplier.

Reference: phase4.md -> Step 4.3
"""

from __future__ import annotations

from dataclasses import dataclass

from src.trading.step_4_2_edge_detection import Signal


# ---------------------------------------------------------------------------
# Kelly computation
# ---------------------------------------------------------------------------

def compute_kelly_W_L(
    direction: str, P_kalshi: float, c: float
) -> tuple[float, float]:
    """Compute direction-specific win/loss payoffs per contract.

    Args:
        direction: BUY_YES or BUY_NO.
        P_kalshi: VWAP effective price (Yes-probability space).
        c: Fee rate (e.g., 0.07).

    Returns:
        (W, L) — win payoff and loss payoff per $1 risked.
    """
    if direction == "BUY_YES":
        # Yes wins: profit = (1 - P_kalshi), fee applied
        # Yes loses: loss = P_kalshi
        W = (1 - c) * (1 - P_kalshi)
        L = P_kalshi
    elif direction == "BUY_NO":
        # No wins (Yes=0): profit = P_kalshi (the sell price), fee applied
        # No loses (Yes=100): loss = (1 - P_kalshi)
        W = (1 - c) * P_kalshi
        L = 1 - P_kalshi
    else:
        W, L = 0.0, 0.0

    return W, L


def compute_kelly(signal: Signal, c: float, K_frac: float) -> float:
    """Kelly fraction with directional P_cons + market alignment multiplier.

    f_kelly = EV / (W * L)
    f_invest = K_frac * f_kelly * kelly_multiplier

    Args:
        signal: Signal from Step 4.2 (contains direction, EV, P_kalshi, kelly_multiplier).
        c: Fee rate.
        K_frac: Fractional Kelly (e.g., 0.25 = quarter Kelly).

    Returns:
        Investment fraction of bankroll (0 to ~cap).
    """
    W, L = compute_kelly_W_L(signal.direction, signal.P_kalshi, c)

    if W * L <= 0:
        return 0.0

    f_kelly = signal.EV / (W * L)

    # Fractional Kelly
    f_invest = K_frac * f_kelly

    # Market alignment multiplier
    # ALIGNED -> 0.8, DIVERGENT -> 0.5, UNAVAILABLE -> 0.6
    f_invest *= signal.kelly_multiplier

    return max(0.0, f_invest)


# ---------------------------------------------------------------------------
# Contract quantity computation
# ---------------------------------------------------------------------------

def compute_contracts(f_invest: float, bankroll: float, P_kalshi: float) -> int:
    """Convert investment fraction to contract count.

    Contracts = floor(f_invest * bankroll / P_kalshi)

    Args:
        f_invest: Investment fraction (after Kelly + risk limits).
        bankroll: Current bankroll in dollars.
        P_kalshi: VWAP effective price.

    Returns:
        Number of contracts (>= 0).
    """
    if P_kalshi <= 0 or f_invest <= 0:
        return 0
    return int(f_invest * bankroll / P_kalshi)


# ---------------------------------------------------------------------------
# Match-correlated position cap (pro-rata scaling)
# ---------------------------------------------------------------------------

def apply_match_cap_pro_rata(
    f_invests: dict[str, float], f_match_cap: float
) -> dict[str, float]:
    """Scale correlated positions within same match to respect match cap.

    If sum of |f_invest| across markets in a match exceeds f_match_cap,
    scale all proportionally.

    Args:
        f_invests: Dict of {market_ticker: f_invest} for a single match.
        f_match_cap: Maximum combined fraction for one match (e.g., 0.05).

    Returns:
        Scaled f_invest dict.
    """
    total = sum(abs(f) for f in f_invests.values())
    if total <= 0 or total <= f_match_cap:
        return dict(f_invests)

    scale = f_match_cap / total
    return {ticker: f * scale for ticker, f in f_invests.items()}
