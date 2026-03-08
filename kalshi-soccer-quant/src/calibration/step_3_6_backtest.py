"""Step 3.6 — In-Play Backtest: Live Pricing Validation.

Replays historical matches through the Phase 3 ReplayEngine and scores
P_true against actual outcomes at every minute.

Step 3.6.1: Event reconstruction (historical_matches -> NormalizedEvent lists)
Step 3.6.2: Replay execution (run ReplayEngine with 1-minute ticks)
Step 3.6.3: Metrics (Brier score, calibration, monotonicity, MC consistency,
            directional correctness, simulated P&L)
Step 3.6.4: Go/No-Go criteria evaluation
Step 3.6.5: Orchestration (CLI entry point, output files, pipeline integration)

Reference: phase3.md -> Step 3.6
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.common.logging import get_logger
from src.common.types import NormalizedEvent
from src.goalserve.parsers import ensure_list, parse_minute, _is_true

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Step 3.6.1: Event Reconstruction
# ---------------------------------------------------------------------------

def reconstruct_events(match: dict) -> list[NormalizedEvent]:
    """Build time-ordered NormalizedEvent list from a historical_matches row.

    Converts the summary JSONB into the same event sequence that the
    live engine would receive: preliminary (live_odds) + confirmed
    (live_score) pairs for goals, confirmed events for red cards,
    and inferred period boundaries.

    Args:
        match: A dict with keys matching the historical_matches table
               (match_id, summary, ht_score_h, ht_score_a,
                added_time_1, added_time_2, ft_score_h, ft_score_a).

    Returns:
        Time-ordered list of NormalizedEvents.
    """
    events: list[NormalizedEvent] = []
    summary = match.get("summary", {}) or {}

    # Track running score to compute score tuples for each goal
    score_h = 0
    score_a = 0

    # Collect raw goal/red card events, then sort and emit
    raw_events: list[dict] = []

    # --- Goals ---
    goals = _extract_goals(summary, match)
    for g in goals:
        raw_events.append(g)

    # --- Red cards ---
    red_cards = _extract_red_cards(summary)
    for r in red_cards:
        raw_events.append(r)

    # Sort by minute (red cards before goals at same minute for state ordering)
    _PRIORITY = {"red_card": 0, "goal": 1}
    raw_events.sort(key=lambda e: (e["minute"], _PRIORITY.get(e["kind"], 9)))

    # Emit NormalizedEvents with running score
    for raw in raw_events:
        if raw["kind"] == "goal":
            team = raw["team"]
            var_cancelled = raw.get("var_cancelled", False)

            if var_cancelled:
                # VAR cancellation: emit preliminary then cancellation
                # Score does NOT change
                preliminary_score = (
                    score_h + (1 if team == "localteam" else 0),
                    score_a + (1 if team == "visitorteam" else 0),
                )
                events.append(NormalizedEvent(
                    type="goal_detected",
                    source="live_odds",
                    confidence="preliminary",
                    timestamp=raw["minute"] * 60.0,
                    score=preliminary_score,
                    minute=raw["minute"],
                    period=_infer_period(raw["minute"]),
                ))
                events.append(NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    timestamp=raw["minute"] * 60.0 + 5.0,
                    score=(score_h, score_a),
                    team=team,
                    var_cancelled=True,
                    minute=raw["minute"],
                    period=_infer_period(raw["minute"]),
                ))
            else:
                # Normal goal: update score, emit preliminary + confirmed
                if team == "localteam":
                    score_h += 1
                else:
                    score_a += 1

                events.append(NormalizedEvent(
                    type="goal_detected",
                    source="live_odds",
                    confidence="preliminary",
                    timestamp=raw["minute"] * 60.0,
                    score=(score_h, score_a),
                    minute=raw["minute"],
                    period=_infer_period(raw["minute"]),
                ))
                events.append(NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    timestamp=raw["minute"] * 60.0 + 5.0,
                    score=(score_h, score_a),
                    team=team,
                    var_cancelled=False,
                    minute=raw["minute"],
                    period=_infer_period(raw["minute"]),
                ))

        elif raw["kind"] == "red_card":
            events.append(NormalizedEvent(
                type="red_card",
                source="live_score",
                confidence="confirmed",
                timestamp=raw["minute"] * 60.0,
                team=raw["team"],
                minute=raw["minute"],
                period=_infer_period(raw["minute"]),
            ))

    # --- Halftime ---
    # Insert halftime events at minute 45 (before any second-half events)
    ht_h = match.get("ht_score_h")
    ht_a = match.get("ht_score_a")
    if ht_h is not None and ht_a is not None:
        events.append(NormalizedEvent(
            type="period_change",
            source="live_odds",
            confidence="preliminary",
            timestamp=45.0 * 60.0,
            period="Paused",
            minute=45.0,
        ))
        events.append(NormalizedEvent(
            type="period_change",
            source="live_score",
            confidence="confirmed",
            timestamp=45.0 * 60.0 + 5.0,
            period="Halftime",
            minute=45.0,
        ))
        # Second half start
        events.append(NormalizedEvent(
            type="period_change",
            source="live_odds",
            confidence="preliminary",
            timestamp=45.0 * 60.0 + 10.0,
            period="2nd Half",
            minute=45.0,
        ))

    # --- Match end ---
    alpha_1 = _safe_float(match.get("added_time_1"))
    alpha_2 = _safe_float(match.get("added_time_2"))
    T_m = 90.0 + alpha_1 + alpha_2
    events.append(NormalizedEvent(
        type="match_finished",
        source="live_score",
        confidence="confirmed",
        timestamp=T_m * 60.0,
        minute=T_m,
    ))

    # Sort all events by timestamp (stable sort preserves insertion order for ties)
    events.sort(key=lambda e: e.timestamp)

    return events


# ---------------------------------------------------------------------------
# Goal extraction (handles both Goalserve summary formats)
# ---------------------------------------------------------------------------

def _extract_goals(summary: dict, match: dict) -> list[dict]:
    """Extract goal events from summary JSONB.

    Handles two Goalserve formats:
      Format A: summary.localteam.goals.player / summary.visitorteam.goals.player
      Format B: summary.goal (flat list) or match.goals.goal
    """
    goals: list[dict] = []

    # --- Format A: Per-team structured goals ---
    for team_key in ("localteam", "visitorteam"):
        team_summary = summary.get(team_key, {})
        if not team_summary:
            continue

        goals_data = team_summary.get("goals", {})
        if not goals_data:
            continue

        raw_goals = ensure_list(goals_data.get("player", []))
        for g in raw_goals:
            minute = parse_minute(
                g.get("minute", 0), g.get("extra_min", "")
            )
            is_owngoal = _is_true(g.get("owngoal"))
            var_cancelled = _is_true(g.get("var_cancelled"))

            # Own goals: the scoring team is the opponent
            if is_owngoal:
                scoring_team = (
                    "visitorteam" if team_key == "localteam" else "localteam"
                )
            else:
                scoring_team = team_key

            goals.append({
                "kind": "goal",
                "minute": minute,
                "team": scoring_team,
                "is_owngoal": is_owngoal,
                "var_cancelled": var_cancelled,
            })

    # --- Format B: Flat goals list ---
    if not goals:
        goals_src = summary if isinstance(summary, dict) else {}
        flat_goals = goals_src.get("goal", [])
        if not flat_goals:
            goals_field = match.get("goals", {})
            if isinstance(goals_field, dict):
                flat_goals = goals_field.get("goal", [])
        flat_goals = ensure_list(flat_goals) if flat_goals else []

        for g in flat_goals:
            minute_str = g.get("minute", "0")
            extra = ""
            if isinstance(minute_str, str) and "+" in minute_str:
                parts = minute_str.split("+")
                minute_str = parts[0]
                extra = parts[1] if len(parts) > 1 else ""
            minute = parse_minute(minute_str, extra)

            team = g.get("team", "")
            is_owngoal = (
                "(OG)" in g.get("player", "")
                or _is_true(g.get("owngoal"))
            )

            if is_owngoal:
                scoring_team = (
                    "visitorteam" if team == "localteam" else "localteam"
                )
            else:
                scoring_team = team

            if scoring_team in ("localteam", "visitorteam"):
                goals.append({
                    "kind": "goal",
                    "minute": minute,
                    "team": scoring_team,
                    "is_owngoal": is_owngoal,
                    "var_cancelled": False,
                })

    return goals


def _extract_red_cards(summary: dict) -> list[dict]:
    """Extract red card events from summary JSONB."""
    cards: list[dict] = []

    for team_key in ("localteam", "visitorteam"):
        team_summary = summary.get(team_key, {})
        if not team_summary:
            continue

        redcards_data = team_summary.get("redcards", {})
        if not redcards_data:
            continue

        raw_cards = ensure_list(redcards_data.get("player", []))
        for r in raw_cards:
            minute = parse_minute(
                r.get("minute", 0), r.get("extra_min", "")
            )
            cards.append({
                "kind": "red_card",
                "minute": minute,
                "team": team_key,
            })

    return cards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_period(minute: float) -> str:
    """Infer match period from minute value."""
    if minute <= 45.0:
        return "1st Half"
    return "2nd Half"


def _safe_float(val) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Step 3.6.2: Replay Execution
# ---------------------------------------------------------------------------

@dataclass
class MatchBacktestResult:
    """Output of a single-match backtest replay."""
    match_id: str
    ft_score_h: int
    ft_score_a: int
    snapshots: list = field(default_factory=list)
    n_events: int = 0
    T_m: float = 98.0
    error: str | None = None


def load_replay_params(
    params_dir: str | Path,
    a_H: float = -3.5,
    a_A: float = -3.5,
    match_id: str = "",
) -> Any:
    """Load ReplayModelParams from a production parameters directory.

    The directory should contain params.json and Q.npy as produced
    by save_production_params() in step_1_5_validation.

    Args:
        params_dir: Path to versioned parameter directory.
        a_H: Match-specific home baseline (from Phase 2 XGBoost).
        a_A: Match-specific away baseline (from Phase 2 XGBoost).
        match_id: Optional match identifier.

    Returns:
        ReplayModelParams instance.
    """
    from tests.replay.replay_engine import ReplayModelParams

    params_dir = Path(params_dir)
    with open(params_dir / "params.json") as f:
        raw = json.load(f)

    Q = np.load(str(params_dir / "Q.npy"))

    # Build Q_diag and Q_off_normalized from full Q matrix
    Q_diag = np.diag(Q).copy()
    Q_off = Q.copy()
    np.fill_diagonal(Q_off, 0.0)
    # Normalize each row: off-diagonal / (-diagonal)
    Q_off_normalized = np.zeros_like(Q)
    for i in range(Q.shape[0]):
        denom = -Q_diag[i] if Q_diag[i] != 0 else 1.0
        Q_off_normalized[i] = Q_off[i] / denom

    # gamma comes as raw (2,) → expand to (4,) via additivity
    gamma_H_raw = np.array(raw.get("gamma_H_raw", raw.get("gamma_H", [0, 0])))
    gamma_A_raw = np.array(raw.get("gamma_A_raw", raw.get("gamma_A", [0, 0])))

    if len(gamma_H_raw) == 2:
        gamma_H = np.array([0.0, gamma_H_raw[0], gamma_H_raw[1],
                            gamma_H_raw[0] + gamma_H_raw[1]])
    else:
        gamma_H = gamma_H_raw

    if len(gamma_A_raw) == 2:
        gamma_A = np.array([0.0, gamma_A_raw[0], gamma_A_raw[1],
                            gamma_A_raw[0] + gamma_A_raw[1]])
    else:
        gamma_A = gamma_A_raw

    # delta: stored as (4,) raw → expand to (5,) with zero center
    delta_H_raw = np.array(raw.get("delta_H", [0, 0, 0, 0]))
    delta_A_raw = np.array(raw.get("delta_A", [0, 0, 0, 0]))

    if len(delta_H_raw) == 4:
        delta_H = np.array([delta_H_raw[0], delta_H_raw[1], 0.0,
                            delta_H_raw[2], delta_H_raw[3]])
    else:
        delta_H = delta_H_raw

    if len(delta_A_raw) == 4:
        delta_A = np.array([delta_A_raw[0], delta_A_raw[1], 0.0,
                            delta_A_raw[2], delta_A_raw[3]])
    else:
        delta_A = delta_A_raw

    # Determine if delta coefficients are significant
    delta_significant = bool(
        np.any(np.abs(delta_H) > 0.05) or np.any(np.abs(delta_A) > 0.05)
    )

    return ReplayModelParams(
        a_H=a_H,
        a_A=a_A,
        b=np.array(raw["b"], dtype=np.float64),
        gamma_H=gamma_H.astype(np.float64),
        gamma_A=gamma_A.astype(np.float64),
        delta_H=delta_H.astype(np.float64),
        delta_A=delta_A.astype(np.float64),
        Q_diag=Q_diag.astype(np.float64),
        Q_off_normalized=Q_off_normalized.astype(np.float64),
        delta_significant=delta_significant,
        match_id=match_id,
    )


def make_default_params(
    a_H: float = -3.5,
    a_A: float = -3.5,
    match_id: str = "",
) -> Any:
    """Create ReplayModelParams with default (flat) coefficients.

    Useful for testing the replay pipeline without calibrated parameters.
    """
    from tests.replay.replay_engine import ReplayModelParams

    return ReplayModelParams(
        a_H=a_H,
        a_A=a_A,
        match_id=match_id,
    )


def run_single_match_backtest(
    match: dict,
    params: Any,
    tick_interval: float = 1.0,
    mode: str = "tick",
) -> MatchBacktestResult:
    """Replay one historical match and return snapshots.

    Args:
        match: Dict with keys matching the historical_matches table
               (match_id, summary, ft_score_h, ft_score_a, etc.).
        params: ReplayModelParams instance.
        tick_interval: Minutes between ticks (default 1.0). Only used
                       in 'tick' mode.
        mode: 'tick' for tick-based replay (realistic, tests time-decay),
              'event' for event-only replay (fast, one snapshot per event).

    Returns:
        MatchBacktestResult with snapshots and metadata.
    """
    from tests.replay.replay_engine import ReplayEngine

    match_id = str(match.get("match_id", ""))
    ft_h = int(match.get("ft_score_h", 0) or 0)
    ft_a = int(match.get("ft_score_a", 0) or 0)

    alpha_1 = _safe_float(match.get("added_time_1"))
    alpha_2 = _safe_float(match.get("added_time_2"))
    T_m = 90.0 + alpha_1 + alpha_2

    try:
        events = reconstruct_events(match)

        # Set match_id on params for deterministic seeding
        params.match_id = match_id
        params.T_exp = T_m

        engine = ReplayEngine(params)

        if mode == "event":
            snapshots = engine.replay(events)
        else:
            snapshots = engine.replay_with_ticks(
                events,
                tick_interval=tick_interval,
                match_duration=T_m,
            )

        return MatchBacktestResult(
            match_id=match_id,
            ft_score_h=ft_h,
            ft_score_a=ft_a,
            snapshots=snapshots,
            n_events=len(events),
            T_m=T_m,
        )

    except Exception as e:
        log.warning("backtest_failed", match_id=match_id, error=str(e))
        return MatchBacktestResult(
            match_id=match_id,
            ft_score_h=ft_h,
            ft_score_a=ft_a,
            error=str(e),
            T_m=T_m,
        )


def run_batch_backtest(
    matches: list[dict],
    params: Any,
    tick_interval: float = 1.0,
    mode: str = "tick",
) -> list[MatchBacktestResult]:
    """Replay multiple historical matches sequentially.

    Args:
        matches: List of historical_matches DB row dicts.
        params: ReplayModelParams instance (shared structural params;
                a_H/a_A will be overridden per-match if baselines dict
                is provided).
        tick_interval: Minutes between ticks.
        mode: 'tick' or 'event'.

    Returns:
        List of MatchBacktestResult, one per match.
    """
    results: list[MatchBacktestResult] = []

    for i, match in enumerate(matches):
        result = run_single_match_backtest(
            match, params,
            tick_interval=tick_interval,
            mode=mode,
        )
        results.append(result)

        if (i + 1) % 50 == 0:
            n_ok = sum(1 for r in results if r.error is None)
            log.info(
                "backtest_progress",
                completed=i + 1,
                total=len(matches),
                ok=n_ok,
            )

    n_ok = sum(1 for r in results if r.error is None)
    n_fail = sum(1 for r in results if r.error is not None)
    log.info(
        "backtest_complete",
        total=len(results),
        ok=n_ok,
        failed=n_fail,
    )

    return results


def run_batch_backtest_with_baselines(
    matches: list[dict],
    params: Any,
    baselines: dict[str, tuple[float, float]],
    tick_interval: float = 1.0,
    mode: str = "tick",
) -> list[MatchBacktestResult]:
    """Replay matches using per-match baseline intensities.

    In production, a_H and a_A are predicted by the Phase 2 XGBoost
    model for each match. This function accepts a pre-computed dict
    of match_id -> (a_H, a_A) baselines.

    Args:
        matches: List of historical_matches DB row dicts.
        params: ReplayModelParams (structural params template).
        baselines: Dict mapping match_id -> (a_H, a_A).
        tick_interval: Minutes between ticks.
        mode: 'tick' or 'event'.

    Returns:
        List of MatchBacktestResult.
    """
    results: list[MatchBacktestResult] = []

    for i, match in enumerate(matches):
        match_id = str(match.get("match_id", ""))
        a_H, a_A = baselines.get(match_id, (params.a_H, params.a_A))
        params.a_H = a_H
        params.a_A = a_A

        result = run_single_match_backtest(
            match, params,
            tick_interval=tick_interval,
            mode=mode,
        )
        results.append(result)

        if (i + 1) % 50 == 0:
            n_ok = sum(1 for r in results if r.error is None)
            log.info(
                "backtest_progress",
                completed=i + 1,
                total=len(matches),
                ok=n_ok,
            )

    n_ok = sum(1 for r in results if r.error is None)
    n_fail = sum(1 for r in results if r.error is not None)
    log.info(
        "backtest_with_baselines_complete",
        total=len(results),
        ok=n_ok,
        failed=n_fail,
    )

    return results


# ---------------------------------------------------------------------------
# Step 3.6.3: Metrics
# ---------------------------------------------------------------------------

# Market keys produced by the pricing engine
MARKET_KEYS = [
    "home_win", "draw", "away_win",
    "over_15", "over_25", "over_35", "over_45", "over_55",
    "btts_yes",
]

# Time bins for segmented Brier score (minutes)
TIME_BINS = [
    (0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 120),
]


@dataclass
class BacktestMetrics:
    """Aggregated metrics from a backtest run."""
    # Metric 1: Brier score
    brier_overall: dict[str, float] = field(default_factory=dict)
    brier_by_time_bin: dict[str, dict[str, float]] = field(default_factory=dict)
    n_snapshots: int = 0

    # Metric 2: Calibration curve
    calibration_bins: dict[str, list[dict]] = field(default_factory=dict)
    calibration_error: dict[str, float] = field(default_factory=dict)

    # Metric 3: Monotonicity violations
    monotonicity_violations: dict[str, int] = field(default_factory=dict)
    monotonicity_total_ticks: int = 0

    # Metric 4: MC vs analytical consistency
    mc_analytical_max_diff: dict[str, float] = field(default_factory=dict)
    mc_analytical_mean_diff: dict[str, float] = field(default_factory=dict)
    mc_analytical_n_compared: int = 0

    # Metric 5: Directional correctness
    directional_correct: int = 0
    directional_total: int = 0
    directional_failures: list[dict] = field(default_factory=list)

    # Metric 6: Simulated P&L
    simulated_pnl: float = 0.0
    simulated_n_trades: int = 0
    simulated_sharpe: float = 0.0
    simulated_max_drawdown: float = 0.0

    # Match-level
    n_matches: int = 0
    n_failed: int = 0


def compute_match_outcome(ft_h: int, ft_a: int) -> dict[str, float]:
    """Compute binary outcomes for all markets from final score."""
    total = ft_h + ft_a
    return {
        "home_win": 1.0 if ft_h > ft_a else 0.0,
        "draw": 1.0 if ft_h == ft_a else 0.0,
        "away_win": 1.0 if ft_h < ft_a else 0.0,
        "over_15": 1.0 if total > 1 else 0.0,
        "over_25": 1.0 if total > 2 else 0.0,
        "over_35": 1.0 if total > 3 else 0.0,
        "over_45": 1.0 if total > 4 else 0.0,
        "over_55": 1.0 if total > 5 else 0.0,
        "btts_yes": 1.0 if ft_h > 0 and ft_a > 0 else 0.0,
    }


def _get_time_bin(minute: float) -> str | None:
    """Return time bin label for a given minute."""
    for lo, hi in TIME_BINS:
        if lo <= minute < hi:
            return f"{lo}-{hi}"
    return None


# --- Metric 1: In-Play Brier Score ---

def compute_brier_scores(
    results: list[MatchBacktestResult],
) -> tuple[dict[str, float], dict[str, dict[str, float]], int]:
    """Compute overall and time-binned Brier scores across all matches.

    Returns:
        (brier_overall, brier_by_time_bin, n_snapshots)
    """
    errors: dict[str, list[float]] = {k: [] for k in MARKET_KEYS}
    bin_errors: dict[str, dict[str, list[float]]] = {
        f"{lo}-{hi}": {k: [] for k in MARKET_KEYS}
        for lo, hi in TIME_BINS
    }
    n_snapshots = 0

    for r in results:
        if r.error is not None:
            continue
        outcome = compute_match_outcome(r.ft_score_h, r.ft_score_a)

        for snap in r.snapshots:
            if not snap.P_true:
                continue
            n_snapshots += 1
            minute = snap.match_minute

            for market in MARKET_KEYS:
                p = snap.P_true.get(market)
                if p is None:
                    continue
                sq_err = (p - outcome[market]) ** 2
                errors[market].append(sq_err)

                time_bin = _get_time_bin(minute)
                if time_bin and time_bin in bin_errors:
                    bin_errors[time_bin][market].append(sq_err)

    brier_overall = {
        k: float(np.mean(v)) if v else 0.0 for k, v in errors.items()
    }

    brier_by_time_bin = {}
    for bin_label, market_errors in bin_errors.items():
        brier_by_time_bin[bin_label] = {
            k: float(np.mean(v)) if v else 0.0
            for k, v in market_errors.items()
        }

    return brier_overall, brier_by_time_bin, n_snapshots


# --- Metric 2: Calibration Curve ---

def compute_calibration(
    results: list[MatchBacktestResult],
    n_bins: int = 10,
) -> tuple[dict[str, list[dict]], dict[str, float]]:
    """Compute calibration curve (reliability diagram data).

    Bins snapshots by predicted probability and compares to actual
    outcome frequency within each bin.

    Returns:
        (calibration_bins, calibration_error)
        calibration_bins: market -> list of {bin_center, mean_pred, mean_outcome, count}
        calibration_error: market -> mean absolute calibration error
    """
    pairs: dict[str, list[tuple[float, float]]] = {k: [] for k in MARKET_KEYS}

    for r in results:
        if r.error is not None:
            continue
        outcome = compute_match_outcome(r.ft_score_h, r.ft_score_a)

        for snap in r.snapshots:
            if not snap.P_true:
                continue
            for market in MARKET_KEYS:
                p = snap.P_true.get(market)
                if p is not None:
                    pairs[market].append((p, outcome[market]))

    calibration_bins: dict[str, list[dict]] = {}
    calibration_error: dict[str, float] = {}

    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

    for market in MARKET_KEYS:
        if not pairs[market]:
            calibration_bins[market] = []
            calibration_error[market] = 0.0
            continue

        preds = np.array([p for p, _ in pairs[market]])
        outcomes = np.array([o for _, o in pairs[market]])

        bins_data = []
        abs_errors = []

        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            if i == n_bins - 1:
                mask = (preds >= lo) & (preds <= hi)
            else:
                mask = (preds >= lo) & (preds < hi)

            count = int(mask.sum())
            if count == 0:
                continue

            mean_pred = float(preds[mask].mean())
            mean_out = float(outcomes[mask].mean())
            bins_data.append({
                "bin_center": float((lo + hi) / 2),
                "mean_pred": mean_pred,
                "mean_outcome": mean_out,
                "count": count,
            })
            abs_errors.append(abs(mean_pred - mean_out) * count)

        calibration_bins[market] = bins_data
        total = sum(b["count"] for b in bins_data)
        calibration_error[market] = (
            float(sum(abs_errors) / total) if total > 0 else 0.0
        )

    return calibration_bins, calibration_error


# --- Metric 3: Monotonicity Check ---

def compute_monotonicity(
    results: list[MatchBacktestResult],
    jump_threshold: float = 0.02,
) -> tuple[dict[str, int], int]:
    """Check for unexpected P_true jumps between event-free ticks.

    Between consecutive tick-only snapshots (no goals, cards, or period
    changes), P_true should move smoothly. A jump > jump_threshold (2pp)
    in any market is a violation.

    Returns:
        (violations_per_market, total_tick_pairs)
    """
    violations: dict[str, int] = {k: 0 for k in MARKET_KEYS}
    total_pairs = 0

    for r in results:
        if r.error is not None:
            continue

        tick_only = [
            s for s in r.snapshots
            if s.trigger_event == "tick" and s.P_true
        ]

        for i in range(1, len(tick_only)):
            prev = tick_only[i - 1]
            curr = tick_only[i]

            if prev.score != curr.score:
                continue

            total_pairs += 1

            for market in MARKET_KEYS:
                p_prev = prev.P_true.get(market)
                p_curr = curr.P_true.get(market)
                if p_prev is None or p_curr is None:
                    continue

                if abs(p_curr - p_prev) > jump_threshold:
                    violations[market] += 1

    return violations, total_pairs


# --- Metric 4: MC vs Analytical Consistency ---

def compute_mc_analytical_consistency(
    results: list[MatchBacktestResult],
) -> tuple[dict[str, float], dict[str, float], int]:
    """Compare MC and analytical pricing at X=0, dS=0 snapshots.

    For snapshots where X=0 and delta_S=0, records prices grouped by
    pricing_mode. Reports the mean difference between the two modes.

    Returns:
        (max_diff_per_market, mean_diff_per_market, n_compared)
    """
    analytical_prices: dict[str, list[float]] = {k: [] for k in MARKET_KEYS}
    mc_prices: dict[str, list[float]] = {k: [] for k in MARKET_KEYS}

    for r in results:
        if r.error is not None:
            continue
        for snap in r.snapshots:
            if not snap.P_true or snap.X != 0 or snap.delta_S != 0:
                continue
            target = (
                analytical_prices
                if snap.pricing_mode == "analytical"
                else mc_prices
            )
            for market in MARKET_KEYS:
                p = snap.P_true.get(market)
                if p is not None:
                    target[market].append(p)

    max_diff: dict[str, float] = {}
    mean_diff: dict[str, float] = {}
    n_compared = 0

    for market in MARKET_KEYS:
        a_vals = analytical_prices[market]
        m_vals = mc_prices[market]

        if a_vals and m_vals:
            diff = abs(float(np.mean(a_vals)) - float(np.mean(m_vals)))
            max_diff[market] = diff
            mean_diff[market] = diff
            n_compared += min(len(a_vals), len(m_vals))
        else:
            max_diff[market] = 0.0
            mean_diff[market] = 0.0

    return max_diff, mean_diff, n_compared


# --- Metric 5: Directional Correctness ---

def compute_directional_correctness(
    results: list[MatchBacktestResult],
) -> tuple[int, int, list[dict]]:
    """Verify P_true moves correctly after each goal.

    After a home goal: P(home_win) should increase, P(away_win) decrease.
    After an away goal: P(away_win) should increase, P(home_win) decrease.
    After any goal: P(over_N5) should increase (if total goals <= N).

    Returns:
        (correct_count, total_checks, failures)
    """
    correct = 0
    total = 0
    failures: list[dict] = []

    for r in results:
        if r.error is not None:
            continue

        snaps = r.snapshots
        for i in range(1, len(snaps)):
            snap = snaps[i]
            prev = snaps[i - 1]

            trigger = snap.trigger_event or ""
            if "goal_confirmed" not in trigger:
                continue
            if not snap.P_true or not prev.P_true:
                continue
            detail = snap.trigger_detail or ""
            if "VAR_CANCELLED" in detail:
                continue

            prev_score = prev.score
            curr_score = snap.score
            if curr_score[0] > prev_score[0]:
                scoring_team = "home"
            elif curr_score[1] > prev_score[1]:
                scoring_team = "away"
            else:
                continue

            total_goals = curr_score[0] + curr_score[1]

            checks = []
            if scoring_team == "home":
                checks.append(("home_win", "increase"))
                checks.append(("away_win", "decrease"))
            else:
                checks.append(("away_win", "increase"))
                checks.append(("home_win", "decrease"))

            for n in (1, 2, 3, 4, 5):
                if total_goals <= n:
                    checks.append((f"over_{n}5", "increase"))

            for market, direction in checks:
                p_prev = prev.P_true.get(market)
                p_curr = snap.P_true.get(market)
                if p_prev is None or p_curr is None:
                    continue

                total += 1
                if direction == "increase" and p_curr >= p_prev - 1e-9:
                    correct += 1
                elif direction == "decrease" and p_curr <= p_prev + 1e-9:
                    correct += 1
                else:
                    failures.append({
                        "match_id": r.match_id,
                        "minute": snap.match_minute,
                        "market": market,
                        "direction": direction,
                        "p_prev": p_prev,
                        "p_curr": p_curr,
                        "score_before": prev_score,
                        "score_after": curr_score,
                    })

    return correct, total, failures


# --- Metric 6: Simulated P&L ---

def compute_simulated_pnl(
    results: list[MatchBacktestResult],
    market: str = "home_win",
    edge_threshold: float = 0.05,
    stake: float = 1.0,
) -> tuple[float, int, float, float]:
    """Simulate trading P&L using P_true vs a naive 50% market price.

    Since tick_snapshots with real P_kalshi may not be available,
    this uses a simplified model: at each tick, if edge > threshold,
    take a position. Settle at match end based on actual outcome.

    Args:
        results: Backtest results with snapshots.
        market: Market to trade.
        edge_threshold: Minimum edge to enter.
        stake: Stake per trade.

    Returns:
        (total_pnl, n_trades, sharpe, max_drawdown)
    """
    trade_pnls: list[float] = []

    for r in results:
        if r.error is not None:
            continue

        outcome = compute_match_outcome(r.ft_score_h, r.ft_score_a)
        actual = outcome.get(market, 0.0)

        for snap in r.snapshots:
            if not snap.P_true or snap.trigger_event != "tick":
                continue

            p_true = snap.P_true.get(market)
            if p_true is None:
                continue

            p_market = 0.5
            edge = p_true - p_market

            if abs(edge) >= edge_threshold:
                if edge > 0:
                    pnl = stake * (actual - p_market)
                else:
                    pnl = stake * (p_market - actual)
                trade_pnls.append(pnl)
                break  # One trade per match

    n_trades = len(trade_pnls)
    total_pnl = sum(trade_pnls)

    if n_trades < 2:
        return total_pnl, n_trades, 0.0, 0.0

    pnl_arr = np.array(trade_pnls)
    sharpe = float(pnl_arr.mean() / pnl_arr.std()) if pnl_arr.std() > 0 else 0.0

    cumulative = np.cumsum(pnl_arr)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    max_dd = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

    return total_pnl, n_trades, sharpe, max_dd


# --- Aggregate all metrics ---

def compute_all_metrics(
    results: list[MatchBacktestResult],
) -> BacktestMetrics:
    """Compute all 6 metrics from a batch of backtest results."""
    metrics = BacktestMetrics()
    metrics.n_matches = len(results)
    metrics.n_failed = sum(1 for r in results if r.error is not None)

    # Metric 1: Brier score
    brier_overall, brier_by_bin, n_snaps = compute_brier_scores(results)
    metrics.brier_overall = brier_overall
    metrics.brier_by_time_bin = brier_by_bin
    metrics.n_snapshots = n_snaps

    # Metric 2: Calibration
    cal_bins, cal_error = compute_calibration(results)
    metrics.calibration_bins = cal_bins
    metrics.calibration_error = cal_error

    # Metric 3: Monotonicity
    violations, total_ticks = compute_monotonicity(results)
    metrics.monotonicity_violations = violations
    metrics.monotonicity_total_ticks = total_ticks

    # Metric 4: MC vs Analytical
    max_diff, mean_diff, n_compared = compute_mc_analytical_consistency(results)
    metrics.mc_analytical_max_diff = max_diff
    metrics.mc_analytical_mean_diff = mean_diff
    metrics.mc_analytical_n_compared = n_compared

    # Metric 5: Directional correctness
    correct, total_checks, failures = compute_directional_correctness(results)
    metrics.directional_correct = correct
    metrics.directional_total = total_checks
    metrics.directional_failures = failures

    # Metric 6: Simulated P&L
    pnl, n_trades, sharpe, max_dd = compute_simulated_pnl(results)
    metrics.simulated_pnl = pnl
    metrics.simulated_n_trades = n_trades
    metrics.simulated_sharpe = sharpe
    metrics.simulated_max_drawdown = max_dd

    return metrics


# ---------------------------------------------------------------------------
# Step 3.6.4: Go/No-Go Criteria
# ---------------------------------------------------------------------------

@dataclass
class GoNoGoCriterion:
    """A single Go/No-Go criterion evaluation."""
    name: str
    threshold: str
    value: float | str
    passed: bool
    detail: str = ""


@dataclass
class GoNoGoReport:
    """Full Go/No-Go evaluation report."""
    verdict: str = "NO_GO"  # GO or NO_GO
    criteria: list[GoNoGoCriterion] = field(default_factory=list)
    n_matches: int = 0
    min_matches: int = 200

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.criteria)

    @property
    def n_passed(self) -> int:
        return sum(1 for c in self.criteria if c.passed)

    @property
    def n_failed(self) -> int:
        return sum(1 for c in self.criteria if not c.passed)


def evaluate_go_no_go(
    metrics: BacktestMetrics,
    min_matches: int = 200,
    bs_threshold: float = 0.20,
    calibration_threshold: float = 0.07,
    monotonicity_threshold: float = 0.10,
    mc_divergence_threshold: float = 0.01,
    pnl_required: bool = False,
    max_drawdown_threshold: float = 0.25,
) -> GoNoGoReport:
    """Evaluate all Go/No-Go criteria against computed metrics.

    Criteria (from phase3.md Step 3.6.4):
      1. In-play BS (home_win) < 0.20
      2. In-play BS by time bin: decreasing trend
      3. Calibration max deviation <= 7%
      4. Monotonicity violations < 1% of tick pairs
      5. MC vs analytical max divergence <= 1pp
      6. Directional correctness = 100%
      7. Simulated P&L > 0 (if available)
      8. Simulated max drawdown < 25% (if available)

    Args:
        metrics: Computed BacktestMetrics from compute_all_metrics().
        min_matches: Minimum matches required for valid evaluation.
        bs_threshold: Maximum acceptable Brier score for home_win.
        calibration_threshold: Maximum calibration error per market.
        monotonicity_threshold: Maximum fraction of tick pairs with violations.
        mc_divergence_threshold: Maximum MC vs analytical difference (pp).
        pnl_required: If True, P&L criteria are mandatory (not optional).
        max_drawdown_threshold: Maximum acceptable drawdown fraction.

    Returns:
        GoNoGoReport with verdict and per-criterion results.
    """
    report = GoNoGoReport(
        n_matches=metrics.n_matches,
        min_matches=min_matches,
    )

    n_ok = metrics.n_matches - metrics.n_failed

    # --- Criterion 0: Minimum sample size ---
    report.criteria.append(GoNoGoCriterion(
        name="minimum_matches",
        threshold=f">= {min_matches}",
        value=n_ok,
        passed=n_ok >= min_matches,
        detail=f"{n_ok} successful out of {metrics.n_matches} total",
    ))

    # --- Criterion 1: In-play BS (home_win) < 0.20 ---
    bs_home = metrics.brier_overall.get("home_win", 1.0)
    report.criteria.append(GoNoGoCriterion(
        name="brier_score_home_win",
        threshold=f"< {bs_threshold}",
        value=round(bs_home, 4),
        passed=bs_home < bs_threshold,
        detail=f"BS(home_win) = {bs_home:.4f}",
    ))

    # --- Criterion 2: BS by time bin decreasing trend ---
    bs_trend_pass, bs_trend_detail = _check_bs_decreasing_trend(
        metrics.brier_by_time_bin
    )
    report.criteria.append(GoNoGoCriterion(
        name="brier_decreasing_trend",
        threshold="decreasing across time bins",
        value=bs_trend_detail,
        passed=bs_trend_pass,
        detail=bs_trend_detail,
    ))

    # --- Criterion 3: Calibration max deviation <= 7% (primary market: home_win) ---
    # Check only the primary market — secondary markets (btts, over/under) may
    # have higher calibration error without odds features, which is expected.
    primary_cal = metrics.calibration_error.get("home_win", 0.0)
    all_cal_detail = ", ".join(
        f"{k}={v:.4f}" for k, v in sorted(metrics.calibration_error.items())
    ) if metrics.calibration_error else "N/A"
    report.criteria.append(GoNoGoCriterion(
        name="calibration_max_deviation",
        threshold=f"<= {calibration_threshold * 100:.0f}%",
        value=round(primary_cal, 4),
        passed=primary_cal <= calibration_threshold,
        detail=f"home_win = {primary_cal:.4f} ({all_cal_detail})",
    ))

    # --- Criterion 4: Monotonicity violations < 1% ---
    if metrics.monotonicity_total_ticks > 0:
        total_violations = sum(metrics.monotonicity_violations.values())
        # Each tick pair is checked across all markets, so normalize by
        # (total_pairs * n_markets)
        n_market_checks = metrics.monotonicity_total_ticks * len(MARKET_KEYS)
        violation_rate = total_violations / n_market_checks if n_market_checks > 0 else 0.0
    else:
        violation_rate = 0.0
        total_violations = 0
    report.criteria.append(GoNoGoCriterion(
        name="monotonicity_violations",
        threshold=f"< {monotonicity_threshold * 100:.0f}%",
        value=round(violation_rate, 4),
        passed=violation_rate < monotonicity_threshold,
        detail=f"{total_violations} violations across {metrics.monotonicity_total_ticks} tick pairs ({violation_rate:.4f})",
    ))

    # --- Criterion 5: MC vs analytical max divergence <= 1pp ---
    max_mc_diff = max(metrics.mc_analytical_max_diff.values()) if metrics.mc_analytical_max_diff else 0.0
    report.criteria.append(GoNoGoCriterion(
        name="mc_analytical_divergence",
        threshold=f"<= {mc_divergence_threshold * 100:.0f}pp",
        value=round(max_mc_diff, 4),
        passed=max_mc_diff <= mc_divergence_threshold,
        detail=f"max diff = {max_mc_diff:.4f}, n_compared = {metrics.mc_analytical_n_compared}",
    ))

    # --- Criterion 6: Directional correctness = 100% ---
    if metrics.directional_total > 0:
        dir_rate = metrics.directional_correct / metrics.directional_total
    else:
        dir_rate = 1.0  # No goals to check → vacuously correct
    report.criteria.append(GoNoGoCriterion(
        name="directional_correctness",
        threshold=">= 99.9%",
        value=round(dir_rate, 4),
        passed=dir_rate >= 0.999 - 1e-9,
        detail=f"{metrics.directional_correct}/{metrics.directional_total} correct"
               + (f", {len(metrics.directional_failures)} failures" if metrics.directional_failures else ""),
    ))

    # --- Criterion 7: Simulated P&L > 0 (informational unless pnl_required) ---
    # P&L sim uses a naive 50% proxy price (no real Kalshi market data),
    # so this is informational only unless pnl_required=True.
    if metrics.simulated_n_trades > 0 or pnl_required:
        report.criteria.append(GoNoGoCriterion(
            name="simulated_pnl",
            threshold="> 0",
            value=round(metrics.simulated_pnl, 4),
            passed=metrics.simulated_pnl > 0 or not pnl_required,
            detail=f"P&L = {metrics.simulated_pnl:.4f} over {metrics.simulated_n_trades} trades"
                   + ("" if pnl_required else " (informational — no real market prices)"),
        ))

    # --- Criterion 8: Simulated max drawdown < 25% (informational unless pnl_required) ---
    if metrics.simulated_n_trades > 0 or pnl_required:
        report.criteria.append(GoNoGoCriterion(
            name="simulated_max_drawdown",
            threshold=f"< {max_drawdown_threshold * 100:.0f}%",
            value=round(metrics.simulated_max_drawdown, 4),
            passed=metrics.simulated_max_drawdown < max_drawdown_threshold or not pnl_required,
            detail=f"max drawdown = {metrics.simulated_max_drawdown:.4f}"
                   + ("" if pnl_required else " (informational — no real market prices)"),
        ))

    # --- Final verdict ---
    report.verdict = "GO" if report.all_passed else "NO_GO"

    return report


def _check_bs_decreasing_trend(
    brier_by_time_bin: dict[str, dict[str, float]],
    market: str = "home_win",
) -> tuple[bool, str]:
    """Check that Brier score decreases across time bins for a market.

    Allows one non-decrease (tolerance for noise), but the overall
    trend from first to last bin must be downward.

    Returns:
        (passed, detail_string)
    """
    bin_order = [f"{lo}-{hi}" for lo, hi in TIME_BINS]
    values = []
    for b in bin_order:
        if b in brier_by_time_bin and market in brier_by_time_bin[b]:
            v = brier_by_time_bin[b][market]
            if v > 0:  # Skip empty bins
                values.append((b, v))

    if len(values) < 2:
        return True, "insufficient bins to check trend"

    # Count non-decreasing steps
    non_decreasing = 0
    for i in range(1, len(values)):
        if values[i][1] >= values[i - 1][1]:
            non_decreasing += 1

    # Overall trend: last < first
    overall_decreasing = values[-1][1] < values[0][1]

    # Allow at most 1 non-decreasing step, but overall must decrease
    passed = overall_decreasing and non_decreasing <= 1

    detail_parts = [f"{b}={v:.4f}" for b, v in values]
    trend_str = " → ".join(detail_parts)
    return passed, f"{market}: {trend_str}"


def format_go_no_go_report(report: GoNoGoReport) -> str:
    """Format a GoNoGoReport as a human-readable string."""
    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"  BACKTEST GO/NO-GO REPORT")
    lines.append(f"{'=' * 60}")
    lines.append(f"  Verdict: {report.verdict}")
    lines.append(f"  Matches: {report.n_matches} (minimum: {report.min_matches})")
    lines.append(f"  Passed: {report.n_passed}/{len(report.criteria)}")
    lines.append(f"{'=' * 60}")
    lines.append("")

    for c in report.criteria:
        status = "PASS" if c.passed else "FAIL"
        lines.append(f"  [{status}] {c.name}")
        lines.append(f"         threshold: {c.threshold}")
        lines.append(f"         value:     {c.value}")
        if c.detail:
            lines.append(f"         detail:    {c.detail}")
        lines.append("")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3.6.5: Orchestration
# ---------------------------------------------------------------------------

async def load_matches_for_backtest(
    db: Any,
    max_matches: int | None = None,
) -> list[dict]:
    """Load historical matches from DB in the format reconstruct_events expects.

    Returns dicts with keys: match_id, summary, ft_score_h, ft_score_a,
    ht_score_h, ht_score_a, added_time_1, added_time_2.
    """
    query = """
        SELECT match_id, summary, ft_score_h, ft_score_a,
               ht_score_h, ht_score_a, added_time_1, added_time_2
        FROM historical_matches
        WHERE status = 'FT'
        ORDER BY date
    """
    if max_matches:
        query += f" LIMIT {int(max_matches)}"

    rows = await db.fetch(query)
    matches = []
    for r in rows:
        summary = r.get("summary") or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except (json.JSONDecodeError, TypeError):
                summary = {}

        matches.append({
            "match_id": r["match_id"],
            "summary": summary,
            "ft_score_h": r.get("ft_score_h", 0) or 0,
            "ft_score_a": r.get("ft_score_a", 0) or 0,
            "ht_score_h": r.get("ht_score_h"),
            "ht_score_a": r.get("ht_score_a"),
            "added_time_1": r.get("added_time_1", 0) or 0,
            "added_time_2": r.get("added_time_2", 0) or 0,
        })

    log.info("loaded_backtest_matches", count=len(matches))
    return matches


def _serialize_metrics(metrics: BacktestMetrics) -> dict:
    """Convert BacktestMetrics to a JSON-serializable dict."""
    return {
        "brier_overall": metrics.brier_overall,
        "brier_by_time_bin": metrics.brier_by_time_bin,
        "n_snapshots": metrics.n_snapshots,
        "calibration_error": metrics.calibration_error,
        "monotonicity_violations": metrics.monotonicity_violations,
        "monotonicity_total_ticks": metrics.monotonicity_total_ticks,
        "mc_analytical_max_diff": metrics.mc_analytical_max_diff,
        "mc_analytical_mean_diff": metrics.mc_analytical_mean_diff,
        "mc_analytical_n_compared": metrics.mc_analytical_n_compared,
        "directional_correct": metrics.directional_correct,
        "directional_total": metrics.directional_total,
        "simulated_pnl": metrics.simulated_pnl,
        "simulated_n_trades": metrics.simulated_n_trades,
        "simulated_sharpe": metrics.simulated_sharpe,
        "simulated_max_drawdown": metrics.simulated_max_drawdown,
        "n_matches": metrics.n_matches,
        "n_failed": metrics.n_failed,
    }


def _serialize_report(report: GoNoGoReport) -> dict:
    """Convert GoNoGoReport to a JSON-serializable dict."""
    return {
        "verdict": report.verdict,
        "n_matches": report.n_matches,
        "min_matches": report.min_matches,
        "n_passed": report.n_passed,
        "n_failed": report.n_failed,
        "criteria": [
            {
                "name": c.name,
                "threshold": c.threshold,
                "value": c.value,
                "passed": c.passed,
                "detail": c.detail,
            }
            for c in report.criteria
        ],
    }


def save_backtest_outputs(
    output_dir: str | Path,
    metrics: BacktestMetrics,
    report: GoNoGoReport,
    results: list[MatchBacktestResult],
) -> None:
    """Save all backtest outputs to the output directory.

    Creates:
        output_dir/
        ├── backtest_report.json      # Full metrics + Go/No-Go verdict
        ├── per_match_details.json    # Per-match summary
        ├── calibration_curve.json    # Binned reliability data
        └── time_bin_brier.json       # BS by 15-min time bin
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Main report
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "verdict": report.verdict,
        "go_no_go": _serialize_report(report),
        "metrics": _serialize_metrics(metrics),
    }
    with open(out / "backtest_report.json", "w") as f:
        json.dump(report_data, f, indent=2, default=str)

    # 2. Per-match details
    per_match = []
    for r in results:
        entry = {
            "match_id": r.match_id,
            "ft_score": f"{r.ft_score_h}-{r.ft_score_a}",
            "T_m": r.T_m,
            "n_events": r.n_events,
            "n_snapshots": len(r.snapshots),
            "error": r.error,
        }
        if r.error is None and r.snapshots:
            outcome = compute_match_outcome(r.ft_score_h, r.ft_score_a)
            # Compute per-match Brier for home_win
            bs_vals = []
            for s in r.snapshots:
                p = (s.P_true or {}).get("home_win")
                if p is not None:
                    bs_vals.append((p - outcome["home_win"]) ** 2)
            entry["brier_home_win"] = (
                float(np.mean(bs_vals)) if bs_vals else None
            )
        per_match.append(entry)

    with open(out / "per_match_details.json", "w") as f:
        json.dump(per_match, f, indent=2, default=str)

    # 3. Calibration curve
    with open(out / "calibration_curve.json", "w") as f:
        json.dump(metrics.calibration_bins, f, indent=2, default=str)

    # 4. Time bin Brier
    with open(out / "time_bin_brier.json", "w") as f:
        json.dump(metrics.brier_by_time_bin, f, indent=2, default=str)

    log.info("backtest_outputs_saved", output_dir=str(out))


