"""Step 4.4: Position Exit Logic — 4 Exit Triggers.

Closes positions when edge decays or reverses due to changing
in-match conditions. Implements v2 fixes for Buy No formulas.

Four triggers (evaluated in order, first match wins):
  1. Edge Decay — EV drops below theta_exit (0.5 cents)
  2. Edge Reversal — model now evaluates opposite to market [v2 fix #1]
  3. Expiry Evaluation — last 3 minutes hold vs exit comparison [v2 fix #2]
  4. bet365 Divergence — warning when bet365 moves against position [v2 fix #3]

Reference: phase4.md -> Step 4.4
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.common.logging import get_logger
from src.trading.step_4_2_edge_detection import compute_conservative_P

log = get_logger(__name__)

# Thresholds
THETA_EXIT = 0.005  # 0.5 cents — minimum EV to keep position
THETA_ENTRY = 0.02  # 2 cents — reversal detection margin
DIVERGENCE_THRESHOLD = 0.05  # 5pp — bet365 divergence warning


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    """Represents an open position for exit evaluation."""

    direction: str  # BUY_YES or BUY_NO
    entry_price: float  # entry price in Yes-probability space
    market_ticker: str = ""
    match_id: str = ""
    contracts: int = 0
    had_bet365_divergence: bool = False
    divergence_snapshot: dict = field(default_factory=dict)


@dataclass
class ExitSignal:
    """Signal to exit a position."""

    reason: str  # EDGE_DECAY, EDGE_REVERSAL, EXPIRY_EVAL, BET365_DIVERGENCE
    EV: float = 0.0
    E_hold: float = 0.0
    E_exit: float = 0.0


@dataclass
class DivergenceAlert:
    """bet365 divergence warning (logging-only by default)."""

    severity: str  # WARNING
    P_bet365: float = 0.0
    P_entry: float = 0.0
    suggested_action: str = "REDUCE_OR_EXIT"


# ---------------------------------------------------------------------------
# Trigger 1: Edge Decay
# ---------------------------------------------------------------------------

def check_edge_decay(
    position: OpenPosition,
    P_true: float,
    sigma_MC: float,
    P_kalshi_bid: float,
    c: float,
    z: float = 1.645,
    theta_exit: float = THETA_EXIT,
) -> ExitSignal | None:
    """Exit if current EV drops below theta_exit.

    Computes what the position's EV would be if exited at current bid.

    Args:
        position: Open position to evaluate.
        P_true: Current model probability.
        sigma_MC: Monte Carlo standard error.
        P_kalshi_bid: Current best bid (Yes sell price).
        c: Fee rate.
        z: Z-score for conservative adjustment.
        theta_exit: Minimum EV threshold (default 0.005).

    Returns:
        ExitSignal if EV < theta_exit, else None.
    """
    P_cons = compute_conservative_P(P_true, sigma_MC, position.direction, z)
    current_EV = _compute_position_EV(P_cons, P_kalshi_bid, position, c)

    if current_EV < theta_exit:
        return ExitSignal(reason="EDGE_DECAY", EV=current_EV)
    return None


def _compute_position_EV(
    P_cons: float,
    P_kalshi_bid: float,
    position: OpenPosition,
    c: float,
) -> float:
    """Compute current EV for an open position at market bid.

    Args:
        P_cons: Conservative probability (direction-adjusted).
        P_kalshi_bid: Current best bid.
        position: Open position.
        c: Fee rate.

    Returns:
        Expected value of holding the position.
    """
    if position.direction == "BUY_YES":
        # Hold to settlement EV at current P_cons vs entry price
        return (
            P_cons * (1 - c) * (1 - position.entry_price)
            - (1 - P_cons) * position.entry_price
        )
    elif position.direction == "BUY_NO":
        return (
            (1 - P_cons) * (1 - c) * position.entry_price
            - P_cons * (1 - position.entry_price)
        )
    return 0.0


# ---------------------------------------------------------------------------
# Trigger 2: Edge Reversal [v2 fix #1]
# ---------------------------------------------------------------------------

def check_edge_reversal(
    position: OpenPosition,
    P_true: float,
    sigma_MC: float,
    P_kalshi_bid: float,
    z: float = 1.645,
    theta_entry: float = THETA_ENTRY,
) -> ExitSignal | None:
    """Immediate exit if model now evaluates opposite to market.

    All comparisons in Yes-probability space.
    No (1-P) conversion even for Buy No.

    [v2 fix #1]: Buy No threshold uses P_kalshi_bid directly,
    NOT (1 - P_kalshi_bid).

    Args:
        position: Open position.
        P_true: Current model probability.
        sigma_MC: MC standard error.
        P_kalshi_bid: Current best bid.
        z: Z-score.
        theta_entry: Reversal margin (default 0.02).

    Returns:
        ExitSignal if reversal detected, else None.
    """
    P_cons = compute_conservative_P(P_true, sigma_MC, position.direction, z)

    if position.direction == "BUY_YES":
        # Reversal if model P(Yes) is theta below market P(Yes)
        if P_cons < P_kalshi_bid - theta_entry:
            return ExitSignal(reason="EDGE_REVERSAL", EV=0.0)

    elif position.direction == "BUY_NO":
        # [v2 fix] If model P(Yes) is theta above market P(Yes)
        # -> model P(No) is lower than market -> No position reversed
        if P_cons > P_kalshi_bid + theta_entry:
            return ExitSignal(reason="EDGE_REVERSAL", EV=0.0)
        # v1 BUG: if P_cons > (1 - P_kalshi_bid) + theta_entry
        # with bid=0.40, required 0.62 -> 20pp too strict

    return None


# ---------------------------------------------------------------------------
# Trigger 3: Expiry Evaluation [v2 fix #2]
# ---------------------------------------------------------------------------

def check_expiry_eval(
    position: OpenPosition,
    P_true: float,
    sigma_MC: float,
    P_kalshi_bid: float,
    c: float,
    z: float,
    t: float,
    T: float,
    expiry_window: float = 3.0,
) -> ExitSignal | None:
    """Near expiry: compare hold-to-settlement vs exit-now.

    [v2 fix #2]: Buy No E_hold uses direction-specific formula,
    NOT the Buy Yes formula reused.

    Args:
        position: Open position.
        P_true: Current model probability.
        sigma_MC: MC standard error.
        P_kalshi_bid: Current best bid.
        c: Fee rate.
        z: Z-score.
        t: Current match time (minutes).
        T: Expected end time (minutes).
        expiry_window: Minutes before expiry to activate (default 3.0).

    Returns:
        ExitSignal if E_exit > E_hold, else None.
    """
    if T - t >= expiry_window:
        return None

    P_cons = compute_conservative_P(P_true, sigma_MC, position.direction, z)

    # E_hold: expected value if held to settlement
    if position.direction == "BUY_YES":
        E_hold = (
            P_cons * (1 - c) * (1 - position.entry_price)
            - (1 - P_cons) * position.entry_price
        )
    elif position.direction == "BUY_NO":
        # [v2 fix] Direction-specific formula
        E_hold = (
            (1 - P_cons) * (1 - c) * position.entry_price
            - P_cons * (1 - position.entry_price)
        )
    else:
        return None

    # E_exit: expected value if exited now at bid
    if position.direction == "BUY_YES":
        # Sell Yes at bid
        profit_if_exit = P_kalshi_bid - position.entry_price
    elif position.direction == "BUY_NO":
        # Close No = buy Yes at bid to offset
        profit_if_exit = position.entry_price - P_kalshi_bid
    else:
        return None

    fee_if_exit = c * max(0.0, profit_if_exit)
    E_exit = profit_if_exit - fee_if_exit

    if E_exit > E_hold:
        return ExitSignal(
            reason="EXPIRY_EVAL", E_hold=E_hold, E_exit=E_exit
        )
    return None


# ---------------------------------------------------------------------------
# Trigger 4: bet365 Divergence Warning [v2 fix #3]
# ---------------------------------------------------------------------------

def check_bet365_divergence(
    position: OpenPosition,
    P_bet365: float | None,
    divergence_threshold: float = DIVERGENCE_THRESHOLD,
) -> DivergenceAlert | None:
    """Warning when bet365 moves against held position.

    [v2 fix #3]: Buy No threshold uses entry_price directly,
    NOT (1 - entry_price).

    Args:
        position: Open position.
        P_bet365: Current bet365 implied probability (or None).
        divergence_threshold: pp threshold (default 0.05 = 5pp).

    Returns:
        DivergenceAlert if triggered, else None.
    """
    if P_bet365 is None:
        return None

    if position.direction == "BUY_YES":
        # Yes held: warning if bet365 P(Yes) drops below entry - threshold
        if P_bet365 < position.entry_price - divergence_threshold:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT",
            )

    elif position.direction == "BUY_NO":
        # [v2 fix] No held (= sold Yes):
        # warning if bet365 P(Yes) rises above entry + threshold
        if P_bet365 > position.entry_price + divergence_threshold:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT",
            )
        # v1 BUG: if P_bet365 > (1 - entry) + 0.05
        # entry=0.40 => v1 needs 0.65 (25pp), v2 needs 0.45 (5pp)

    return None


# ---------------------------------------------------------------------------
# Full Exit Evaluation Loop
# ---------------------------------------------------------------------------

def evaluate_exit(
    position: OpenPosition,
    P_true: float,
    sigma_MC: float,
    P_kalshi_bid: float,
    P_bet365: float | None,
    c: float,
    z: float,
    t: float,
    T: float,
    bet365_divergence_auto_exit: bool = False,
) -> ExitSignal | None:
    """Evaluate all 4 exit triggers for an open position.

    Called each tick for every open position.
    First trigger that fires wins (priority order).

    Args:
        position: Open position to evaluate.
        P_true: Current model probability.
        sigma_MC: MC standard error.
        P_kalshi_bid: Current best bid.
        P_bet365: Current bet365 implied probability.
        c: Fee rate.
        z: Z-score.
        t: Current match time.
        T: Expected end time.
        bet365_divergence_auto_exit: If True, trigger 4 causes exit.

    Returns:
        ExitSignal if any trigger fires, else None.
    """
    # Trigger 1: Edge decay
    exit_sig = check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z)
    if exit_sig:
        return exit_sig

    # Trigger 2: Edge reversal
    exit_sig = check_edge_reversal(
        position, P_true, sigma_MC, P_kalshi_bid, z
    )
    if exit_sig:
        return exit_sig

    # Trigger 3: Expiry evaluation
    exit_sig = check_expiry_eval(
        position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T
    )
    if exit_sig:
        return exit_sig

    # Trigger 4: bet365 divergence warning
    divergence = check_bet365_divergence(position, P_bet365)
    if divergence:
        log.warning(
            "bet365_divergence",
            ticker=position.market_ticker,
            P_bet365=divergence.P_bet365,
            P_entry=divergence.P_entry,
        )
        position.had_bet365_divergence = True
        position.divergence_snapshot = {
            "P_bet365": P_bet365,
            "P_kalshi_bid": P_kalshi_bid,
            "P_true": P_true,
            "t": t,
        }
        if bet365_divergence_auto_exit:
            return ExitSignal(reason="BET365_DIVERGENCE")

    return None
