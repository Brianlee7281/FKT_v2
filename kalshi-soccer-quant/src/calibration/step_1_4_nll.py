"""Step 1.4 — Joint NLL Optimization (MMPP Calibration).

Jointly optimize time profile b, red-card penalty γ, score-difference
effect δ, and match-level baseline intensity a using PyTorch.

λ_H(t) = exp(a_H + b_{i(t)} + γ^H_{X(t)} + δ_H(ΔS(t)))
λ_A(t) = exp(a_A + b_{i(t)} + γ^A_{X(t)} + δ_A(ΔS(t)))

Parameters: a_H[M], a_A[M], b[6], γ^H[2], γ^A[2], δ_H[4], δ_A[4]
Total: 2M + 18 free parameters

Reference: phase1.md → Step 1.4
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import torch
import torch.nn as nn

from src.common.types import IntervalRecord
from src.calibration.step_1_1_intervals import HALFTIME_BREAK


# ---------------------------------------------------------------------------
# Time basis function assignment
# ---------------------------------------------------------------------------

# 6 piecewise-constant time bins
N_TIME_BINS = 6


def assign_time_bin(t: float, alpha_1: float = 0.0) -> int:
    """Assign a time point to one of 6 basis function bins.

    Bins (using raw match time, not effective time):
        0: [0, 15)        — early first half
        1: [15, 30)       — mid first half
        2: [30, 45+α₁)    — late first half + stoppage
        3: [HT_end, HT_end+15)   — early second half
        4: [HT_end+15, HT_end+30) — mid second half
        5: [HT_end+30, ...)       — late second half + stoppage

    Returns bin index 0-5, or -1 if in halftime.
    """
    ht_start = 45.0 + alpha_1
    ht_end = ht_start + HALFTIME_BREAK

    if t < 15.0:
        return 0
    elif t < 30.0:
        return 1
    elif t < ht_start:
        return 2
    elif t < ht_end:
        return -1  # Halftime — excluded
    elif t < ht_end + 15.0:
        return 3
    elif t < ht_end + 30.0:
        return 4
    else:
        return 5


def delta_S_to_index(delta_S: int) -> int:
    """Map ΔS to δ parameter index.

    δ indices: 0 → ΔS ≤ -2, 1 → ΔS = -1, 2 → ΔS = +1, 3 → ΔS ≥ +2
    ΔS = 0 is the reference point (δ = 0, not parameterized).
    Returns -1 for ΔS = 0 (no δ contribution).
    """
    if delta_S <= -2:
        return 0
    elif delta_S == -1:
        return 1
    elif delta_S == 0:
        return -1  # Reference: δ(0) = 0
    elif delta_S == 1:
        return 2
    else:  # >= +2
        return 3


# ---------------------------------------------------------------------------
# Clamping bounds
# ---------------------------------------------------------------------------

CLAMP_BOUNDS = {
    "b": (-0.5, 0.5),
    "gamma_H_1": (-1.5, 0.0),   # Home dismissed → home scoring down
    "gamma_H_2": (0.0, 1.5),    # Away dismissed → home scoring up
    "gamma_A_1": (0.0, 1.5),    # Home dismissed → away scoring up
    "gamma_A_2": (-1.5, 0.0),   # Away dismissed → away scoring down
    "delta_H_neg2": (-0.5, 1.0),
    "delta_H_neg1": (-0.5, 1.0),
    "delta_H_pos1": (-1.0, 0.5),
    "delta_H_pos2": (-1.0, 0.5),
    "delta_A_neg2": (-1.0, 0.5),
    "delta_A_neg1": (-1.0, 0.5),
    "delta_A_pos1": (-0.5, 1.0),
    "delta_A_pos2": (-0.5, 1.0),
}


# ---------------------------------------------------------------------------
# Preprocessed interval data for PyTorch
# ---------------------------------------------------------------------------

@dataclass
class MatchData:
    """Preprocessed match data for NLL computation."""
    match_idx: int
    intervals: list[_IntervalTensor]
    a_H_init: float = -3.5   # ln(μ_H / T_m)
    a_A_init: float = -3.5


@dataclass
class _IntervalTensor:
    """Single interval data, ready for tensor computation."""
    bin_durations: list[tuple[int, float]]  # [(bin_index, duration), ...] per time bin
    state_X: int            # Markov state 0-3
    delta_S: int            # Score difference
    # Goal data — all lists are aligned by goal index (includes own goals)
    goal_time_bins: list[int]      # Time bin for each goal
    goal_delta_indices: list[int]  # δ index for each goal's pre-goal ΔS
    goal_is_home: list[bool]       # Whether each goal is home
    goal_is_owngoal: list[bool]    # Own goal flags
    is_halftime: bool = False


def _split_by_time_bins(
    t_start: float, t_end: float, alpha_1: float,
) -> list[tuple[int, float]]:
    """Split an interval [t_start, t_end) into per-bin sub-intervals.

    Returns list of (bin_index, duration) pairs. Bins with zero duration
    are omitted. Halftime portions (bin=-1) are omitted.
    """
    ht_start = 45.0 + alpha_1
    ht_end = ht_start + HALFTIME_BREAK

    # Bin boundaries in raw match time
    boundaries = [
        (0, 0.0, 15.0),
        (1, 15.0, 30.0),
        (2, 30.0, ht_start),
        (-1, ht_start, ht_end),       # Halftime — excluded
        (3, ht_end, ht_end + 15.0),
        (4, ht_end + 15.0, ht_end + 30.0),
        (5, ht_end + 30.0, float("inf")),
    ]

    result = []
    for bin_idx, b_start, b_end in boundaries:
        if bin_idx == -1:
            continue  # Skip halftime bin
        overlap_start = max(t_start, b_start)
        overlap_end = min(t_end, b_end)
        duration = overlap_end - overlap_start
        if duration > 1e-9:
            result.append((bin_idx, duration))

    return result


def preprocess_intervals(
    intervals: list[IntervalRecord],
    a_init_map: dict[str, tuple[float, float]] | None = None,
) -> list[MatchData]:
    """Convert IntervalRecords into MatchData for NLL computation.

    Args:
        intervals: Output of Step 1.1.
        a_init_map: {match_id: (a_H_init, a_A_init)} from Step 1.3.
    """
    # Group by match
    by_match: dict[str, list[IntervalRecord]] = {}
    for iv in intervals:
        by_match.setdefault(iv.match_id, []).append(iv)

    match_data_list = []
    for idx, (match_id, match_ivs) in enumerate(sorted(by_match.items())):
        sorted_ivs = sorted(match_ivs, key=lambda iv: iv.t_start)

        a_H_init, a_A_init = -3.5, -3.5
        if a_init_map and match_id in a_init_map:
            a_H_init, a_A_init = a_init_map[match_id]

        iv_tensors = []
        for iv in sorted_ivs:
            # Split interval duration across time bins
            if iv.is_halftime:
                bin_durations = []  # No integration for halftime
            else:
                bin_durations = _split_by_time_bins(iv.t_start, iv.t_end, iv.alpha_1)

            # Build aligned goal lists (all goals including own goals)
            goal_time_bins = []
            goal_delta_indices = []
            goal_is_home = []
            goal_is_owngoal_list = []

            # Process home goals
            for gi, gt in enumerate(iv.home_goal_times):
                is_og = iv.goal_is_owngoal[gi] if gi < len(iv.goal_is_owngoal) else False
                gb = assign_time_bin(gt, iv.alpha_1)
                pre_delta = iv.goal_delta_before[gi] if gi < len(iv.goal_delta_before) else iv.delta_S
                di = delta_S_to_index(pre_delta)

                goal_time_bins.append(gb)
                goal_delta_indices.append(di)
                goal_is_home.append(True)
                goal_is_owngoal_list.append(is_og)

            # Process away goals
            offset = len(iv.home_goal_times)
            for gi, gt in enumerate(iv.away_goal_times):
                abs_gi = offset + gi
                is_og = iv.goal_is_owngoal[abs_gi] if abs_gi < len(iv.goal_is_owngoal) else False
                gb = assign_time_bin(gt, iv.alpha_1)
                pre_delta = iv.goal_delta_before[abs_gi] if abs_gi < len(iv.goal_delta_before) else iv.delta_S
                di = delta_S_to_index(pre_delta)

                goal_time_bins.append(gb)
                goal_delta_indices.append(di)
                goal_is_home.append(False)
                goal_is_owngoal_list.append(is_og)

            iv_tensors.append(_IntervalTensor(
                bin_durations=bin_durations,
                state_X=iv.state_X,
                delta_S=iv.delta_S,
                goal_time_bins=goal_time_bins,
                goal_delta_indices=goal_delta_indices,
                goal_is_home=goal_is_home,
                goal_is_owngoal=goal_is_owngoal_list,
                is_halftime=iv.is_halftime,
            ))

        match_data_list.append(MatchData(
            match_idx=idx,
            intervals=iv_tensors,
            a_H_init=a_H_init,
            a_A_init=a_A_init,
        ))

    return match_data_list


# ---------------------------------------------------------------------------
# PyTorch MMPP Loss Module
# ---------------------------------------------------------------------------

class MMPPLoss(nn.Module):
    """MMPP Negative Log-Likelihood with regularization (vectorized).

    Learnable parameters:
        a_H[M], a_A[M]: match-level baseline intensities
        b[6]:            time-interval scoring profile
        gamma_H[2]:      home-team red-card penalty (γ^H_1, γ^H_2)
        gamma_A[2]:      away-team red-card penalty (γ^A_1, γ^A_2)
        delta_H[4]:      home score-difference effect
        delta_A[4]:      away score-difference effect

    All interval/goal data is pre-indexed into tensors at init time,
    so forward() uses pure tensor ops with no Python loops.
    """

    def __init__(
        self,
        match_data: list[MatchData],
        sigma_a: float = 1.0,
        lambda_reg: float = 0.01,
    ):
        super().__init__()
        self.sigma_a = sigma_a
        self.lambda_reg = lambda_reg

        M = len(match_data)

        # Match-level baselines initialized from ML prior
        a_H_init = torch.tensor([md.a_H_init for md in match_data], dtype=torch.float64)
        a_A_init = torch.tensor([md.a_A_init for md in match_data], dtype=torch.float64)
        self.register_buffer("a_H_init", a_H_init)
        self.register_buffer("a_A_init", a_A_init)

        self.a_H = nn.Parameter(a_H_init.clone())
        self.a_A = nn.Parameter(a_A_init.clone())

        # Shared parameters
        self.b = nn.Parameter(torch.zeros(N_TIME_BINS, dtype=torch.float64))
        # Initialize gamma slightly off-zero so they aren't stuck on the
        # clamp boundary.  Signs match football intuition:
        #   gamma_H = [home_dismissed→home_down, away_dismissed→home_up]
        #   gamma_A = [home_dismissed→away_up, away_dismissed→away_down]
        self.gamma_H = nn.Parameter(torch.tensor([-0.05, 0.05], dtype=torch.float64))
        self.gamma_A = nn.Parameter(torch.tensor([0.05, -0.05], dtype=torch.float64))
        self.delta_H = nn.Parameter(torch.zeros(4, dtype=torch.float64))
        self.delta_A = nn.Parameter(torch.zeros(4, dtype=torch.float64))

        # --- Pre-build index tensors for vectorized forward ---

        # Integration sub-intervals: one entry per (interval, time_bin) pair
        int_match_list = []
        int_bin_list = []
        int_state_list = []
        int_delta_list = []   # shifted: original -1 → 0, original 0 → 1, etc.
        int_dur_list = []

        # Goal point events: one entry per non-own-goal
        goal_match_list = []
        goal_bin_list = []
        goal_state_list = []
        goal_delta_list = []
        goal_is_home_list = []

        for md in match_data:
            m = md.match_idx
            for iv in md.intervals:
                if iv.is_halftime or not iv.bin_durations:
                    continue

                d_s_idx = delta_S_to_index(iv.delta_S)
                d_s_shifted = d_s_idx + 1  # -1→0, 0→1, 1→2, 2→3, 3→4

                for bin_idx, duration in iv.bin_durations:
                    int_match_list.append(m)
                    int_bin_list.append(bin_idx)
                    int_state_list.append(iv.state_X)
                    int_delta_list.append(d_s_shifted)
                    int_dur_list.append(duration)

                for gi in range(len(iv.goal_is_home)):
                    if iv.goal_is_owngoal[gi]:
                        continue

                    gb = iv.goal_time_bins[gi]
                    if gb == -1:
                        gb = iv.bin_durations[0][0] if iv.bin_durations else 0
                    if gb < 0 or gb >= N_TIME_BINS:
                        gb = 0

                    g_delta_idx = iv.goal_delta_indices[gi]
                    g_delta_shifted = g_delta_idx + 1

                    goal_match_list.append(m)
                    goal_bin_list.append(gb)
                    goal_state_list.append(iv.state_X)
                    goal_delta_list.append(g_delta_shifted)
                    goal_is_home_list.append(iv.goal_is_home[gi])

        self.register_buffer("int_match", torch.tensor(int_match_list, dtype=torch.long))
        self.register_buffer("int_bin", torch.tensor(int_bin_list, dtype=torch.long))
        self.register_buffer("int_state", torch.tensor(int_state_list, dtype=torch.long))
        self.register_buffer("int_delta", torch.tensor(int_delta_list, dtype=torch.long))
        self.register_buffer("int_dur", torch.tensor(int_dur_list, dtype=torch.float64))

        self.register_buffer("goal_match", torch.tensor(goal_match_list, dtype=torch.long))
        self.register_buffer("goal_bin", torch.tensor(goal_bin_list, dtype=torch.long))
        self.register_buffer("goal_state", torch.tensor(goal_state_list, dtype=torch.long))
        self.register_buffer("goal_delta", torch.tensor(goal_delta_list, dtype=torch.long))
        self.register_buffer("goal_is_home", torch.tensor(goal_is_home_list, dtype=torch.bool))

    def _build_gamma_full(self):
        """Build [4] gamma vectors: [0, γ_1, γ_2, γ_1+γ_2]."""
        zero = torch.tensor([0.0], dtype=torch.float64)
        gamma_H_full = torch.cat([zero, self.gamma_H[:1], self.gamma_H[1:],
                                  (self.gamma_H[0] + self.gamma_H[1]).unsqueeze(0)])
        gamma_A_full = torch.cat([zero, self.gamma_A[:1], self.gamma_A[1:],
                                  (self.gamma_A[0] + self.gamma_A[1]).unsqueeze(0)])
        return gamma_H_full, gamma_A_full

    def _build_delta_full(self):
        """Build [5] delta vectors: [0, δ_0, δ_1, δ_2, δ_3] (index 0 = no effect)."""
        zero = torch.tensor([0.0], dtype=torch.float64)
        delta_H_full = torch.cat([zero, self.delta_H])
        delta_A_full = torch.cat([zero, self.delta_A])
        return delta_H_full, delta_A_full

    def forward(self) -> torch.Tensor:
        """Compute total loss = NLL + regularization (fully vectorized)."""
        gamma_H_full, gamma_A_full = self._build_gamma_full()
        delta_H_full, delta_A_full = self._build_delta_full()

        # --- Integration term (survival): sum of exp(lin) * duration ---
        a_h_int = self.a_H[self.int_match]
        a_a_int = self.a_A[self.int_match]
        b_int = self.b[self.int_bin]
        g_h_int = gamma_H_full[self.int_state]
        g_a_int = gamma_A_full[self.int_state]
        d_h_int = delta_H_full[self.int_delta]
        d_a_int = delta_A_full[self.int_delta]

        # Clamp exponents to prevent exp() overflow/underflow
        lin_h = torch.clamp(a_h_int + b_int + g_h_int + d_h_int, -20.0, 20.0)
        lin_a = torch.clamp(a_a_int + b_int + g_a_int + d_a_int, -20.0, 20.0)
        mu_h = torch.exp(lin_h) * self.int_dur
        mu_a = torch.exp(lin_a) * self.int_dur
        nll = torch.sum(mu_h) + torch.sum(mu_a)

        # --- Point-event term (goals): sum of ln(lambda) ---
        if self.goal_match.numel() > 0:
            a_h_g = self.a_H[self.goal_match]
            a_a_g = self.a_A[self.goal_match]
            b_g = self.b[self.goal_bin]
            g_h_g = gamma_H_full[self.goal_state]
            g_a_g = gamma_A_full[self.goal_state]
            d_h_g = delta_H_full[self.goal_delta]
            d_a_g = delta_A_full[self.goal_delta]

            ln_lam_h = a_h_g + b_g + g_h_g + d_h_g
            ln_lam_a = a_a_g + b_g + g_a_g + d_a_g
            ln_lam = torch.where(self.goal_is_home, ln_lam_h, ln_lam_a)
            nll -= torch.sum(ln_lam)

        # ML Prior regularization: (a - a_init)^2 / (2σ²)
        reg_a = (1.0 / (2.0 * self.sigma_a ** 2)) * (
            torch.sum((self.a_H - self.a_H_init) ** 2)
            + torch.sum((self.a_A - self.a_A_init) ** 2)
        )

        # L2 regularization on shared parameters (exclude gamma — clamp
        # bounds already constrain them, and with only ~17% red-card matches
        # the gradient signal is too weak to overcome L2)
        reg_l2 = self.lambda_reg * (
            torch.sum(self.b ** 2)
            + torch.sum(self.delta_H ** 2)
            + torch.sum(self.delta_A ** 2)
        )

        return nll + reg_a + reg_l2

    def clamp_parameters(self) -> None:
        """Clamp all parameters to physically meaningful bounds."""
        with torch.no_grad():
            lo, hi = CLAMP_BOUNDS["b"]
            self.b.clamp_(lo, hi)

            self.gamma_H[0].clamp_(*CLAMP_BOUNDS["gamma_H_1"])
            self.gamma_H[1].clamp_(*CLAMP_BOUNDS["gamma_H_2"])
            self.gamma_A[0].clamp_(*CLAMP_BOUNDS["gamma_A_1"])
            self.gamma_A[1].clamp_(*CLAMP_BOUNDS["gamma_A_2"])

            self.delta_H[0].clamp_(*CLAMP_BOUNDS["delta_H_neg2"])
            self.delta_H[1].clamp_(*CLAMP_BOUNDS["delta_H_neg1"])
            self.delta_H[2].clamp_(*CLAMP_BOUNDS["delta_H_pos1"])
            self.delta_H[3].clamp_(*CLAMP_BOUNDS["delta_H_pos2"])

            self.delta_A[0].clamp_(*CLAMP_BOUNDS["delta_A_neg2"])
            self.delta_A[1].clamp_(*CLAMP_BOUNDS["delta_A_neg1"])
            self.delta_A[2].clamp_(*CLAMP_BOUNDS["delta_A_pos1"])
            self.delta_A[3].clamp_(*CLAMP_BOUNDS["delta_A_pos2"])


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@dataclass
class TrainingResult:
    """Output of NLL optimization."""
    b: np.ndarray                   # (6,) time profile
    gamma_H: np.ndarray             # (2,) → full (4,) via additivity
    gamma_A: np.ndarray             # (2,) → full (4,) via additivity
    delta_H: np.ndarray             # (4,) score-difference effect
    delta_A: np.ndarray             # (4,) score-difference effect
    a_H: np.ndarray                 # (M,) corrected match baselines
    a_A: np.ndarray                 # (M,) corrected match baselines
    final_loss: float
    loss_history: list[float] = field(default_factory=list)


def train_nll(
    match_data: list[MatchData],
    sigma_a: float = 1.0,
    lambda_reg: float = 0.01,
    adam_lr: float = 1e-3,
    adam_epochs: int = 1000,
    lbfgs_epochs: int = 50,
    seed: int = 42,
) -> TrainingResult:
    """Train MMPP parameters via NLL minimization.

    Two-stage: Adam (coarse) → L-BFGS (fine-tuning).

    Args:
        match_data: Preprocessed from preprocess_intervals.
        sigma_a: Prior regularization strength.
        lambda_reg: L2 regularization.
        adam_lr: Adam learning rate.
        adam_epochs: Number of Adam epochs.
        lbfgs_epochs: Number of L-BFGS iterations.
        seed: Random seed.

    Returns:
        TrainingResult with optimized parameters.
    """
    torch.manual_seed(seed)

    model = MMPPLoss(match_data, sigma_a=sigma_a, lambda_reg=lambda_reg)
    model = model.double()

    loss_history = []

    # Stage 1: Adam
    optimizer_adam = torch.optim.Adam(model.parameters(), lr=adam_lr)
    for epoch in range(adam_epochs):
        optimizer_adam.zero_grad()
        loss = model()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0)
        optimizer_adam.step()
        model.clamp_parameters()

        if epoch % 100 == 0 or epoch == adam_epochs - 1:
            lv = loss.item()
            loss_history.append(lv)
            if math.isnan(lv):
                break

    # Stage 2: L-BFGS
    optimizer_lbfgs = torch.optim.LBFGS(
        model.parameters(),
        max_iter=20,
        line_search_fn="strong_wolfe",
    )

    for _ in range(lbfgs_epochs):
        def closure():
            optimizer_lbfgs.zero_grad()
            loss = model()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0)
            return loss

        loss = optimizer_lbfgs.step(closure)
        model.clamp_parameters()

        if loss is not None:
            lv = loss.item()
            loss_history.append(lv)
            if math.isnan(lv):
                break

    # Extract results
    final_loss = model().item()
    loss_history.append(final_loss)

    return TrainingResult(
        b=model.b.detach().numpy().copy(),
        gamma_H=model.gamma_H.detach().numpy().copy(),
        gamma_A=model.gamma_A.detach().numpy().copy(),
        delta_H=model.delta_H.detach().numpy().copy(),
        delta_A=model.delta_A.detach().numpy().copy(),
        a_H=model.a_H.detach().numpy().copy(),
        a_A=model.a_A.detach().numpy().copy(),
        final_loss=final_loss,
        loss_history=loss_history,
    )


def train_nll_multi_start(
    match_data: list[MatchData],
    n_starts: int = 5,
    **kwargs,
) -> TrainingResult:
    """Multi-start NLL optimization — run n_starts seeds, keep best.

    Args:
        match_data: Preprocessed intervals.
        n_starts: Number of random restarts.
        **kwargs: Passed to train_nll.

    Returns:
        TrainingResult with lowest final loss.
    """
    import gc

    best: TrainingResult | None = None

    for i in range(n_starts):
        result = train_nll(match_data, seed=42 + i * 7, **kwargs)
        if math.isnan(result.final_loss):
            continue
        if best is None or result.final_loss < best.final_loss:
            best = result
        gc.collect()

    if best is None:
        # All starts produced NaN — return last result as fallback
        best = result
    return best


# ---------------------------------------------------------------------------
# Parameter extraction helpers
# ---------------------------------------------------------------------------

def expand_gamma(gamma_2: np.ndarray) -> np.ndarray:
    """Expand 2-element gamma to full 4-state vector via additivity.

    gamma_2 = [γ_1, γ_2]
    Returns [0, γ_1, γ_2, γ_1 + γ_2]
    """
    return np.array([0.0, gamma_2[0], gamma_2[1], gamma_2[0] + gamma_2[1]])


def get_full_params(result: TrainingResult) -> dict[str, Any]:
    """Extract all parameters in a serializable format."""
    return {
        "b": result.b.tolist(),
        "gamma_H": expand_gamma(result.gamma_H).tolist(),
        "gamma_A": expand_gamma(result.gamma_A).tolist(),
        "gamma_H_raw": result.gamma_H.tolist(),
        "gamma_A_raw": result.gamma_A.tolist(),
        "delta_H": result.delta_H.tolist(),
        "delta_A": result.delta_A.tolist(),
        "final_loss": result.final_loss,
    }
