"""Step 2.5: Live Engine Initialization.

Loads Phase 1 parameters, precomputes matrix exponentials,
normalizes Q_off, and assembles the initial model instance
ready for Phase 3.

Input:  Phase 1 production params dir, Step 2.3 result, Step 2.4 result
Output: LiveModelInstance (ready-to-trade state)

Reference: phase2.md -> Step 2.5
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import scipy.linalg

from src.common.logging import get_logger
from src.common.types import SanityResult
from src.prematch.step_2_3_a_parameter import AParameterResult

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Model instance
# ---------------------------------------------------------------------------

@dataclass
class LiveModelInstance:
    """Complete state for the live trading engine at kickoff."""

    # Time state
    current_time: float = 0.0
    engine_phase: str = "WAITING_FOR_KICKOFF"
    T_exp: float = 98.0  # 90 + E[alpha_1] + E[alpha_2]

    # Match state
    current_state: int = 0       # Markov state X (0 = 11v11)
    current_score: tuple[int, int] = (0, 0)
    delta_S: int = 0

    # Intensity parameters
    a_H: float = -3.5
    a_A: float = -3.5
    b: np.ndarray = field(default_factory=lambda: np.zeros(6))
    gamma_H: np.ndarray = field(default_factory=lambda: np.zeros(4))
    gamma_A: np.ndarray = field(default_factory=lambda: np.zeros(4))
    delta_H: np.ndarray = field(default_factory=lambda: np.zeros(5))
    delta_A: np.ndarray = field(default_factory=lambda: np.zeros(5))
    C_time: float = 98.0

    # Markov model
    Q: np.ndarray = field(default_factory=lambda: np.zeros((4, 4)))
    Q_off_normalized: np.ndarray = field(default_factory=lambda: np.zeros((4, 4)))
    P_grid: dict[int, np.ndarray] = field(default_factory=dict)
    P_fine_grid: dict[int, np.ndarray] = field(default_factory=dict)

    # Phase 3 mode controls
    delta_significant: bool = False
    preliminary_cache: dict = field(default_factory=dict)

    # Event state machine
    event_state: str = "IDLE"
    cooldown: bool = False
    ob_freeze: bool = False

    # Connectivity
    match_id: str = ""
    live_score_ready: bool = False
    live_odds_healthy: bool = False
    kalshi_healthy: bool = False

    # Sanity check result
    sanity_verdict: str = "GO"
    delta_match_winner: float = 0.0
    delta_over_under: float = 0.0

    # Risk parameters
    bankroll: float = 0.0
    f_order_cap: float = 0.03
    f_match_cap: float = 0.05
    f_total_cap: float = 0.20

    # Expected stoppage times
    E_alpha_1: float = 3.0
    E_alpha_2: float = 5.0


# ---------------------------------------------------------------------------
# Parameter loading
# ---------------------------------------------------------------------------

def load_phase1_params(params_dir: str) -> dict:
    """Load Phase 1 production parameters from disk.

    Expected directory structure:
        params_dir/
        ├── params.json    (b, gamma_H, gamma_A, delta_H, delta_A)
        ├── Q.npy
        └── validation_report.json  (optional, for delta_lrt_pass)

    Returns:
        Dict with all loaded parameters.
    """
    p = Path(params_dir)

    with open(p / "params.json") as f:
        params = json.load(f)

    Q = np.load(str(p / "Q.npy"))

    # Load validation report for DELTA_SIGNIFICANT flag
    delta_significant = False
    report_path = p / "validation_report.json"
    if report_path.exists():
        with open(report_path) as f:
            report = json.load(f)
            delta_significant = report.get("delta_lrt_pass", False)

    return {
        "b": np.array(params["b"]),
        "gamma_H": np.array(params["gamma_H"]),
        "gamma_A": np.array(params["gamma_A"]),
        "delta_H": np.array(params["delta_H"]),
        "delta_A": np.array(params["delta_A"]),
        "Q": Q,
        "delta_significant": delta_significant,
    }


# ---------------------------------------------------------------------------
# Matrix exponential precomputation
# ---------------------------------------------------------------------------

def precompute_P_grid(Q: np.ndarray) -> dict[int, np.ndarray]:
    """Precompute P(dt) = expm(Q * dt) for dt = 0..100 minutes.

    Used for O(1) analytic mode lookups in Phase 3.
    """
    P_grid: dict[int, np.ndarray] = {}
    for dt_min in range(101):
        P_grid[dt_min] = scipy.linalg.expm(Q * dt_min)
    return P_grid


def precompute_P_fine_grid(Q: np.ndarray) -> dict[int, np.ndarray]:
    """Precompute fine-grained P(dt) for final 5 minutes.

    10-second increments: dt_10sec = 0..30 (0 to 5 minutes).
    """
    P_fine_grid: dict[int, np.ndarray] = {}
    for dt_10sec in range(31):
        dt_min = dt_10sec / 6.0
        P_fine_grid[dt_10sec] = scipy.linalg.expm(Q * dt_min)
    return P_fine_grid


# ---------------------------------------------------------------------------
# Q_off normalization
# ---------------------------------------------------------------------------

def normalize_Q_off(Q: np.ndarray) -> np.ndarray:
    """Normalize off-diagonal Q entries into transition probabilities.

    For MC simulation: given a transition occurs from state i,
    Q_off_normalized[i,j] = probability of transitioning to state j.

    Q_off is team-independent (one matrix, no home/away split).
    """
    n = Q.shape[0]
    Q_off = np.zeros((n, n))
    for i in range(n):
        total_off_diag = -Q[i, i]
        if total_off_diag > 0:
            for j in range(n):
                if i != j:
                    Q_off[i, j] = Q[i, j] / total_off_diag
    return Q_off


# ---------------------------------------------------------------------------
# Full initialization
# ---------------------------------------------------------------------------

def initialize_model(
    params_dir: str,
    a_result: AParameterResult,
    sanity_result: SanityResult,
    match_id: str,
    E_alpha_1: float = 3.0,
    E_alpha_2: float = 5.0,
    bankroll: float = 0.0,
) -> LiveModelInstance:
    """Full Step 2.5: load params, precompute, initialize model.

    Args:
        params_dir: Path to Phase 1 production parameters.
        a_result: Output from Step 2.3 (a_H, a_A, C_time, mu).
        sanity_result: Output from Step 2.4.
        match_id: Goalserve match ID.
        E_alpha_1: Expected first-half stoppage time.
        E_alpha_2: Expected second-half stoppage time.
        bankroll: Current account balance.

    Returns:
        Fully initialized LiveModelInstance.
    """
    # Load Phase 1 parameters
    phase1 = load_phase1_params(params_dir)

    log.info(
        "phase1_params_loaded",
        b_shape=phase1["b"].shape,
        Q_shape=phase1["Q"].shape,
        delta_significant=phase1["delta_significant"],
    )

    # Precompute matrix exponentials
    Q = phase1["Q"]
    P_grid = precompute_P_grid(Q)
    P_fine_grid = precompute_P_fine_grid(Q)

    log.info(
        "matrix_exponentials_precomputed",
        P_grid_size=len(P_grid),
        P_fine_grid_size=len(P_fine_grid),
    )

    # Normalize Q_off
    Q_off_normalized = normalize_Q_off(Q)

    # Compute T_exp
    T_exp = 90.0 + E_alpha_1 + E_alpha_2

    return LiveModelInstance(
        # Time state
        current_time=0.0,
        engine_phase="WAITING_FOR_KICKOFF",
        T_exp=T_exp,
        # Match state
        current_state=0,
        current_score=(0, 0),
        delta_S=0,
        # Intensity parameters
        a_H=a_result.a_H,
        a_A=a_result.a_A,
        b=phase1["b"],
        gamma_H=phase1["gamma_H"],
        gamma_A=phase1["gamma_A"],
        delta_H=phase1["delta_H"],
        delta_A=phase1["delta_A"],
        C_time=a_result.C_time,
        # Markov model
        Q=Q,
        Q_off_normalized=Q_off_normalized,
        P_grid=P_grid,
        P_fine_grid=P_fine_grid,
        # Phase 3 controls
        delta_significant=phase1["delta_significant"],
        preliminary_cache={},
        # Event state
        event_state="IDLE",
        cooldown=False,
        ob_freeze=False,
        # Connectivity
        match_id=match_id,
        live_score_ready=False,
        live_odds_healthy=False,
        kalshi_healthy=False,
        # Sanity
        sanity_verdict=sanity_result.verdict,
        delta_match_winner=sanity_result.delta_match_winner,
        delta_over_under=sanity_result.delta_over_under,
        # Risk
        bankroll=bankroll,
        # Stoppage
        E_alpha_1=E_alpha_1,
        E_alpha_2=E_alpha_2,
    )
