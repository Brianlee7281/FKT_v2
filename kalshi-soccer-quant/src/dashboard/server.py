"""Dashboard Server — FastAPI + WebSocket for real-time trading dashboard.

Entry point for the dashboard backend. Subscribes to Redis Pub/Sub channels
published by MatchEngine, streams data to browser via WebSocket.

Sprint D1.1 from dashboard_implementation_roadmap.md.

Usage:
    uvicorn src.dashboard.server:app --reload --port 8000

Reference: docs/blueprint.md → Process N+1: DASHBOARD_SERVER,
           docs/dashboard_implementation_roadmap.md → Sprint D1
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.common.config import SystemConfig
from src.common.logging import get_logger
from src.dashboard.api.live import router as live_router
from src.dashboard.api.portfolio import router as portfolio_router
from src.dashboard.api.analytics import router as analytics_router

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared state — set during lifespan, accessed by routers via app.state
# ---------------------------------------------------------------------------

_config: SystemConfig | None = None
_redis = None
_scheduler = None


def configure(
    config: SystemConfig | None = None,
    redis_client=None,
    scheduler=None,
) -> None:
    """Configure shared dependencies before app startup.

    Called either programmatically (tests) or during lifespan.
    """
    global _config, _redis, _scheduler
    _config = config
    _redis = redis_client
    _scheduler = scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan — load config, connect Redis."""
    global _config, _redis

    if _config is None:
        _config = SystemConfig.load()

    # Auto-connect to Redis if not already provided
    if _redis is None and _config.redis_url:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(_config.redis_url)
            await _redis.ping()
        except Exception as e:
            log.warning("redis_connect_failed", error=str(e))
            _redis = None

    app.state.config = _config
    app.state.redis = _redis
    app.state.scheduler = _scheduler

    log.info(
        "dashboard_starting",
        trading_mode=_config.trading_mode,
        redis_configured=_redis is not None,
    )

    yield

    if _redis is not None:
        await _redis.aclose()

    log.info("dashboard_stopped")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    config: SystemConfig | None = None,
    redis_client=None,
    scheduler=None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: System configuration (loads from file if None).
        redis_client: Redis client for pub/sub (optional for paper mode).
        scheduler: MatchScheduler instance for querying active engines.

    Returns:
        Configured FastAPI app.
    """
    configure(config=config, redis_client=redis_client, scheduler=scheduler)

    application = FastAPI(
        title="Kalshi Soccer Quant Dashboard",
        lifespan=lifespan,
    )

    # CORS — allow React dev server
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers
    application.include_router(live_router)
    application.include_router(portfolio_router)
    application.include_router(analytics_router)

    # Serve React build in production (if exists)
    frontend_build = Path(__file__).parent / "frontend" / "build"
    if frontend_build.exists():
        application.mount(
            "/",
            StaticFiles(directory=str(frontend_build), html=True),
            name="frontend",
        )

    return application


# Default app instance (for uvicorn src.dashboard.server:app)
app = create_app()
