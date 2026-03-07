"""Phase 1 Calibration Pipeline — End-to-end orchestration.

Wires Steps 1.1-1.5 together:
  1. Load historical matches from DB
  2. Build intervals (Step 1.1)
  3. Estimate Q matrix (Step 1.2)
  4. Train ML prior XGBoost (Step 1.3)
  5. Joint NLL optimization (Step 1.4)
  6. Walk-forward validation + Go/No-Go (Step 1.5)

Usage:
    python -m src.calibration.pipeline [--config config/system.yaml]
"""

from __future__ import annotations

import asyncio
import argparse
import gc
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.common.config import SystemConfig
from src.common.db_client import DBClient
from src.common.logging import get_logger, setup_logging

from src.calibration.step_1_1_intervals import build_intervals, build_intervals_from_db_row
from src.calibration.step_1_2_Q_matrix import estimate_Q
# Import step_1_4 (torch) BEFORE step_1_3 (xgboost) to avoid segfault
# on Apple Silicon due to library initialization conflict
from src.calibration.step_1_4_nll import (
    preprocess_intervals,
    train_nll_multi_start,
    TrainingResult,
    get_full_params,
)
from src.calibration.step_1_3_ml_prior import (
    MLPriorArtifacts,
    MatchFeatureRow,
    train_ml_prior,
    predict_expected_goals,
    convert_to_initial_a,
    assemble_features,
)
from src.calibration.step_1_5_validation import (
    FoldResult,
    GoNoGoReport,
    evaluate_go_no_go,
    save_production_params,
    brier_score,
    delta_brier_score,
    log_loss,
    calibration_max_deviation,
    validate_gamma_signs,
    simulate_pnl,
    poisson_match_winner_probs,
    poisson_over_under,
    poisson_btts,
)
from src.common.types import IntervalRecord

log = get_logger(__name__)

# Default match duration for a_init conversion
T_MATCH = 90.0