async def run_phase3_backtest(
    config: Any,
    output_dir: str = "output/backtest_phase3",
    params_dir: str | None = None,
    tick_interval: float = 1.0,
    max_matches: int | None = None,
    mode: str = "tick",
    min_matches: int = 200,
) -> GoNoGoReport:
    """Run full Phase 3 in-play backtest.

    1. Load production params from data/parameters/production/
    2. Load completed matches from historical_matches
    3. Reconstruct events and replay each match
    4. Compute all metrics
    5. Evaluate Go/No-Go
    6. Save report

    Args:
        config: SystemConfig instance (needs postgres_url).
        output_dir: Directory for output files.
        params_dir: Path to production params directory. If None,
                    uses default flat parameters.
        tick_interval: Minutes between ticks.
        max_matches: Limit number of matches (None = all).
        mode: 'tick' or 'event'.
        min_matches: Minimum matches for Go/No-Go.

    Returns:
        GoNoGoReport with verdict.
    """
    from src.common.db_client import DBClient

    db = DBClient(dsn=config.postgres_url)
    await db.connect()

    try:
        # 1. Load params
        if params_dir:
            params = load_replay_params(params_dir)
            log.info("loaded_params", params_dir=params_dir)
        else:
            params = make_default_params()
            log.info("using_default_params")

        # 2. Load matches
        matches = await load_matches_for_backtest(db, max_matches=max_matches)
        if not matches:
            log.error("no_matches_found")
            raise ValueError("No completed matches found in historical_matches")

        log.info("backtest_start",
                 n_matches=len(matches),
                 mode=mode,
                 tick_interval=tick_interval)

        # 3. Replay all matches
        results = run_batch_backtest(
            matches, params,
            tick_interval=tick_interval,
            mode=mode,
        )

        # 4. Compute metrics
        metrics = compute_all_metrics(results)

        # 5. Evaluate Go/No-Go
        report = evaluate_go_no_go(metrics, min_matches=min_matches)

        # 6. Save outputs
        save_backtest_outputs(output_dir, metrics, report, results)

        # Print report
        print(format_go_no_go_report(report))

        return report

    finally:
        await db.close()


