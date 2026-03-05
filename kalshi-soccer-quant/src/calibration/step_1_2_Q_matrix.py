"""Step 1.2 — Estimating Markov Chain Generator Matrix Q.

Estimates red-card transition rates from historical interval data
and constructs a 4×4 generator matrix Q.

State space:
    0: 11v11 (normal)
    1: 10v11 (home sent off)
    2: 11v10 (away sent off)
    3: 10v10 (both teams sent off)

Reference: phase1.md → Step 1.2
"""

from __future__ import annotations

import numpy as np

from src.common.types import IntervalRecord

# Number of Markov states
N_STATES = 4

# Valid transitions (from → to)
VALID_TRANSITIONS = {
    (0, 1),  # 11v11 → 10v11 (home red)
    (0, 2),  # 11v11 → 11v10 (away red)
    (1, 3),  # 10v11 → 10v10 (away red while home down)
    (2, 3),  # 11v10 → 10v10 (home red while away down)
}


def estimate_Q(
    intervals: list[IntervalRecord],
    shrinkage_alpha: float = 0.0,
) -> np.ndarray:
    """Estimate the generator matrix Q from interval records.

    Q[i, j] = N_ij / T_i  for i != j
    Q[i, i] = -sum(Q[i, j] for j != i)

    where:
        N_ij = number of observed i→j transitions
        T_i  = total effective play time spent in state i (halftime excluded)

    Args:
        intervals: List of IntervalRecords from Step 1.1.
        shrinkage_alpha: Optional shrinkage weight toward pooled estimate.
                         0.0 = pure empirical, 1.0 = fully pooled.

    Returns:
        4×4 generator matrix Q.
    """
    N_transitions = np.zeros((N_STATES, N_STATES))
    T_state = np.zeros(N_STATES)

    # Group intervals by match to detect transitions
    matches: dict[str, list[IntervalRecord]] = {}
    for iv in intervals:
        matches.setdefault(iv.match_id, []).append(iv)

    for match_id, match_intervals in matches.items():
        # Sort by t_start within each match
        sorted_ivs = sorted(match_intervals, key=lambda iv: iv.t_start)

        for i, iv in enumerate(sorted_ivs):
            # Accumulate time in state (exclude halftime)
            if not iv.is_halftime:
                duration = iv.t_end - iv.t_start
                if duration > 0:
                    T_state[iv.state_X] += duration

            # Detect state transitions between consecutive intervals
            if i > 0:
                prev = sorted_ivs[i - 1]
                if prev.state_X != iv.state_X and not iv.is_halftime and not prev.is_halftime:
                    from_state = prev.state_X
                    to_state = iv.state_X
                    if (from_state, to_state) in VALID_TRANSITIONS:
                        N_transitions[from_state, to_state] += 1

    # Build Q matrix
    Q = np.zeros((N_STATES, N_STATES))
    for i in range(N_STATES):
        if T_state[i] > 0:
            for j in range(N_STATES):
                if i != j:
                    Q[i, j] = N_transitions[i, j] / T_state[i]

    # Apply additivity assumption for sparse state 3 transitions
    if shrinkage_alpha > 0:
        Q = _apply_additivity_shrinkage(Q, shrinkage_alpha)

    # Set diagonal: q_ii = -Σ_{j≠i} q_ij
    for i in range(N_STATES):
        Q[i, i] = -np.sum(Q[i, :]) + Q[i, i]  # subtract off-diag sum
    # Simpler: just set diagonal
    for i in range(N_STATES):
        Q[i, i] = 0.0
        Q[i, i] = -np.sum(Q[i, :])

    return Q


def compute_Q_off_normalized(Q: np.ndarray) -> np.ndarray:
    """Normalize off-diagonal Q entries into transition probabilities.

    For MC simulation: given a red card event in state i,
    Q_off_normalized[i, j] = probability of transitioning to state j.

    Args:
        Q: 4×4 generator matrix from estimate_Q.

    Returns:
        4×4 matrix where each row's off-diagonal entries sum to 1
        (or 0 if the row has no transitions).
    """
    Q_off = np.zeros_like(Q)
    for i in range(N_STATES):
        total_off_diag = -Q[i, i]
        if total_off_diag > 0:
            for j in range(N_STATES):
                if i != j:
                    Q_off[i, j] = Q[i, j] / total_off_diag
    return Q_off


def estimate_Q_by_league(
    intervals: list[IntervalRecord],
) -> dict[str, np.ndarray]:
    """Estimate league-specific Q matrices.

    Groups intervals by league_id (derived from match metadata)
    and estimates a separate Q for each league.

    Note: This requires league_id to be embedded in match_id or
    provided via a separate mapping. For now, returns a single
    pooled Q under key "pooled".
    """
    return {"pooled": estimate_Q(intervals)}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _apply_additivity_shrinkage(
    Q: np.ndarray, alpha: float
) -> np.ndarray:
    """Shrink sparse-state transitions toward additivity assumption.

    Additivity: q_{1→3} ≈ q_{0→2}, q_{2→3} ≈ q_{0→1}
    """
    Q_shrunk = Q.copy()

    # q_{1→3} shrink toward q_{0→2}
    if Q[0, 2] > 0:
        Q_shrunk[1, 3] = (1 - alpha) * Q[1, 3] + alpha * Q[0, 2]

    # q_{2→3} shrink toward q_{0→1}
    if Q[0, 1] > 0:
        Q_shrunk[2, 3] = (1 - alpha) * Q[2, 3] + alpha * Q[0, 1]

    return Q_shrunk
