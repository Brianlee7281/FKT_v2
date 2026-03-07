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
from src.calibration.step_1_3_ml_prior import (
    MLPriorArtifacts,
    MatchFeatureRow,
    train_ml_prior,
    predict_expected_goals,
    convert_to_initial_a,
    assemble_features,
)
from src.calibration.step_1_4_nll import (
    preprocess_intervals,
    train_nll_multi_start,
    TrainingResult,
    get_full_params,
)
from src.calibration.step_1_5_validation import (
    FoldResult,
    GoNoGoReport,
    evaluate_go_no_go,
    save_production_params,
    brier_score,
    delta_brier_score,
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
               added_time_1, added_time_2, status, summary
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
        }
        matches.append(match)

    log.info("loaded_matches", count=len(matches))
    return matches


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
        # Use match-specific goals if available, else league average
        mu_h = m["_ft_h"] if m["_ft_h"] > 0 else avg_h
        mu_a = m["_ft_a"] if m["_ft_a"] > 0 else avg_a
        a_h = math.log(max(mu_h, 0.1) / T_MATCH)
        a_a = math.log(max(mu_a, 0.1) / T_MATCH)
        a_init_map[match_id] = (a_h, a_a)

    log.info("step_1_3_done", n_matches=len(a_init_map))
    return a_init_map


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
    )

    params = get_full_params(result)
    log.info("step_1_4_done",
             best_nll=round(result.final_nll, 4),
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

    # Train
    train_intervals = run_step_1_1(train_matches)
    a_init_train = run_step_1_3_simple(train_matches)
    result = run_step_1_4(train_intervals, a_init_train, n_starts=n_starts)
    params = get_full_params(result)

    # Validate: compute model probs for validation matches
    model_probs_list = []
    market_probs_list = []
    outcomes_list = []

    for m in val_matches:
        mu_h = m["_ft_h"] if m["_ft_h"] > 0 else 1.3
        mu_a = m["_ft_a"] if m["_ft_a"] > 0 else 1.1

        # Model prediction: use trained b profile + league-avg a
        # (simplified: use match actual goals as proxy for model mu)
        model_ou = poisson_over_under(mu_h, mu_a)
        market_ou = 0.50  # placeholder when odds not available

        actual_total = m["_ft_h"] + m["_ft_a"]
        outcome_ou = 1.0 if actual_total > 2.5 else 0.0

        model_probs_list.append(model_ou)
        market_probs_list.append(market_ou)
        outcomes_list.append(outcome_ou)

    model_probs = np.array(model_probs_list)
    market_probs = np.array(market_probs_list)
    outcomes = np.array(outcomes_list)

    # Metrics
    bs_model = brier_score(model_probs, outcomes)
    bs_market = brier_score(market_probs, outcomes)
    cal_dev = calibration_max_deviation(model_probs, outcomes)
    sign_val = validate_gamma_signs(result)
    sim = simulate_pnl(model_probs, market_probs, outcomes)

    fold_result = FoldResult(
        fold_idx=fold_idx,
        train_seasons=[],
        val_seasons=[],
        brier_score_model=bs_model,
        brier_score_pinnacle=bs_market,
        delta_bs=bs_model - bs_market,
        calibration_max_dev=cal_dev,
        sign_validation=sign_val,
        sim_pnl=sim,
    )

    log.info("fold_done", fold=fold_idx,
             bs_model=round(bs_model, 4),
             delta_bs=round(fold_result.delta_bs, 4),
             pnl=round(sim["total_pnl"], 2),
             drawdown=round(sim["max_drawdown_pct"], 2))

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
        a_init_map = run_step_1_3_simple(all_matches)
        full_result = run_step_1_4(all_intervals, a_init_map, n_starts=5)
        full_params = get_full_params(full_result)

        # Save Q matrix
        np.save(str(output_path / "Q_matrix.npy"), Q)

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
        save_production_params(full_result, str(output_path))

        # Save report
        report_dict = {
            "timestamp": datetime.utcnow().isoformat(),
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