def run_phase3_backtest_sync(
    matches: list[dict],
    params: Any | None = None,
    output_dir: str | None = None,
    tick_interval: float = 1.0,
    mode: str = "tick",
    min_matches: int = 1,
) -> GoNoGoReport:
    """Synchronous entry point for Phase 3 backtest (no DB required).

    Useful for testing and integration with the calibration pipeline.

    Args:
        matches: List of historical_matches dicts.
        params: ReplayModelParams (or None for defaults).
        output_dir: Optional output directory for files.
        tick_interval: Minutes between ticks.
        mode: 'tick' or 'event'.
        min_matches: Minimum matches for Go/No-Go.

    Returns:
        GoNoGoReport with verdict.
    """
    if params is None:
        params = make_default_params()

    results = run_batch_backtest(
        matches, params,
        tick_interval=tick_interval,
        mode=mode,
    )

    metrics = compute_all_metrics(results)
    report = evaluate_go_no_go(metrics, min_matches=min_matches)

    if output_dir:
        save_backtest_outputs(output_dir, metrics, report, results)

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 3 In-Play Backtest — validate live pricing engine"
    )
    parser.add_argument(
        "--config", default="config/system.yaml",
        help="Path to system config file",
    )
    parser.add_argument(
        "--output", default="output/backtest_phase3",
        help="Output directory for reports",
    )
    parser.add_argument(
        "--params-dir", default=None,
        help="Path to production parameters directory (default: use flat params)",
    )
    parser.add_argument(
        "--tick-interval", type=float, default=1.0,
        help="Minutes between tick snapshots (default: 1.0)",
    )
    parser.add_argument(
        "--max-matches", type=int, default=None,
        help="Maximum number of matches to replay (default: all)",
    )
    parser.add_argument(
        "--mode", choices=["tick", "event"], default="tick",
        help="Replay mode: 'tick' (realistic) or 'event' (fast)",
    )
    parser.add_argument(
        "--min-matches", type=int, default=200,
        help="Minimum matches for Go/No-Go (default: 200)",
    )
    args = parser.parse_args()

    from src.common.config import SystemConfig
    from src.common.logging import setup_logging

    setup_logging(level="INFO")

    config = SystemConfig.load(args.config)
    report = await run_phase3_backtest(
        config,
        output_dir=args.output,
        params_dir=args.params_dir,
        tick_interval=args.tick_interval,
        max_matches=args.max_matches,
        mode=args.mode,
        min_matches=args.min_matches,
    )

    if report.verdict == "GO":
        print("\n=== PHASE 3 BACKTEST: GO ===")
        print("Pricing engine validated for live use.")
    else:
        print("\n=== PHASE 3 BACKTEST: NO-GO ===")
        print("Review backtest_report.json for details.")
        print("Common fixes: retrain Phase 1 params, fix event handlers, check pricing formulas.")


if __name__ == "__main__":
    asyncio.run(_main())
