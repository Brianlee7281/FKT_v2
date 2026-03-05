"""Async PostgreSQL wrapper using asyncpg."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from src.common.logging import get_logger

log = get_logger(__name__)


class DBClient:
    """Async PostgreSQL client for the Kalshi trading system."""

    def __init__(self, dsn: str = "postgresql://kalshi:kalshi_dev@localhost:5432/kalshi"):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
        )
        log.info("db_connected", dsn=self._dsn.split("@")[-1])

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            log.info("db_closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("DBClient not connected. Call connect() first.")
        return self._pool

    # ── Generic query helpers ──

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    # ── Match-specific operations ──

    async def upsert_match_result(self, match: dict) -> None:
        """Insert or update a historical match result."""
        await self.execute(
            """
            INSERT INTO historical_matches
                (match_id, league_id, date, home_team, away_team,
                 ft_score_h, ft_score_a, ht_score_h, ht_score_a,
                 added_time_1, added_time_2, status, summary, lineups)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (match_id) DO UPDATE SET
                ft_score_h = EXCLUDED.ft_score_h,
                ft_score_a = EXCLUDED.ft_score_a,
                ht_score_h = EXCLUDED.ht_score_h,
                ht_score_a = EXCLUDED.ht_score_a,
                added_time_1 = EXCLUDED.added_time_1,
                added_time_2 = EXCLUDED.added_time_2,
                status = EXCLUDED.status,
                summary = EXCLUDED.summary,
                lineups = EXCLUDED.lineups,
                collected_at = NOW()
            """,
            match.get("id", ""),
            match.get("league_id", ""),
            match.get("date", ""),
            match.get("localteam", {}).get("name", ""),
            match.get("visitorteam", {}).get("name", ""),
            _safe_int(match.get("localteam", {}).get("ft_score")),
            _safe_int(match.get("visitorteam", {}).get("ft_score")),
            _safe_int(match.get("localteam", {}).get("ht_score")),
            _safe_int(match.get("visitorteam", {}).get("ht_score")),
            _safe_int(match.get("matchinfo", {}).get("time", {}).get("addedTime_period1")),
            _safe_int(match.get("matchinfo", {}).get("time", {}).get("addedTime_period2")),
            match.get("status", ""),
            json.dumps(match.get("summary", {})),
            json.dumps(match.get("teams", {})),
        )

    async def upsert_match_stats(self, match_id: str, stats: dict) -> None:
        """Update match with detailed team/player stats."""
        await self.execute(
            """
            UPDATE historical_matches
            SET stats = $2,
                player_stats = $3,
                collected_at = NOW()
            WHERE match_id = $1
            """,
            match_id,
            json.dumps(stats.get("stats", {})),
            json.dumps(stats.get("player_stats", {})),
        )

    async def upsert_match_odds(self, match_odds: dict) -> None:
        """Update match with pregame odds data."""
        match_id = match_odds.get("id", match_odds.get("static_id", ""))
        if not match_id:
            return
        await self.execute(
            """
            UPDATE historical_matches
            SET odds = $2,
                collected_at = NOW()
            WHERE match_id = $1
            """,
            match_id,
            json.dumps(match_odds.get("bookmakers", match_odds)),
        )

    async def get_match_count(self) -> int:
        result = await self.fetchval("SELECT COUNT(*) FROM historical_matches")
        return result or 0

    async def upsert_match_job(self, match_id: str, league_id: str,
                                home_team: str, away_team: str,
                                kickoff_time: str, status: str = "SCHEDULED") -> None:
        await self.execute(
            """
            INSERT INTO match_jobs (match_id, league_id, home_team, away_team, kickoff_time, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (match_id) DO UPDATE SET
                status = EXCLUDED.status,
                updated_at = NOW()
            """,
            match_id, league_id, home_team, away_team, kickoff_time, status,
        )

    # ── Health check ──

    async def ping(self) -> bool:
        try:
            result = await self.fetchval("SELECT 1")
            return result == 1
        except Exception:
            return False


def _safe_int(val: Any) -> int:
    """Convert a value to int, defaulting to 0."""
    if val is None or val == "":
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
