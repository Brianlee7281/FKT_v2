"""Tests for Step 5.4 Sprint D1.1: Dashboard Backend.

Verifies FastAPI server, REST endpoints, and WebSocket connections
for live match data, portfolio, and analytics.

Reference: docs/dashboard_implementation_roadmap.md -> D1.1
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.common.config import SystemConfig
from src.dashboard.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25"],
        target_leagues=["1204", "1399"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,
        goalserve_api_key="test_key",
        slack_webhook="",
        telegram_bot_token="",
        telegram_chat_id="",
    )


def _make_engine(positions=None, trade_log=None, bankroll=10000.0):
    """Create a mock engine with given positions and trades."""
    engine = MagicMock()
    engine.positions = positions or {}
    engine.trade_log = trade_log or []
    engine.bankroll = bankroll
    engine.lifecycle = SimpleNamespace(value="LIVE")
    engine.state = SimpleNamespace(
        engine_phase="running",
        event_state="CONFIRMED",
        t=45,
        score_h=1,
        score_a=0,
        X=2,
        cooldown=False,
        ob_freeze=False,
    )
    return engine


def _make_trade(pnl=0.0, timestamp=1000, match_id="match_1"):
    return SimpleNamespace(pnl=pnl, timestamp=timestamp, match_id=match_id)


def _make_position(direction="BUY_YES", entry_price=0.55, quantity=10, entry_time=1000):
    return SimpleNamespace(
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        entry_time=entry_time,
    )


def _make_job(match_id="match_1", home="Arsenal", away="Chelsea",
              engine=None, status="LIVE", league_id="1204"):
    from datetime import datetime, timezone
    job = SimpleNamespace(
        match_id=match_id,
        league_id=league_id,
        home_team=home,
        away_team=away,
        kickoff_time=datetime(2025, 1, 1, 15, 0, tzinfo=timezone.utc),
        status=status,
        engine=engine,
    )
    return job


def _make_scheduler(jobs=None, active_engines=None):
    """Create a mock scheduler."""
    scheduler = MagicMock()
    scheduler.jobs = jobs or {}

    if active_engines is None:
        active_engines = {}
        for mid, job in scheduler.jobs.items():
            if job.engine is not None:
                active_engines[mid] = job.engine

    scheduler.get_active_engines.return_value = active_engines
    scheduler.get_job_summary.return_value = [
        {"match_id": j.match_id, "home": j.home_team, "away": j.away_team, "status": j.status}
        for j in scheduler.jobs.values()
    ]

    # Risk manager mock
    risk_manager = MagicMock()
    risk_manager.get_match_exposure.return_value = 100.0
    scheduler.risk_manager = risk_manager

    # Alerter mock
    alerter = MagicMock()
    alerter.get_recent_alerts.return_value = [
        {"severity": "info", "title": "Test Alert", "body": "test", "timestamp": "2025-01-01T00:00:00Z"}
    ]
    scheduler.alerter = alerter

    return scheduler


def _inject_state(app, config, redis_client=None, scheduler=None):
    """Manually set app.state for testing (lifespan doesn't run with ASGITransport)."""
    app.state.config = config
    app.state.redis = redis_client
    app.state.scheduler = scheduler
    return app


@pytest.fixture
def app_no_scheduler(config):
    """App with config but no scheduler (empty state)."""
    app = create_app(config=config, redis_client=None, scheduler=None)
    return _inject_state(app, config)


@pytest.fixture
def app_with_scheduler(config):
    """App with a scheduler containing one active match."""
    engine = _make_engine(
        positions={"over_25": _make_position()},
        trade_log=[
            _make_trade(pnl=5.0, timestamp=1000),
            _make_trade(pnl=-2.0, timestamp=2000),
        ],
    )
    job = _make_job(engine=engine)
    scheduler = _make_scheduler(jobs={"match_1": job})
    app = create_app(config=config, redis_client=None, scheduler=scheduler)
    return _inject_state(app, config, scheduler=scheduler)


# ---------------------------------------------------------------------------
# REST: Active Matches
# ---------------------------------------------------------------------------

class TestActiveMatches:
    @pytest.mark.asyncio
    async def test_no_scheduler_returns_empty(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/matches/active")
            assert resp.status_code == 200
            assert resp.json() == []

    @pytest.mark.asyncio
    async def test_with_scheduler_returns_jobs(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/matches/active")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["match_id"] == "match_1"


class TestMatchDetail:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/matches/match_1")
            assert resp.status_code == 200
            assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_match_found(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/matches/match_1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["match_id"] == "match_1"
            assert data["home"] == "Arsenal"
            assert data["away"] == "Chelsea"
            assert data["trade_count"] == 2

    @pytest.mark.asyncio
    async def test_match_not_found(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/matches/nonexistent")
            assert resp.status_code == 200
            assert resp.json()["error"] == "match not found"


# ---------------------------------------------------------------------------
# REST: Portfolio
# ---------------------------------------------------------------------------

class TestPositions:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/positions")
            assert resp.status_code == 200
            assert resp.json() == []

    @pytest.mark.asyncio
    async def test_returns_open_positions(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/positions")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["market"] == "over_25"
            assert data[0]["direction"] == "BUY_YES"
            assert data[0]["status"] == "open"

    @pytest.mark.asyncio
    async def test_filter_settled_returns_empty(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/positions?status=settled")
            assert resp.status_code == 200
            assert resp.json() == []


class TestPortfolioSummary:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/summary")
            assert resp.status_code == 200
            data = resp.json()
            assert data["trading_mode"] == "paper"
            assert data["bankroll"] == 10000.0
            assert data["active_matches"] == 0
            assert data["open_positions"] == 0

    @pytest.mark.asyncio
    async def test_with_scheduler(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/summary")
            assert resp.status_code == 200
            data = resp.json()
            assert data["active_matches"] == 1
            assert data["open_positions"] == 1
            assert data["realized_pnl"] == 3.0  # 5.0 + (-2.0)
            assert data["total_exposure"] == 100.0
            assert "risk_limits" in data


class TestPnlTimeline:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/pnl_timeline")
            assert resp.status_code == 200
            assert resp.json() == []

    @pytest.mark.asyncio
    async def test_with_trades(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/portfolio/pnl_timeline")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 2
            assert data[0]["pnl"] == 5.0
            assert data[0]["cumulative"] == 5.0
            assert data[1]["pnl"] == -2.0
            assert data[1]["cumulative"] == 3.0


# ---------------------------------------------------------------------------
# REST: Analytics
# ---------------------------------------------------------------------------

class TestHealthDashboard:
    @pytest.mark.asyncio
    async def test_returns_7_metrics(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/health")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["metrics"]) == 7
            assert data["overall_status"] == "pending"

    @pytest.mark.asyncio
    async def test_metric_structure(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/health")
            metric = resp.json()["metrics"][0]
            assert "name" in metric
            assert "value" in metric
            assert "status" in metric
            assert "threshold" in metric


class TestCalibration:
    @pytest.mark.asyncio
    async def test_returns_empty_bins(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/calibration")
            assert resp.status_code == 200
            data = resp.json()
            assert data["bins"] == []
            assert data["market"] == "all"

    @pytest.mark.asyncio
    async def test_market_filter(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/calibration?market=over_25")
            data = resp.json()
            assert data["market"] == "over_25"


class TestCumulativePnl:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/pnl_cumulative")
            assert resp.status_code == 200
            data = resp.json()
            assert data["series"] == []
            assert data["max_drawdown_pct"] == 0

    @pytest.mark.asyncio
    async def test_with_trades(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/pnl_cumulative")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["series"]) == 2
            # First trade: +5, peak=5
            assert data["series"][0]["cumulative_pnl"] == 5.0
            # Second trade: +5-2=3, drawdown=5-3=2, dd_pct=40%
            assert data["series"][1]["cumulative_pnl"] == 3.0
            assert data["max_drawdown_pct"] == 40.0


class TestDirectional:
    @pytest.mark.asyncio
    async def test_stub_response(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/directional")
            assert resp.status_code == 200
            data = resp.json()
            assert "buy_yes" in data
            assert "buy_no" in data


class TestAlignmentEffect:
    @pytest.mark.asyncio
    async def test_stub_response(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/alignment_effect")
            assert resp.status_code == 200
            data = resp.json()
            assert "aligned" in data
            assert "divergent" in data


class TestPreliminary:
    @pytest.mark.asyncio
    async def test_stub_response(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/preliminary")
            assert resp.status_code == 200
            data = resp.json()
            assert data["rapid_entry_ready"] is False
            assert "rapid_entry_checks" in data


class TestParamsEndpoints:
    @pytest.mark.asyncio
    async def test_current_params(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/params/current")
            assert resp.status_code == 200
            data = resp.json()
            assert data["K_frac"] == 0.25
            assert data["z"] == 1.645
            assert data["trading_mode"] == "paper"

    @pytest.mark.asyncio
    async def test_param_history(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/params/history")
            assert resp.status_code == 200
            data = resp.json()
            assert data["history"] == []
            assert "current" in data


class TestRecentAlerts:
    @pytest.mark.asyncio
    async def test_no_scheduler(self, app_no_scheduler):
        transport = ASGITransport(app=app_no_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/alerts/recent")
            assert resp.status_code == 200
            assert resp.json() == []

    @pytest.mark.asyncio
    async def test_with_alerter(self, app_with_scheduler):
        transport = ASGITransport(app=app_with_scheduler)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/analytics/alerts/recent")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["title"] == "Test Alert"


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------

class TestAppFactory:
    def test_creates_app(self, config):
        app = create_app(config=config)
        assert app.title == "Kalshi Soccer Quant Dashboard"

    def test_cors_configured(self, config):
        app = create_app(config=config)
        # CORSMiddleware is added as middleware
        middleware_classes = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_classes

    def test_routers_included(self, config):
        app = create_app(config=config)
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/matches/active" in routes
        assert "/api/portfolio/summary" in routes
        assert "/api/analytics/health" in routes
