"""Step 2.4: Pre-Match Sanity Check.

Two-level verification that model probabilities do not deviate
excessively from market consensus:

  Primary:   Match Winner vs Pinnacle (+ market average fallback)
  Secondary: Over/Under 2.5 cross-validation

Input:  mu_H, mu_A, odds_features (from Steps 2.1/2.3)
Output: SanityResult (verdict, deltas, warning)

Reference: phase2.md -> Step 2.4
"""

from __future__ import annotations

from src.calibration.step_1_5_validation import (
    poisson_match_winner_probs,
    poisson_over_under,
)
from src.common.types import SanityResult


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Primary check: max |P_model - P_pinnacle| across H/D/A
PINNACLE_GO_THRESHOLD = 0.15
PINNACLE_HOLD_THRESHOLD = 0.25

# Pinnacle-deviates-but-market-agrees fallback
MARKET_AVG_CAUTION_THRESHOLD = 0.10

# Secondary check: |P_model_over25 - P_market_over25|
OU_CONSISTENCY_THRESHOLD = 0.15


# ---------------------------------------------------------------------------
# Primary: Match Winner vs Pinnacle
# ---------------------------------------------------------------------------

def primary_sanity_check(
    mu_H: float,
    mu_A: float,
    pinnacle_prob: tuple[float, float, float],
    market_avg_prob: tuple[float, float, float],
) -> tuple[str, float, float]:
    """Compare model Match Winner probs vs Pinnacle + market average.

    Args:
        mu_H: Model expected home goals.
        mu_A: Model expected away goals.
        pinnacle_prob: (P_home, P_draw, P_away) from Pinnacle.
        market_avg_prob: (P_home, P_draw, P_away) market average.

    Returns:
        (verdict, delta_pinnacle, delta_market)
    """
    probs = poisson_match_winner_probs(mu_H, mu_A)
    model_vec = (probs["home"], probs["draw"], probs["away"])

    delta_pin = max(
        abs(model_vec[i] - pinnacle_prob[i]) for i in range(3)
    )
    delta_mkt = max(
        abs(model_vec[i] - market_avg_prob[i]) for i in range(3)
    )

    if delta_pin < PINNACLE_GO_THRESHOLD:
        verdict = "GO"
    elif delta_pin < PINNACLE_HOLD_THRESHOLD:
        if delta_mkt < MARKET_AVG_CAUTION_THRESHOLD:
            verdict = "GO_WITH_CAUTION"
        else:
            verdict = "HOLD"
    else:
        verdict = "SKIP"

    return verdict, delta_pin, delta_mkt


# ---------------------------------------------------------------------------
# Secondary: Over/Under 2.5 cross-validation
# ---------------------------------------------------------------------------

def secondary_sanity_check(
    mu_H: float,
    mu_A: float,
    ou_over_odds: float,
    ou_under_odds: float,
) -> tuple[bool, float]:
    """Cross-check model total goals vs O/U 2.5 market.

    Args:
        mu_H: Model expected home goals.
        mu_A: Model expected away goals.
        ou_over_odds: Decimal odds for Over 2.5.
        ou_under_odds: Decimal odds for Under 2.5.

    Returns:
        (ou_consistent, delta_ou)
    """
    P_model_over25 = poisson_over_under(mu_H, mu_A, threshold=2.5)

    # Remove overround from O/U odds
    if ou_over_odds <= 0 or ou_under_odds <= 0:
        return True, 0.0  # No O/U data — pass by default

    ou_sum = 1.0 / ou_over_odds + 1.0 / ou_under_odds
    P_market_over25 = (1.0 / ou_over_odds) / ou_sum

    delta_ou = abs(P_model_over25 - P_market_over25)
    return bool(delta_ou < OU_CONSISTENCY_THRESHOLD), float(delta_ou)


# ---------------------------------------------------------------------------
# Combined verdict
# ---------------------------------------------------------------------------

def run_sanity_check(
    mu_H: float,
    mu_A: float,
    odds_features: dict,
) -> SanityResult:
    """Full Step 2.4: primary + secondary sanity check.

    Args:
        mu_H: Model expected home goals (from Step 2.3).
        mu_A: Model expected away goals (from Step 2.3).
        odds_features: Odds features dict from Step 2.1, containing
                       pinnacle/market probs and optionally O/U odds.

    Returns:
        SanityResult with verdict, deltas, and optional warning.
    """
    # Extract Pinnacle and market average probs
    pinnacle_prob = (
        float(odds_features.get("pinnacle_home_prob", 0.0)),
        float(odds_features.get("pinnacle_draw_prob", 0.0)),
        float(odds_features.get("pinnacle_away_prob", 0.0)),
    )
    market_avg_prob = (
        float(odds_features.get("market_avg_home_prob", 0.0)),
        float(odds_features.get("market_avg_draw_prob", 0.0)),
        float(odds_features.get("market_avg_away_prob", 0.0)),
    )

    # Check if we have valid odds data
    if sum(pinnacle_prob) < 0.5:
        return SanityResult(
            verdict="HOLD",
            warning="Insufficient odds data for sanity check",
        )

    # Primary check
    primary_verdict, delta_pin, delta_mkt = primary_sanity_check(
        mu_H, mu_A, pinnacle_prob, market_avg_prob
    )

    # Secondary check (O/U)
    ou_over = float(odds_features.get("_ou_over_odds", 0.0))
    ou_under = float(odds_features.get("_ou_under_odds", 0.0))

    if ou_over > 0 and ou_under > 0:
        ou_consistent, delta_ou = secondary_sanity_check(
            mu_H, mu_A, ou_over, ou_under
        )
    else:
        ou_consistent = True
        delta_ou = 0.0

    # Combined verdict
    if primary_verdict == "SKIP":
        return SanityResult(
            verdict="SKIP",
            delta_match_winner=delta_pin,
            delta_over_under=delta_ou,
        )

    if primary_verdict == "GO" and ou_consistent:
        return SanityResult(
            verdict="GO",
            delta_match_winner=delta_pin,
            delta_over_under=delta_ou,
        )

    if primary_verdict == "GO" and not ou_consistent:
        return SanityResult(
            verdict="GO_WITH_CAUTION",
            delta_match_winner=delta_pin,
            delta_over_under=delta_ou,
            warning="O/U mismatch — mu ratio may be off",
        )

    if primary_verdict == "HOLD":
        return SanityResult(
            verdict="HOLD",
            delta_match_winner=delta_pin,
            delta_over_under=delta_ou,
        )

    # GO_WITH_CAUTION from primary (pinnacle deviates, market agrees)
    return SanityResult(
        verdict="GO_WITH_CAUTION",
        delta_match_winner=delta_pin,
        delta_over_under=delta_ou,
    )
