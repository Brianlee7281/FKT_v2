"""MC Core — Numba JIT-compiled Monte Carlo Simulation.

Simulates remaining match trajectories under the MMPP model,
accounting for score-dependent intensity (delta), red-card state
transitions (gamma + Markov chain), and piecewise time profile (b).

Returns final_scores shape (N, 2) for downstream pricing.

Performance target: < 1ms for N=50,000 after JIT warmup.

Reference: phase3.md → Step 3.4 → Logic B: Monte Carlo Pricing
"""

from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True)
def mc_simulate_remaining(
    t_now: float,
    T_end: float,
    S_H: int,
    S_A: int,
    state: int,
    score_diff: int,
    a_H: float,
    a_A: float,
    b: np.ndarray,            # shape (6,)
    gamma_H: np.ndarray,      # shape (4,)
    gamma_A: np.ndarray,      # shape (4,)
    delta_H: np.ndarray,      # shape (5,)
    delta_A: np.ndarray,      # shape (5,)
    Q_diag: np.ndarray,       # shape (4,) — diagonal of Q (negative)
    Q_off: np.ndarray,        # shape (4, 4) — normalized off-diagonal probs
    basis_bounds: np.ndarray,  # shape (7,) — bin boundary times
    N: int,
    seed: int,
) -> np.ndarray:
    """Simulate N remaining match trajectories under MMPP.

    Each simulation advances from t_now to T_end using thinning
    (next-event) with competing Poisson processes for home goals,
    away goals, and Markov state transitions (red cards).

    Args:
        t_now: Current match time (minutes).
        T_end: Expected end time (minutes, incl. stoppage).
        S_H, S_A: Current home/away scores.
        state: Current Markov state X ∈ {0, 1, 2, 3}.
        score_diff: Current ΔS = S_H - S_A.
        a_H, a_A: Match-level baseline intensities.
        b: Time profile coefficients, shape (6,).
        gamma_H, gamma_A: Red-card intensity adjustments, shape (4,).
        delta_H, delta_A: Score-diff intensity adjustments, shape (5,).
        Q_diag: Diagonal of Q matrix (negative departure rates), shape (4,).
        Q_off: Normalized off-diagonal transition probabilities, shape (4, 4).
        basis_bounds: Time bin boundaries, shape (7,).
        N: Number of simulations.
        seed: RNG seed for reproducibility.

    Returns:
        final_scores: shape (N, 2), dtype int32.
            Column 0 = final home score, column 1 = final away score.
    """
    np.random.seed(seed)
    results = np.empty((N, 2), dtype=np.int32)

    for sim in range(N):
        s = t_now
        sh = S_H
        sa = S_A
        st = state
        sd = score_diff

        while s < T_end:
            # Current basis index (default to last bin for late-game/stoppage)
            bi = 5
            for k in range(6):
                if s >= basis_bounds[k] and s < basis_bounds[k + 1]:
                    bi = k
                    break

            # Delta index: ΔS → {0: ≤-2, 1: -1, 2: 0, 3: +1, 4: ≥+2}
            di = max(0, min(4, sd + 2))

            # Compute intensities using team-specific gamma
            lam_H = np.exp(a_H + b[bi] + gamma_H[st] + delta_H[di])
            lam_A = np.exp(a_A + b[bi] + gamma_A[st] + delta_A[di])
            lam_red = -Q_diag[st]
            lam_total = lam_H + lam_A + lam_red

            if lam_total <= 0.0:
                break

            # Waiting time to next event (exponential)
            dt = -np.log(np.random.random()) / lam_total
            s_next = s + dt

            # Find next basis boundary or match end
            next_bound = T_end
            for k in range(7):
                if basis_bounds[k] > s:
                    next_bound = min(next_bound, basis_bounds[k])
                    break

            # If event falls beyond boundary, advance to boundary
            if s_next >= next_bound:
                s = next_bound
                continue

            s = s_next

            # Sample event type
            u = np.random.random() * lam_total
            if u < lam_H:
                # Home goal
                sh += 1
                sd += 1
            elif u < lam_H + lam_A:
                # Away goal
                sa += 1
                sd -= 1
            else:
                # Markov transition (red card)
                cum = 0.0
                r = np.random.random()
                for j in range(4):
                    if j == st:
                        continue
                    cum += Q_off[st, j]
                    if r < cum:
                        st = j
                        break

        results[sim, 0] = sh
        results[sim, 1] = sa

    return results


def warmup_jit() -> None:
    """Trigger JIT compilation with a tiny simulation.

    Call once at startup so that the first real invocation
    doesn't pay the compilation penalty (~1-2s).
    """
    b = np.zeros(6, dtype=np.float64)
    gamma_H = np.zeros(4, dtype=np.float64)
    gamma_A = np.zeros(4, dtype=np.float64)
    delta_H = np.zeros(5, dtype=np.float64)
    delta_A = np.zeros(5, dtype=np.float64)
    Q_diag = np.array([-0.01, -0.01, -0.01, 0.0], dtype=np.float64)
    Q_off = np.zeros((4, 4), dtype=np.float64)
    # Simple transitions for warmup
    Q_off[0, 1] = 0.5
    Q_off[0, 2] = 0.5
    Q_off[1, 3] = 1.0
    Q_off[2, 3] = 1.0
    basis_bounds = np.array([0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 98.0],
                            dtype=np.float64)

    mc_simulate_remaining(
        0.0, 98.0, 0, 0, 0, 0,
        -3.5, -3.5,
        b, gamma_H, gamma_A, delta_H, delta_A,
        Q_diag, Q_off, basis_bounds,
        10, 42,
    )
