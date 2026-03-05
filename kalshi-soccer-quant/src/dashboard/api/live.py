"""WebSocket endpoints for real-time match and portfolio data streams.

Subscribes to Redis Pub/Sub channels published by MatchEngine and
forwards data to connected browser clients.

Endpoints:
  /ws/live/{match_id}   — single match state stream
  /ws/portfolio         — all matches aggregated stream
  /ws/events/{match_id} — event log stream for a match
  /api/matches/active   — REST: list active matches

Reference: docs/dashboard_implementation_roadmap.md → D1.1
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request

from src.common.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# WebSocket: Per-match state stream
# ---------------------------------------------------------------------------

@router.websocket("/ws/live/{match_id}")
async def ws_match_live(websocket: WebSocket, match_id: str):
    """Stream real-time state snapshots for a single match.

    Subscribes to Redis channel `match:{match_id}:state`.
    Falls back to polling scheduler if Redis is unavailable.
    """
    await websocket.accept()
    redis = websocket.app.state.redis

    if redis is not None:
        await _stream_redis_channel(
            websocket, redis, f"match:{match_id}:state",
        )
    else:
        # No Redis — poll scheduler for state (paper mode fallback)
        await _poll_scheduler_state(websocket, match_id)


@router.websocket("/ws/events/{match_id}")
async def ws_match_events(websocket: WebSocket, match_id: str):
    """Stream event log entries for a single match.

    Subscribes to Redis channel `match:{match_id}:events`.
    """
    await websocket.accept()
    redis = websocket.app.state.redis

    if redis is not None:
        await _stream_redis_channel(
            websocket, redis, f"match:{match_id}:events",
        )
    else:
        # No events without Redis — send keepalive
        await _keepalive(websocket)


# ---------------------------------------------------------------------------
# WebSocket: Portfolio stream (all matches)
# ---------------------------------------------------------------------------

@router.websocket("/ws/portfolio")
async def ws_portfolio(websocket: WebSocket):
    """Stream state snapshots from all active matches.

    Subscribes to Redis pattern `match:*:state`.
    """
    await websocket.accept()
    redis = websocket.app.state.redis

    if redis is not None:
        await _stream_redis_pattern(
            websocket, redis, "match:*:state",
        )
    else:
        # No Redis — poll all engines
        await _poll_all_engines(websocket)


# ---------------------------------------------------------------------------
# REST: Active matches
# ---------------------------------------------------------------------------

@router.get("/api/matches/active")
async def get_active_matches(request: Request):
    """Return list of currently active (LIVE) matches.

    Uses MatchScheduler if available, otherwise returns empty list.
    """
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return []

    return scheduler.get_job_summary()


@router.get("/api/matches/{match_id}")
async def get_match_detail(request: Request, match_id: str):
    """Return detailed info for a specific match."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return {"error": "scheduler not available"}

    job = scheduler.jobs.get(match_id)
    if job is None:
        return {"error": "match not found"}

    result = {
        "match_id": job.match_id,
        "league_id": job.league_id,
        "home": job.home_team,
        "away": job.away_team,
        "kickoff": job.kickoff_time.isoformat(),
        "status": job.status,
    }

    if job.engine is not None:
        result["lifecycle"] = job.engine.lifecycle.value
        result["trade_count"] = len(job.engine.trade_log)
        result["bankroll"] = job.engine.bankroll

    return result


# ---------------------------------------------------------------------------
# Redis streaming helpers
# ---------------------------------------------------------------------------

async def _stream_redis_channel(
    websocket: WebSocket, redis, channel: str,
) -> None:
    """Subscribe to a Redis channel and forward messages to WebSocket."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message is not None and message.get("type") == "message":
                await websocket.send_text(message["data"])

            # Check if client disconnected
            try:
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=0.01,
                )
            except asyncio.TimeoutError:
                pass  # No message from client, continue

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await pubsub.unsubscribe(channel)


async def _stream_redis_pattern(
    websocket: WebSocket, redis, pattern: str,
) -> None:
    """Subscribe to a Redis pattern and forward messages to WebSocket."""
    pubsub = redis.pubsub()
    await pubsub.psubscribe(pattern)

    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message is not None and message.get("type") == "pmessage":
                # Include the channel name so client knows which match
                payload = {
                    "channel": message.get("channel", ""),
                    "data": json.loads(message["data"]) if isinstance(message["data"], str) else message["data"],
                }
                await websocket.send_text(json.dumps(payload))

            try:
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=0.01,
                )
            except asyncio.TimeoutError:
                pass

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await pubsub.punsubscribe(pattern)


# ---------------------------------------------------------------------------
# Scheduler polling fallback (no Redis)
# ---------------------------------------------------------------------------

async def _poll_scheduler_state(
    websocket: WebSocket, match_id: str,
) -> None:
    """Poll scheduler for match state when Redis is unavailable."""
    scheduler = websocket.app.state.scheduler

    try:
        while True:
            if scheduler is not None:
                job = scheduler.jobs.get(match_id)
                if job is not None and job.engine is not None:
                    snapshot = _engine_to_snapshot(job)
                    await websocket.send_text(json.dumps(snapshot))

            await asyncio.sleep(1.0)

    except (WebSocketDisconnect, Exception):
        pass


async def _poll_all_engines(websocket: WebSocket) -> None:
    """Poll all active engines when Redis is unavailable."""
    scheduler = websocket.app.state.scheduler

    try:
        while True:
            if scheduler is not None:
                for match_id, job in scheduler.jobs.items():
                    if job.engine is not None and job.status == "LIVE":
                        snapshot = _engine_to_snapshot(job)
                        payload = {
                            "channel": f"match:{match_id}:state",
                            "data": snapshot,
                        }
                        await websocket.send_text(json.dumps(payload))

            await asyncio.sleep(1.0)

    except (WebSocketDisconnect, Exception):
        pass


async def _keepalive(websocket: WebSocket) -> None:
    """Send periodic keepalive pings."""
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"type": "keepalive"}))
    except (WebSocketDisconnect, Exception):
        pass


def _engine_to_snapshot(job) -> dict:
    """Extract a state snapshot dict from a MatchJob's engine."""
    engine = job.engine
    snapshot = {
        "match_id": job.match_id,
        "home": job.home_team,
        "away": job.away_team,
        "status": job.status,
        "lifecycle": engine.lifecycle.value if engine else None,
    }

    # Add engine state if available
    if engine is not None and hasattr(engine, "state"):
        state = engine.state
        snapshot.update({
            "engine_phase": state.engine_phase,
            "event_state": state.event_state,
            "t": state.t,
            "score_h": state.score_h,
            "score_a": state.score_a,
            "X": state.X,
            "cooldown": state.cooldown,
            "ob_freeze": state.ob_freeze,
        })

    if engine is not None:
        snapshot["trade_count"] = len(engine.trade_log)
        snapshot["bankroll"] = engine.bankroll

    return snapshot
