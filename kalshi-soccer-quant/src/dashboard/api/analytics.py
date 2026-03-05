"""Analytics REST endpoints — Layer 3 data.

Provides post-match analytics: health dashboard, calibration,
cumulative P&L, directional analysis, and parameter history.

In Phase 0 (paper), most of these return stub/empty data.
They will query PostgreSQL when the DB layer is added.

Endpoints:
  /api/analytics/health          — 7-metric health dashboard
  /api/analytics/calibration     — reliability diagram data
  /api/analytics/pnl_cumulative  — cumulative P&L + drawdown
  /api/analytics/directional     — Buy Yes vs Buy No breakdown
  /api/analytics/alignment_effect — bet365 cross-validation
  /api/analytics/preliminary     — PRELIMINARY accuracy stats
  /api/analytics/params/history  — parameter evolution
  /api/analytics/params/current  — current parameter values

Reference: docs/dashboard_implementation_roadmap.md → D4.8
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from src.common.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# Health Dashboard (3A)
# ---------------------------------------------------------------------------

@router.get("/health")
async def get_health(request: Request):
    """Layer 3: 7-metric system health dashboard.

    Returns gauge data with traffic-light status indicators.
    In Phase 0, returns placeholder values.
    """
    # Phase 0 stub — will query daily_analytics table in production
    return {
        "metrics": [
            _metric("Brier Score", None, "pending", "< Phase1.5 ± 0.02"),
            _metric("ΔBS vs Pinnacle", None, "pending", "< 0"),
            _metric("Edge Realization", None, "pending", "0.7–1.3"),
            _metric("Max Drawdown", None, "pending", "< 10%"),
            _metric("Alignment Value", None, "pending", "ALIGNED > DIVERGENT + 1¢"),
            _metric("Preliminary Accuracy", None, "pending", "> 0.95"),
            _metric("No-dir Edge Real.", None, "pending", "0.7–1.3"),
        ],
        "overall_status": "pending",
        "total_trades": 0,
        "note": "Requires minimum trade history for meaningful metrics.",
    }


# ---------------------------------------------------------------------------
# Calibration Plot (3B)
# ---------------------------------------------------------------------------

@router.get("/calibration")
async def get_calibration(request: Request, market: str = "all"):
    """Layer 3: calibration/reliability diagram data.

    Returns bins of (predicted_prob_range, actual_frequency, n_obs).
    Requires settled positions to compute.
    """
    # Phase 0 stub
    return {
        "market": market,
        "bins": [],
        "note": "Requires settled positions for calibration data.",
    }


# ---------------------------------------------------------------------------
# Cumulative P&L + Drawdown (3C)
# ---------------------------------------------------------------------------

@router.get("/pnl_cumulative")
async def get_cumulative_pnl(request: Request):
    """Layer 3: cumulative realized P&L with drawdown regions.

    Aggregated by day or week for the chart.
    """
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return {"series": [], "max_drawdown_pct": 0}

    # Build cumulative series from all trade logs
    all_trades = []
    for job in scheduler.jobs.values():
        if job.engine is None:
            continue
        for trade in job.engine.trade_log:
            all_trades.append(trade)

    all_trades.sort(key=lambda t: t.timestamp if hasattr(t, "timestamp") else 0)

    series = []
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for trade in all_trades:
        pnl = getattr(trade, "pnl", 0) or 0
        cumulative += pnl
        peak = max(peak, cumulative)
        dd = peak - cumulative
        if peak > 0:
            dd_pct = dd / peak * 100
            max_dd = max(max_dd, dd_pct)

        series.append({
            "timestamp": trade.timestamp if hasattr(trade, "timestamp") else 0,
            "cumulative_pnl": round(cumulative, 2),
            "drawdown": round(dd, 2),
        })

    return {
        "series": series,
        "max_drawdown_pct": round(max_dd, 1),
    }


# ---------------------------------------------------------------------------
# Directional Analysis (3D)
# ---------------------------------------------------------------------------

@router.get("/directional")
async def get_directional(request: Request):
    """Layer 3: Buy Yes vs Buy No performance breakdown."""
    # Phase 0 stub
    return {
        "buy_yes": {
            "trades": 0, "win_rate": 0, "edge_realization": 0,
            "avg_ev_entry": 0, "avg_actual_return": 0,
        },
        "buy_no": {
            "trades": 0, "win_rate": 0, "edge_realization": 0,
            "avg_ev_entry": 0, "avg_actual_return": 0,
        },
    }


# ---------------------------------------------------------------------------
# Alignment Effect (3E)
# ---------------------------------------------------------------------------

@router.get("/alignment_effect")
async def get_alignment_effect(request: Request):
    """Layer 3: bet365 cross-validation effect analysis."""
    # Phase 0 stub
    return {
        "aligned": {"avg_return": 0, "n_trades": 0, "win_rate": 0},
        "divergent": {"avg_return": 0, "n_trades": 0, "win_rate": 0},
        "alignment_value": 0,
    }


# ---------------------------------------------------------------------------
# Preliminary Accuracy (3F)
# ---------------------------------------------------------------------------

@router.get("/preliminary")
async def get_preliminary_stats(request: Request):
    """Layer 3: PRELIMINARY detection accuracy + Rapid Entry readiness."""
    # Phase 0 stub
    return {
        "total_events": 0,
        "confirmed": 0,
        "var_cancelled": 0,
        "false_alarm": 0,
        "accuracy": 0,
        "rapid_entry_ready": False,
        "rapid_entry_checks": {
            "accuracy_gt_095": False,
            "var_rate_lt_003": False,
            "hypothetical_pnl_gt_0": False,
            "trades_gte_200": False,
        },
    }


# ---------------------------------------------------------------------------
# Parameter History (3G)
# ---------------------------------------------------------------------------

@router.get("/params/history")
async def get_param_history(request: Request):
    """Layer 3: parameter evolution over time."""
    # Phase 0 stub — returns current values as initial point
    config = request.app.state.config
    return {
        "history": [],
        "current": _current_params(config),
    }


@router.get("/params/current")
async def get_current_params(request: Request):
    """Return current trading parameters."""
    config = request.app.state.config
    return _current_params(config)


# ---------------------------------------------------------------------------
# Alerts history (for notification panel)
# ---------------------------------------------------------------------------

@router.get("/alerts/recent")
async def get_recent_alerts(request: Request, limit: int = 20):
    """Return recent alert history for the notification panel."""
    scheduler = request.app.state.scheduler
    if scheduler is not None and hasattr(scheduler, "alerter"):
        return scheduler.alerter.get_recent_alerts(limit=limit)
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metric(name: str, value, status: str, threshold: str) -> dict:
    return {
        "name": name,
        "value": value,
        "status": status,  # "healthy", "warning", "risk", "pending"
        "threshold": threshold,
    }


def _current_params(config) -> dict:
    if config is None:
        return {}
    return {
        "K_frac": config.K_frac,
        "z": config.z,
        "theta_entry": config.theta_entry,
        "theta_exit": config.theta_exit,
        "cooldown_seconds": config.cooldown_seconds,
        "low_confidence_multiplier": config.low_confidence_multiplier,
        "rapid_entry_enabled": config.rapid_entry_enabled,
        "bet365_divergence_auto_exit": config.bet365_divergence_auto_exit,
        "f_order_cap": config.f_order_cap,
        "f_match_cap": config.f_match_cap,
        "f_total_cap": config.f_total_cap,
        "trading_mode": config.trading_mode,
    }