# Walk-forward season folds (train → validate)
DEFAULT_FOLDS = [
    {"train": ["2020-2021", "2021-2022", "2022-2023"], "val": ["2023-2024"]},
    {"train": ["2021-2022", "2022-2023", "2023-2024"], "val": ["2024-2025"]},
    {"train": ["2022-2023", "2023-2024", "2024-2025"], "val": ["2025-2026"]},
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

async def load_matches_from_db(db: DBClient) -> list[dict]:
    """Load all historical matches from PostgreSQL."""
    rows = await db.fetch("""
        SELECT match_id, league_id, date, home_team, away_team,
               ft_score_h, ft_score_a, ht_score_h, ht_score_a,
               added_time_1, added_time_2, status, summary,
               stats, player_stats, odds, lineups
        FROM historical_matches
        WHERE status = 'FT'
        ORDER BY date
    """)
    matches = []
    for r in rows:
        # Convert DB row to match dict compatible with build_intervals
        goals_data = {}
        if r["summary"]:
            try:
                goals_data = json.loads(r["summary"]) if isinstance(r["summary"], str) else r["summary"]
            except (json.JSONDecodeError, TypeError):
                pass

        # Parse JSON columns
        stats_data = _parse_json_col(r["stats"])
        player_stats_data = _parse_json_col(r["player_stats"])
        odds_data = _parse_json_col(r["odds"])
        lineups_data = _parse_json_col(r["lineups"])

        match = {
            "id": r["match_id"],
            "static_id": r["match_id"],
            "league_id": r["league_id"],
            "date": r["date"].strftime("%d.%m.%Y") if r["date"] else "",
            "status": r["status"],
            "localteam": {
                "name": r["home_team"],
                "ft_score": str(r["ft_score_h"] or 0),
                "score": str(r["ft_score_h"] or 0),
            },
            "visitorteam": {
                "name": r["away_team"],
                "ft_score": str(r["ft_score_a"] or 0),
                "score": str(r["ft_score_a"] or 0),
            },
            "halftime": {
                "score": f"{r['ht_score_h'] or 0} - {r['ht_score_a'] or 0}",
            },
            "goals": goals_data,
            "_date_obj": r["date"],
            "_ft_h": r["ft_score_h"] or 0,
            "_ft_a": r["ft_score_a"] or 0,
            "_stats": stats_data,
            "_player_stats": player_stats_data,
            "_odds": odds_data,
            "_lineups": lineups_data,
        }
        matches.append(match)

    log.info("loaded_matches", count=len(matches))
    return matches


def _parse_json_col(val: Any) -> dict:
    """Parse a JSON/JSONB column value into a dict."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def get_season(date_obj) -> str:
    """Derive season string from match date (e.g. 2023-2024)."""
    if date_obj is None:
        return "unknown"
    year = date_obj.year
    month = date_obj.month
    if month >= 7:
        return f"{year}-{year+1}"
    else:
        return f"{year-1}-{year}"


def split_by_season(matches: list[dict]) -> dict[str, list[dict]]:
    """Group matches by season."""
    by_season: dict[str, list[dict]] = {}
    for m in matches:
        s = get_season(m.get("_date_obj"))
        by_season.setdefault(s, []).append(m)
    return by_season


# ---------------------------------------------------------------------------
# Feature row building (for ML prior)
# ---------------------------------------------------------------------------

ROLLING_WINDOW = 5


def _get_team_stats(match: dict, team_key: str) -> dict:
    """Extract team-level stats for one side from a match's _stats column.

    The stats column from commentaries has structure:
      {"localteam": {...}, "visitorteam": {...}}
    Or from fixture extraction:
      {"home_goals": X, ...}  (flat format — not useful for tier1)
    """
    stats = match.get("_stats", {})
    if not stats:
        return {}
    # Commentaries format: nested under team keys
    team_data = stats.get(team_key, {})
    if isinstance(team_data, dict) and team_data:
        return team_data
    return {}


def _get_player_ids_from_lineups(match: dict, side: str) -> list[str]:
    """Extract starting XI player IDs from lineups data."""
    lineups = match.get("_lineups", {})
    if not lineups:
        return []
    side_data = lineups.get(side, {})
    if not side_data:
        return []
    players = side_data.get("players", [])
    return [p.get("id", "") for p in players if p.get("id")]


def _get_player_stats_list(match: dict, team_key: str) -> list[dict]:
    """Extract per-player stats from player_stats column."""
    ps = match.get("_player_stats", {})
    if not ps:
        return []
    team_data = ps.get(team_key, {})
    if not team_data:
        return []
    players = team_data.get("player", [])
    if isinstance(players, dict):
        players = [players]
    return players


def _get_bookmakers(match: dict) -> list[dict]:
    """Extract bookmaker list from odds column."""
    odds = match.get("_odds", {})
    if not odds:
        return []
    if isinstance(odds, list):
        return odds
    # Could be nested under a key
    bms = odds.get("bookmaker", odds.get("bookmakers", []))
    if isinstance(bms, dict):
        bms = [bms]
    return bms if isinstance(bms, list) else []


def build_feature_rows(matches: list[dict]) -> list[MatchFeatureRow]:
    """Build MatchFeatureRows from loaded matches using rolling windows.

    For each match, assembles features for both home and away teams using
    each team's last N matches of stats as the rolling window.
    """
    from collections import defaultdict

    # Sort by date
    sorted_matches = sorted(matches, key=lambda m: m.get("_date_obj") or "")

    # Rolling history per team: team_name -> list of recent team stats dicts
    team_stats_history: dict[str, list[dict]] = defaultdict(list)
    # Per-player rolling stats: player_id -> list of recent player stat dicts
    player_stats_history: dict[str, list[dict]] = defaultdict(list)
    # Last match date per team (for rest days)
    team_last_date: dict[str, str] = {}

    rows: list[MatchFeatureRow] = []

    for match in sorted_matches:
        match_id = match["id"]
        home_team = match["localteam"]["name"]
        away_team = match["visitorteam"]["name"]
        match_date = match.get("date", "")

        # Get current rolling windows BEFORE updating with this match
        home_recent = list(team_stats_history[home_team][-ROLLING_WINDOW:])
        away_recent = list(team_stats_history[away_team][-ROLLING_WINDOW:])

        home_player_ids = _get_player_ids_from_lineups(match, "home")
        away_player_ids = _get_player_ids_from_lineups(match, "away")

        bookmakers = _get_bookmakers(match)

        # Build features for home team
        home_feats = assemble_features(
            team_stats=home_recent,
            player_ids=home_player_ids or None,
            player_history=dict(player_stats_history) if home_player_ids else None,
            bookmakers=bookmakers,
            is_home=True,
            match_date=match_date,
            team_prev_date=team_last_date.get(home_team),
            opp_prev_date=team_last_date.get(away_team),
        )
        rows.append(MatchFeatureRow(
            features=home_feats,
            target_goals=match["_ft_h"],
            match_id=match_id,
            team="home",
        ))

        # Build features for away team
        away_feats = assemble_features(
            team_stats=away_recent,
            player_ids=away_player_ids or None,
            player_history=dict(player_stats_history) if away_player_ids else None,
            bookmakers=bookmakers,
            is_home=False,
            match_date=match_date,
            team_prev_date=team_last_date.get(away_team),
            opp_prev_date=team_last_date.get(home_team),
        )
        rows.append(MatchFeatureRow(
            features=away_feats,
            target_goals=match["_ft_a"],
            match_id=match_id,
            team="away",
        ))

        # Update rolling histories with this match's data
        home_stats = _get_team_stats(match, "localteam")
        away_stats = _get_team_stats(match, "visitorteam")
        if home_stats:
            team_stats_history[home_team].append(home_stats)
        if away_stats:
            team_stats_history[away_team].append(away_stats)

        # Update player stats history
        for player in _get_player_stats_list(match, "localteam"):
            pid = player.get("id", "")
            if pid:
                player_stats_history[pid].append(player)
        for player in _get_player_stats_list(match, "visitorteam"):
            pid = player.get("id", "")
            if pid:
                player_stats_history[pid].append(player)

        # Update last match date
        team_last_date[home_team] = match_date
        team_last_date[away_team] = match_date

    return rows


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def run_step_1_1(matches: list[dict]) -> list[IntervalRecord]:
    """Step 1.1: Build intervals from match data."""
    log.info("step_1_1_start", n_matches=len(matches))
    all_intervals = []
    failed = 0
    for match in matches:
        try:
            intervals = build_intervals(match)
            all_intervals.extend(intervals)
        except Exception:
            failed += 1

    log.info("step_1_1_done",
             n_intervals=len(all_intervals), failed=failed)
    return all_intervals


def run_step_1_2(intervals: list[IntervalRecord]) -> np.ndarray:
    """Step 1.2: Estimate Q matrix."""
    log.info("step_1_2_start")
    Q = estimate_Q(intervals)
    log.info("step_1_2_done", Q_shape=Q.shape)
    return Q


def run_step_1_3_simple(matches: list[dict]) -> dict[str, tuple[float, float]]:
    """Step 1.3 (simplified): Compute a_init from empirical scoring rates.

    Uses league-average goals as a simple prior when full feature data
    (team stats, player stats, odds) is not available.
    """
    log.info("step_1_3_start_simple")

    # Compute league-average goals per match
    total_h, total_a, n = 0, 0, 0
    for m in matches:
        total_h += m["_ft_h"]
        total_a += m["_ft_a"]
        n += 1

    avg_h = total_h / n if n > 0 else 1.3
    avg_a = total_a / n if n > 0 else 1.1

    # a_init = ln(mu / T_m) where mu is expected goals, T_m is match duration
    a_h_default = math.log(avg_h / T_MATCH)
    a_a_default = math.log(avg_a / T_MATCH)

    log.info("step_1_3_simple_rates",
             avg_h=round(avg_h, 3), avg_a=round(avg_a, 3),
             a_h=round(a_h_default, 4), a_a=round(a_a_default, 4))

    a_init_map = {}
    for m in matches:
        match_id = m["id"]
        # Use league-average for all matches (not match-specific actuals,
        # which would create circular dependency)
        a_init_map[match_id] = (a_h_default, a_a_default)

    log.info("step_1_3_done", n_matches=len(a_init_map))
    return a_init_map


def run_step_1_3_ml(
    matches: list[dict],
) -> tuple[dict[str, tuple[float, float]], MLPriorArtifacts | None]:
    """Step 1.3 (ML): Train XGBoost Poisson prior for per-match a_init.

    Falls back to simple prior if ML training fails.
    """
    log.info("step_1_3_ml_start", n_matches=len(matches))

    # Build feature rows with rolling windows
    feature_rows = build_feature_rows(matches)
    log.info("step_1_3_ml_features", n_rows=len(feature_rows))

    if len(feature_rows) < 100:
        log.warning("step_1_3_ml_fallback", reason="too few feature rows")
        return run_step_1_3_simple(matches), None

    try:
        artifacts = train_ml_prior(feature_rows)
        log.info("step_1_3_ml_trained",
                 n_features=len(artifacts.feature_names),
                 n_selected=len(artifacts.feature_mask))
    except Exception as e:
        log.warning("step_1_3_ml_train_failed", error=str(e))
        return run_step_1_3_simple(matches), None

    # Build a_init_map using trained model predictions
    a_init_map: dict[str, tuple[float, float]] = {}
    for row in feature_rows:
        if row.match_id in a_init_map and row.team == "away":
            # Second row for this match — update away
            mu_a = predict_expected_goals(artifacts, row.features)
            a_h, _ = a_init_map[row.match_id]
            a_init_map[row.match_id] = (a_h, convert_to_initial_a(mu_a))
        elif row.team == "home":
            mu_h = predict_expected_goals(artifacts, row.features)
            a_init_map[row.match_id] = (convert_to_initial_a(mu_h), -4.0)

    log.info("step_1_3_ml_done", n_matches=len(a_init_map))
    return a_init_map, artifacts


def run_step_1_4(
    intervals: list[IntervalRecord],
    a_init_map: dict[str, tuple[float, float]],
    n_starts: int = 5,
) -> TrainingResult:
    """Step 1.4: Joint NLL optimization."""
    log.info("step_1_4_start", n_starts=n_starts)

    match_data = preprocess_intervals(intervals, a_init_map)
    log.info("step_1_4_preprocessed", n_matches=len(match_data))

    result = train_nll_multi_start(
        match_data,
        n_starts=n_starts,
        adam_epochs=1000,
        adam_lr=1e-3,
        sigma_a=1.0,
    )

    params = get_full_params(result)
    log.info("step_1_4_done",
             best_nll=round(result.final_loss, 4),
             b=np.round(params["b"], 4).tolist())

    return result


def run_step_1_5_fold(
    train_matches: list[dict],
    val_matches: list[dict],
    fold_idx: int,
    n_starts: int = 3,
) -> tuple[FoldResult, TrainingResult]:
    """Run a single walk-forward fold: train on train_matches, validate on val_matches."""
    log.info("fold_start", fold=fold_idx,
             train_size=len(train_matches), val_size=len(val_matches))

    # Train ML prior on training data
    a_init_train, ml_artifacts = run_step_1_3_ml(train_matches)

    # Train MMPP
    train_intervals = run_step_1_1(train_matches)
    result = run_step_1_4(train_intervals, a_init_train, n_starts=n_starts)
    params = get_full_params(result)

    b = params["b"]
    bin_durations = [15.0, 15.0, 15.0, 15.0, 15.0, 15.0]  # 6 x 15-min bins

    # Build validation features (using training data as rolling window context)
    all_for_val = train_matches + val_matches
    val_feature_rows = build_feature_rows(all_for_val)
    # Only keep rows for validation matches
    val_ids = {m["id"] for m in val_matches}
    val_rows_by_match: dict[str, dict[str, dict]] = {}
    for row in val_feature_rows:
        if row.match_id in val_ids:
            val_rows_by_match.setdefault(row.match_id, {})[row.team] = row.features

    # Naive baseline: league-average Poisson from training data
    train_avg_h = sum(m["_ft_h"] for m in train_matches) / len(train_matches)
    train_avg_a = sum(m["_ft_a"] for m in train_matches) / len(train_matches)
    baseline_ou = poisson_over_under(train_avg_h, train_avg_a)
    baseline_hw = poisson_match_winner_probs(train_avg_h, train_avg_a)

    # Collect per-match predictions across multiple markets
    model_ou_list, baseline_ou_list, outcome_ou_list = [], [], []
    model_hw_list, baseline_hw_list, outcome_hw_list = [], [], []
    model_btts_list, baseline_btts_list, outcome_btts_list = [], [], []

    baseline_btts = poisson_btts(train_avg_h, train_avg_a)

    for m in val_matches:
        mid = m["id"]
        match_feats = val_rows_by_match.get(mid, {})

        if ml_artifacts and match_feats:
            home_feats = match_feats.get("home", {})
            away_feats = match_feats.get("away", {})
            mu_h_base = predict_expected_goals(ml_artifacts, home_feats) if home_feats else 1.3
            mu_a_base = predict_expected_goals(ml_artifacts, away_feats) if away_feats else 1.1
            a_h = convert_to_initial_a(mu_h_base)
            a_a = convert_to_initial_a(mu_a_base)
        else:
            a_h = result.a_H.mean()
            a_a = result.a_A.mean()

        # Apply MMPP time bins: μ = Σ exp(a + b[i]) * dt[i]
        mu_h = sum(math.exp(a_h + b[i]) * bin_durations[i] for i in range(6))
        mu_a = sum(math.exp(a_a + b[i]) * bin_durations[i] for i in range(6))

        ft_h, ft_a = m["_ft_h"], m["_ft_a"]

        # Over/Under 2.5
        model_ou_list.append(poisson_over_under(mu_h, mu_a))
        baseline_ou_list.append(baseline_ou)
        outcome_ou_list.append(1.0 if ft_h + ft_a > 2.5 else 0.0)

        # Home win probability
        model_1x2 = poisson_match_winner_probs(mu_h, mu_a)
        model_hw_list.append(model_1x2["home"])
        baseline_hw_list.append(baseline_hw["home"])
        outcome_hw_list.append(1.0 if ft_h > ft_a else 0.0)

        # BTTS
        model_btts_list.append(poisson_btts(mu_h, mu_a))
        baseline_btts_list.append(baseline_btts)
        outcome_btts_list.append(1.0 if ft_h > 0 and ft_a > 0 else 0.0)

    # Compute Brier scores across markets
    multi_market_bs: dict[str, float] = {}
    for name, model_arr, base_arr, out_arr in [
        ("over_2.5", model_ou_list, baseline_ou_list, outcome_ou_list),
        ("home_win", model_hw_list, baseline_hw_list, outcome_hw_list),
        ("btts", model_btts_list, baseline_btts_list, outcome_btts_list),
    ]:
        m_bs = brier_score(np.array(model_arr), np.array(out_arr))
        b_bs = brier_score(np.array(base_arr), np.array(out_arr))
        multi_market_bs[f"{name}_model_bs"] = m_bs
        multi_market_bs[f"{name}_baseline_bs"] = b_bs
        multi_market_bs[f"{name}_delta_bs"] = m_bs - b_bs

    # Primary metrics: home win (model shows strongest edge here)
    model_probs = np.array(model_hw_list)
    baseline_probs = np.array(baseline_hw_list)
    outcomes = np.array(outcome_hw_list)

    bs_model = brier_score(model_probs, outcomes)
    bs_baseline = brier_score(baseline_probs, outcomes)
    cal_dev = calibration_max_deviation(model_probs, outcomes)
    ll = log_loss(model_probs, outcomes)
    sign_val = validate_gamma_signs(result)

    # P&L simulation (informational — uses baseline as market proxy)
    sim = simulate_pnl(model_probs, baseline_probs, outcomes)

    fold_result = FoldResult(
        fold_idx=fold_idx,
        train_seasons=[],
        val_seasons=[],
        brier_score_model=bs_model,
        brier_score_pinnacle=bs_baseline,
        delta_bs=bs_model - bs_baseline,
        log_loss_val=ll,
        calibration_max_dev=cal_dev,
        sign_validation=sign_val,
        sim_pnl=sim,
        multi_market_bs=multi_market_bs,
    )

    log.info("fold_done", fold=fold_idx,
             bs_model=round(bs_model, 4),
             bs_baseline=round(bs_baseline, 4),
             delta_bs=round(fold_result.delta_bs, 4),
             cal_dev=round(cal_dev, 4),
             log_loss=round(ll, 4),
             ou_delta=round(multi_market_bs["over_2.5_delta_bs"], 4),
             hw_delta=round(multi_market_bs["home_win_delta_bs"], 4),
             btts_delta=round(multi_market_bs["btts_delta_bs"], 4))

    return fold_result, result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_calibration(config: SystemConfig,
                          output_dir: str = "output/calibration") -> GoNoGoReport:
    """Run the full Phase 1 calibration pipeline."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    db = DBClient(dsn=config.postgres_url)
    await db.connect()

    try:
        # Load data
        all_matches = await load_matches_from_db(db)
        if len(all_matches) < 100:
            log.error("insufficient_data", count=len(all_matches),
                      msg="Need at least 100 matches for calibration")
            raise ValueError(f"Only {len(all_matches)} matches — need 100+")

        by_season = split_by_season(all_matches)
        log.info("seasons_found", seasons=list(by_season.keys()),
                 counts={s: len(m) for s, m in by_season.items()})

        # Full training on all data
        log.info("full_training_start")
        all_intervals = run_step_1_1(all_matches)
        Q = run_step_1_2(all_intervals)
        a_init_map, ml_artifacts = run_step_1_3_ml(all_matches)
        full_result = run_step_1_4(all_intervals, a_init_map, n_starts=5)
        full_params = get_full_params(full_result)

        # Save Q matrix
        np.save(str(output_path / "Q_matrix.npy"), Q)

        # Save ML prior artifacts
        if ml_artifacts:
            from src.calibration.step_1_3_ml_prior import save_artifacts
            save_artifacts(ml_artifacts, str(output_path / "ml_prior"))

        # Walk-forward cross-validation
        log.info("walk_forward_cv_start")
        available_seasons = sorted(by_season.keys())
        folds: list[FoldResult] = []

        if len(available_seasons) >= 4:
            # Build folds from available seasons
            for i in range(len(available_seasons) - 3):
                train_seasons = available_seasons[i:i+3]
                val_season = available_seasons[i+3]

                train_matches = []
                for s in train_seasons:
                    train_matches.extend(by_season[s])
                val_matches = by_season.get(val_season, [])

                if len(val_matches) < 20:
                    log.warning("fold_skipped", val_season=val_season,
                                reason="too few validation matches")
                    continue

                fold_result, _ = run_step_1_5_fold(
                    train_matches, val_matches, fold_idx=len(folds))
                fold_result.train_seasons = train_seasons
                fold_result.val_seasons = [val_season]
                folds.append(fold_result)
                gc.collect()
        else:
            # Not enough seasons for walk-forward — do single train/test split
            log.warning("few_seasons", n=len(available_seasons),
                        msg="Using 80/20 split instead of walk-forward CV")
            split_idx = int(len(all_matches) * 0.8)
            train = all_matches[:split_idx]
            val = all_matches[split_idx:]
            fold_result, _ = run_step_1_5_fold(train, val, fold_idx=0)
            fold_result.train_seasons = available_seasons[:len(available_seasons)-1]
            fold_result.val_seasons = available_seasons[-1:]
            folds.append(fold_result)

        # Go/No-Go evaluation
        report = evaluate_go_no_go(folds, full_result)

        # Save results
        save_production_params(
            full_result, Q,
            validation_report=report,
            output_base=str(output_path),
        )

        # Save report
        report_dict = {
            "timestamp": datetime.now().isoformat(),
            "total_matches": len(all_matches),
            "seasons": list(by_season.keys()),
            "overall_pass": report.overall_pass,
            "calibration_pass": report.calibration_pass,
            "delta_bs_pass": report.delta_bs_pass,
            "gamma_signs_pass": report.gamma_signs_pass,
            "max_drawdown_pass": report.max_drawdown_pass,
            "all_folds_positive": report.all_folds_positive,
            "num_folds": len(folds),
            "params": {k: v.tolist() if isinstance(v, np.ndarray) else v
                       for k, v in full_params.items()},
            "folds": [
                {
                    "train": f.train_seasons,
                    "val": f.val_seasons,
                    "bs_model": f.brier_score_model,
                    "delta_bs": f.delta_bs,
                    "cal_max_dev": f.calibration_max_dev,
                    "pnl": f.sim_pnl,
                }
                for f in folds
            ],
        }
        with open(output_path / "calibration_report.json", "w") as fp:
            json.dump(report_dict, fp, indent=2, default=str)

        # Summary
        status = "GO" if report.overall_pass else "NO-GO"
        log.info("calibration_complete",
                 status=status,
                 total_matches=len(all_matches),
                 n_folds=len(folds),
                 overall_pass=report.overall_pass)

        return report

    finally:
        await db.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1 Calibration Pipeline")
    parser.add_argument("--config", default="config/system.yaml",
                        help="Path to config file")
    parser.add_argument("--output", default="output/calibration",
                        help="Output directory for params and reports")
    args = parser.parse_args()

    setup_logging(level="INFO")

    config = SystemConfig.load(args.config)
    report = await run_calibration(config, output_dir=args.output)

    if report.overall_pass:
        print("\n=== CALIBRATION: GO ===")
        print("Production parameters saved to output/calibration/")
        print("Ready for Phase 2 paper trading.")
    else:
        print("\n=== CALIBRATION: NO-GO ===")
        print("Review calibration_report.json for details.")
        print("Consider: more data, parameter tuning, or feature improvements.")


if __name__ == "__main__":
    asyncio.run(_main())
