"""Portfolio REST endpoints — Layer 2 data.

Provides portfolio-level aggregations: positions, risk limit usage,
P&L timeline, and summary metrics.

Endpoints:
  /api/positions              — all positions (filterable by status)
  /api/portfolio/summary      — bankroll, exposure, P&L, risk limits
  /api/portfolio/pnl_timeline — timestamped P&L series

Reference: docs/dashboard_implementation_roadmap.md → D3.4–D3.7
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from src.common.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@router.get("/positions")
async def get_positions(request: Request, status: str = "all"):
    """Return all positions across active engines.

    Args:
        status: Filter by "open", "settled", or "all".
    """
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return []

    positions = []
    for match_id, job in scheduler.jobs.items():
        if job.engine is None:
            continue

        for market, pos in job.engine.positions.items():
            entry = {
                "match_id": match_id,
                "home": job.home_team,
                "away": job.away_team,
                "market": market,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "entry_time": pos.entry_time,
                "status": "open",
            }
            positions.append(entry)

    if status == "open":
        positions = [p for p in positions if p["status"] == "open"]
    elif status == "settled":
        positions = [p for p in positions if p["status"] == "settled"]

    return positions


# ---------------------------------------------------------------------------
# Portfolio Summary
# ---------------------------------------------------------------------------

@router.get("/summary")
async def get_portfolio_summary(request: Request):
    """Return portfolio-level summary metrics.

    Includes bankroll, total exposure, unrealized P&L, risk limit usage.
    """
    scheduler = request.app.state.scheduler
    config = request.app.state.config

    if scheduler is None:
        return {
            "trading_mode": config.trading_mode if config else "paper",
            "bankroll": config.initial_bankroll if config else 0,
            "active_matches": 0,
            "open_positions": 0,
            "total_exposure": 0,
            "total_exposure_pct": 0,
            "unrealized_pnl": 0,
            "realized_pnl": 0,
            "risk_limits": _empty_risk_limits(config),
        }

    active = scheduler.get_active_engines()
    bankroll = config.initial_bankroll if config else 0

    # Aggregate across all engines
    open_positions = 0
    total_exposure = 0.0
    unrealized_pnl = 0.0
    realized_pnl = 0.0

    for match_id, engine in active.items():
        if engine is None:
            continue
        open_positions += len(engine.positions)
        total_exposure += scheduler.risk_manager.get_match_exposure(match_id)

        # Sum realized P&L from trade log
        for trade in engine.trade_log:
            if hasattr(trade, "pnl") and trade.pnl is not None:
                realized_pnl += trade.pnl

    total_exposure_pct = (total_exposure / bankroll * 100) if bankroll > 0 else 0

    return {
        "trading_mode": config.trading_mode if config else "paper",
        "bankroll": bankroll,
        "active_matches": len(active),
        "open_positions": open_positions,
        "total_exposure": round(total_exposure, 2),
        "total_exposure_pct": round(total_exposure_pct, 1),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "risk_limits": {
            "l1_order_cap": config.f_order_cap if config else 0.03,
            "l2_match_cap": config.f_match_cap if config else 0.05,
            "l3_total_cap": config.f_total_cap if config else 0.20,
            "l3_used_pct": round(total_exposure_pct, 1),
        },
    }


# ---------------------------------------------------------------------------
# P&L Timeline
# ---------------------------------------------------------------------------

@router.get("/pnl_timeline")
async def get_pnl_timeline(request: Request):
    """Return timestamped P&L series for the daily P&L chart.

    In Phase 0 (paper), aggregates from in-memory trade logs.
    In production, queries PostgreSQL.
    """
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return []

    timeline = []
    cumulative = 0.0

    # Collect all trades across engines, sorted by time
    all_trades = []
    for job in scheduler.jobs.values():
        if job.engine is None:
            continue
        for trade in job.engine.trade_log:
            all_trades.append(trade)

    all_trades.sort(key=lambda t: t.timestamp if hasattr(t, "timestamp") else 0)

    for trade in all_trades:
        pnl = getattr(trade, "pnl", 0) or 0
        cumulative += pnl
        timeline.append({
            "timestamp": trade.timestamp if hasattr(trade, "timestamp") else 0,
            "match_id": trade.match_id if hasattr(trade, "match_id") else "",
            "pnl": round(pnl, 2),
            "cumulative": round(cumulative, 2),
        })

    return timeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_risk_limits(config) -> dict:
    return {
        "l1_order_cap": config.f_order_cap if config else 0.03,
        "l2_match_cap": config.f_match_cap if config else 0.05,
        "l3_total_cap": config.f_total_cap if config else 0.20,
        "l3_used_pct": 0,
    }
