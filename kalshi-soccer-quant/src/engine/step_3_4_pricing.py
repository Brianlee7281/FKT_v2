"""Step 3.4: Hybrid Pricing — True Probability Estimation.

Uses remaining expected goals (μ_H, μ_A) to estimate true probabilities
for all active markets simultaneously.

Two modes:
  A. Analytic (X=0, ΔS=0, delta not significant): Poisson/Skellam — 0.1ms
  B. Monte Carlo (otherwise): Numba JIT via ThreadPoolExecutor — ~0.5ms

Reference: phase3.md → Step 3.4
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import poisson

from src.common.logging import get_logger
from src.engine.mc_core import mc_simulate_remaining

log = get_logger(__name__)

# MC configuration
N_MC = 50_000
mc_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mc")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PricingResult:
    """Output of the pricing step."""

    P_true: dict[str, float] = field(default_factory=dict)
    sigma_MC: float = 0.0
    pricing_mode: str = "analytical"  # "analytical" or "monte_carlo"


# ---------------------------------------------------------------------------
# Logic A: Analytic pricing (X=0, ΔS=0)
# ---------------------------------------------------------------------------

def analytical_pricing(
    mu_H: float,
    mu_A: float,
    current_score: tuple[int, int],
) -> dict[str, float]:
    """Compute P_true for all markets using Poisson/Skellam.

    Applicable when X=0, ΔS=0, and delta is not significant,
    so home/away scoring is independent.

    Args:
        mu_H: Remaining expected home goals.
        mu_A: Remaining expected away goals.
        current_score: Current (S_H, S_A).

    Returns:
        P_true dict with keys for all markets.
    """
    S_H, S_A = current_score
    G = S_H + S_A
    mu_total = mu_H + mu_A

    result = {}

    # Over/Under markets
    for n in (1, 2, 3, 4, 5):
        if G > n:
            result[f"over_{n}5"] = 1.0
        else:
            remaining_needed = n - G
            result[f"over_{n}5"] = float(
                1.0 - poisson.cdf(remaining_needed, mu_total)
            )

    # Match Odds (independent Poisson convolution)
    max_goals = 12
    p_home, p_draw, p_away = 0.0, 0.0, 0.0

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            final_h = S_H + h
            final_a = S_A + a
            p = poisson.pmf(h, mu_H) * poisson.pmf(a, mu_A)
            if final_h > final_a:
                p_home += p
            elif final_h == final_a:
                p_draw += p
            else:
                p_away += p

    result["home_win"] = float(p_home)
    result["draw"] = float(p_draw)
    result["away_win"] = float(p_away)

    # Both Teams to Score
    p_h_zero = poisson.pmf(0, mu_H) if S_H == 0 else 0.0
    p_a_zero = poisson.pmf(0, mu_A) if S_A == 0 else 0.0

    if S_H > 0 and S_A > 0:
        result["btts_yes"] = 1.0
    elif S_H > 0:
        result["btts_yes"] = float(1.0 - p_a_zero)
    elif S_A > 0:
        result["btts_yes"] = float(1.0 - p_h_zero)
    else:
        result["btts_yes"] = float((1.0 - p_h_zero) * (1.0 - p_a_zero))

    return result


# ---------------------------------------------------------------------------
# Logic B: MC pricing aggregation
# ---------------------------------------------------------------------------

def aggregate_markets(
    final_scores: np.ndarray,
    current_score: tuple[int, int],
) -> dict[str, float]:
    """Aggregate MC simulation results into market probabilities.

    Args:
        final_scores: Shape (N, 2) — final (home, away) scores.
        current_score: Current (S_H, S_A) for reference.

    Returns:
        P_true dict with keys for all markets.
    """
    N = len(final_scores)
    sh = final_scores[:, 0]
    sa = final_scores[:, 1]
    total = sh + sa

    result = {}

    # Over/Under
    for n in (1, 2, 3, 4, 5):
        result[f"over_{n}5"] = float(np.mean(total > n))

    # Match Odds
    result["home_win"] = float(np.mean(sh > sa))
    result["draw"] = float(np.mean(sh == sa))
    result["away_win"] = float(np.mean(sh < sa))

    # BTTS
    result["btts_yes"] = float(np.mean((sh > 0) & (sa > 0)))

    return result


def compute_mc_stderr(P_true: dict[str, float], N: int) -> float:
    """Compute worst-case MC standard error across all markets.

    σ_MC = max over markets of sqrt(p(1-p)/N).
    """
    max_se = 0.0
    for p in P_true.values():
        se = np.sqrt(p * (1 - p) / N) if 0 < p < 1 else 0.0
        max_se = max(max_se, se)
    return float(max_se)


# ---------------------------------------------------------------------------
# Hybrid pricing entry point
# ---------------------------------------------------------------------------

def price_analytical(
    mu_H: float,
    mu_A: float,
    current_score: tuple[int, int],
) -> PricingResult:
    """Synchronous analytical pricing (for X=0, ΔS=0 fast path)."""
    P_true = analytical_pricing(mu_H, mu_A, current_score)
    return PricingResult(P_true=P_true, sigma_MC=0.0, pricing_mode="analytical")


def price_from_mc(
    final_scores: np.ndarray,
    current_score: tuple[int, int],
) -> PricingResult:
    """Aggregate MC results into a PricingResult."""
    P_true = aggregate_markets(final_scores, current_score)
    sigma_MC = compute_mc_stderr(P_true, len(final_scores))
    return PricingResult(
        P_true=P_true, sigma_MC=sigma_MC, pricing_mode="monte_carlo"
    )


async def price_hybrid_async(
    mu_H: float,
    mu_A: float,
    current_score: tuple[int, int],
    X: int,
    delta_S: int,
    delta_significant: bool,
    t_now: float,
    T_end: float,
    a_H: float,
    a_A: float,
    b: np.ndarray,
    gamma_H: np.ndarray,
    gamma_A: np.ndarray,
    delta_H: np.ndarray,
    delta_A: np.ndarray,
    Q_diag: np.ndarray,
    Q_off: np.ndarray,
    basis_bounds: np.ndarray,
    match_id: str = "",
    mc_version: int = 0,
) -> PricingResult | None:
    """Non-blocking hybrid pricing — analytic or MC via executor.

    Returns None if MC result is stale (version mismatch).
    """
    # Fast path: analytic
    if X == 0 and delta_S == 0 and not delta_significant:
        return price_analytical(mu_H, mu_A, current_score)

    # MC path: run in executor
    loop = asyncio.get_event_loop()

    seed = hash((match_id, t_now, current_score[0],
                 current_score[1], X)) % (2**31)

    final_scores = await loop.run_in_executor(
        mc_executor,
        mc_simulate_remaining,
        t_now, T_end, current_score[0], current_score[1],
        X, delta_S,
        a_H, a_A, b,
        gamma_H, gamma_A,
        delta_H, delta_A,
        Q_diag, Q_off,
        basis_bounds, N_MC, seed,
    )

    return price_from_mc(final_scores, current_score)
