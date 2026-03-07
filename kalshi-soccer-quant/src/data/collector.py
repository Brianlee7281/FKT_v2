"""Historical data collector — backfill and daily ingestion."""

from __future__ import annotations

import asyncio
import argparse
import json
from datetime import datetime, timedelta

from tqdm import tqdm

from src.common.config import SystemConfig
from src.common.db_client import DBClient
from src.common.logging import get_logger, setup_logging
from src.goalserve.client import GoalserveClient

log = get_logger(__name__)

# Rate-limit delay between API calls (seconds)
RATE_LIMIT_DELAY = 1.0


class DataCollector:
    """Collects Goalserve historical data into PostgreSQL.

    Two modes:
      1. Backfill: one-time historical ingestion (3-5 seasons).
      2. Daily: collect yesterday's results + stats + odds.
    """

    def __init__(self, config: SystemConfig):
        self.config = config
        self.goalserve = GoalserveClient(
            api_key=config.goalserve_api_key,
            base_url=config.goalserve_base_url,
        )
        self.db = DBClient(dsn=config.postgres_url)

    async def start(self) -> None:
        await self.db.connect()
        log.info("data_collector_started")

    async def stop(self) -> None:
        await self.goalserve.close()
        await self.db.close()
        log.info("data_collector_stopped")

    # ── Backfill ──

    async def backfill_historical(self, league_ids: list[str],
                                   seasons: list[str]) -> None:
        """One-time backfill of historical match data.

        For each league+season:
          1. Fetch fixtures/results via soccerhistory endpoint
          2. Upsert completed matches into DB

        Args:
            league_ids: Goalserve league IDs.
            seasons: Season identifiers (e.g., ["2022-2023", "2023-2024"]).
                     Single years like "2023" are auto-converted to "2023-2024".
        """
        total_matches = 0

        for league_id in league_ids:
            for season in seasons:
                # Convert "2023" → "2023-2024" if needed
                if "-" not in season:
                    season_str = f"{season}-{int(season)+1}"
                else:
                    season_str = season

                log.info("backfill_season", league_id=league_id,
                         season=season_str)

                try:
                    fixtures = await self.goalserve.get_historical(
                        league_id, season_str)
                except Exception as e:
                    log.error("backfill_fixtures_failed",
                              league_id=league_id, season=season_str,
                              error=str(e))
                    continue

                inserted = 0
                for match in fixtures:
                    status = match.get("status", "")
                    if status not in ("FT", "Full-time", "AET", "Pen."):
                        continue

                    match["league_id"] = league_id
                    await self.db.upsert_match_result(match)
                    total_matches += 1
                    inserted += 1

                log.info("backfill_season_done",
                         league_id=league_id, season=season_str,
                         inserted=inserted)

                await asyncio.sleep(RATE_LIMIT_DELAY)

        # Also fetch current season for each league
        for league_id in league_ids:
            log.info("backfill_current_season", league_id=league_id)
            try:
                fixtures = await self.goalserve.get_fixtures(league_id)
                inserted = 0
                for match in fixtures:
                    status = match.get("status", "")
                    if status not in ("FT", "Full-time", "AET", "Pen."):
                        continue
                    match["league_id"] = league_id
                    await self.db.upsert_match_result(match)
                    total_matches += 1
                    inserted += 1
                log.info("backfill_current_done",
                         league_id=league_id, inserted=inserted)
            except Exception as e:
                log.error("backfill_current_failed",
                          league_id=league_id, error=str(e))
            await asyncio.sleep(RATE_LIMIT_DELAY)

        count = await self.db.get_match_count()
        log.info("backfill_complete", total_inserted=total_matches,
                 db_total=count)

    # ── Daily collection ──

    async def collect_yesterday_results(self) -> None:
        """Collect completed matches from yesterday."""
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d.%m.%Y")
        log.info("collecting_yesterday", date=yesterday)

        for league_id in self.config.target_leagues:
            try:
                fixtures = await self.goalserve.get_fixtures(league_id, date=yesterday)
            except Exception as e:
                log.error("daily_fixtures_failed",
                          league_id=league_id, date=yesterday, error=str(e))
                continue

            for match in fixtures:
                status = match.get("status", "")
                if status not in ("FT", "Full-time", "AET", "Pen."):
                    continue

                match["league_id"] = league_id
                await self.db.upsert_match_result(match)

                match_id = match.get("id", match.get("static_id", ""))
                if match_id:
                    await self._fetch_and_store_stats(match_id, league_id)
                    await asyncio.sleep(RATE_LIMIT_DELAY)

            # Odds
            try:
                odds_data = await self.goalserve.get_odds(
                    league_id=league_id, date_start=yesterday)
                await self.db.upsert_odds_snapshot(league_id, odds_data)
            except Exception as e:
                log.error("daily_odds_failed",
                          league_id=league_id, date=yesterday, error=str(e))

        log.info("daily_collection_complete", date=yesterday)

    async def collect_odds_snapshot(self) -> None:
        """Periodic odds snapshot (every 6 hours)."""
        for league_id in self.config.target_leagues:
            try:
                odds_data = await self.goalserve.get_odds(league_id=league_id)
                await self.db.upsert_odds_snapshot(league_id, odds_data)
                await asyncio.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                log.error("odds_snapshot_failed",
                          league_id=league_id, error=str(e))

    async def verify_data_integrity(self) -> dict:
        """Verify historical data integrity (weekly check)."""
        total = await self.db.get_match_count()
        nulls = await self.db.fetchval(
            "SELECT COUNT(*) FROM historical_matches WHERE summary IS NULL"
        )
        no_stats = await self.db.fetchval(
            "SELECT COUNT(*) FROM historical_matches WHERE stats IS NULL"
        )
        report = {
            "total_matches": total,
            "missing_summary": nulls or 0,
            "missing_stats": no_stats or 0,
        }
        log.info("data_integrity_check", **report)
        return report

    # ── Stats backfill ──

    async def backfill_stats(self, batch_size: int = 100) -> dict:
        """Backfill match stats for all matches missing stats data.

        Queries historical_matches WHERE stats IS NULL, then fetches
        stats from Goalserve commentaries endpoint for each match.

        Args:
            batch_size: Number of matches to process per batch
                        (commits progress between batches).

        Returns:
            Summary dict with counts of success/failure/skipped.
        """
        total_missing = await self.db.fetchval(
            "SELECT COUNT(*) FROM historical_matches WHERE stats IS NULL"
        )
        log.info("backfill_stats_start", total_missing=total_missing)

        success = 0
        failed = 0
        skipped = 0
        offset = 0
        pbar = tqdm(total=total_missing, desc="Backfill stats", unit="match")

        while True:
            rows = await self.db.fetch(
                """
                SELECT match_id, league_id
                FROM historical_matches
                WHERE stats IS NULL
                ORDER BY date
                LIMIT $1
                """,
                batch_size,
            )

            if not rows:
                break

            for row in rows:
                match_id = row["match_id"]
                league_id = row["league_id"] or ""

                try:
                    stats = await self.goalserve.get_match_stats(
                        match_id, league_id
                    )
                    if stats:
                        await self.db.upsert_match_stats(match_id, stats)
                        success += 1
                    else:
                        # Mark as attempted so we don't retry indefinitely
                        await self.db.execute(
                            """
                            UPDATE historical_matches
                            SET stats = '{}'::jsonb, collected_at = NOW()
                            WHERE match_id = $1
                            """,
                            match_id,
                        )
                        skipped += 1
                except Exception as e:
                    log.warning("backfill_stats_failed",
                                match_id=match_id, error=str(e))
                    # Mark with empty so we don't retry on next batch
                    await self.db.execute(
                        """
                        UPDATE historical_matches
                        SET stats = '{}'::jsonb, collected_at = NOW()
                        WHERE match_id = $1
                        """,
                        match_id,
                    )
                    failed += 1

                pbar.update(1)
                await asyncio.sleep(RATE_LIMIT_DELAY)

            processed = success + failed + skipped
            log.info("backfill_stats_progress",
                     processed=processed, total=total_missing,
                     success=success, failed=failed, skipped=skipped)

        pbar.close()
        report = {
            "total_missing": total_missing,
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }
        log.info("backfill_stats_complete", **report)
        return report

    async def backfill_odds(self, batch_size: int = 50) -> dict:
        """Backfill pregame odds for all matches missing odds data.

        Uses the Goalserve odds endpoint with date filtering to fetch
        odds for matches grouped by league and date.

        Args:
            batch_size: Number of (league, date) groups per batch.

        Returns:
            Summary dict with counts.
        """
        # Get distinct league+date pairs missing odds
        rows = await self.db.fetch(
            """
            SELECT DISTINCT league_id, date
            FROM historical_matches
            WHERE odds IS NULL AND date IS NOT NULL
            ORDER BY date
            """
        )
        total_groups = len(rows)
        log.info("backfill_odds_start", total_groups=total_groups)

        success = 0
        failed = 0

        for row in tqdm(rows, desc="Backfill odds", unit="group"):
            league_id = row["league_id"]
            match_date = row["date"]

            if not league_id or not match_date:
                continue

            date_str = match_date.strftime("%d.%m.%Y")

            try:
                odds_data = await self.goalserve.get_odds(
                    league_id=league_id, date_start=date_str, date_end=date_str
                )
                if odds_data:
                    # Try to extract per-match odds and store individually
                    matches_odds = _extract_match_odds(odds_data)
                    if matches_odds:
                        for match_odds in matches_odds:
                            await self.db.upsert_match_odds(match_odds)
                        success += 1
                    else:
                        # Bulk update for the league+date
                        await self.db.execute(
                            """
                            UPDATE historical_matches
                            SET odds = $3::jsonb, collected_at = NOW()
                            WHERE league_id = $1 AND date = $2 AND odds IS NULL
                            """,
                            league_id,
                            match_date,
                            json.dumps(odds_data),
                        )
                        success += 1
                else:
                    # Mark as attempted
                    await self.db.execute(
                        """
                        UPDATE historical_matches
                        SET odds = '{}'::jsonb, collected_at = NOW()
                        WHERE league_id = $1 AND date = $2 AND odds IS NULL
                        """,
                        league_id,
                        match_date,
                    )
            except Exception as e:
                log.warning("backfill_odds_failed",
                            league_id=league_id, date=date_str, error=str(e))
                failed += 1

            await asyncio.sleep(RATE_LIMIT_DELAY)

        report = {
            "total_groups": total_groups,
            "success": success,
            "failed": failed,
        }
        log.info("backfill_odds_complete", **report)
        return report

    # ── Internal helpers ──

    async def _fetch_and_store_stats(self, match_id: str,
                                     league_id: str = "") -> None:
        """Fetch match stats via commentaries and store in DB."""
        try:
            stats = await self.goalserve.get_match_stats(match_id, league_id)
            if stats:
                await self.db.upsert_match_stats(match_id, stats)
        except Exception as e:
            log.warning("stats_fetch_failed",
                        match_id=match_id, error=str(e))


def _extract_match_odds(odds_data: dict) -> list[dict]:
    """Extract per-match odds from a Goalserve odds response.

    The odds endpoint returns matches nested under categories/tournaments.
    This flattens them into a list of per-match dicts suitable for
    upsert_match_odds.
    """
    matches = []
    # Navigate various Goalserve response shapes
    scores = odds_data.get("scores", odds_data.get("results", odds_data))
    if not isinstance(scores, dict):
        return matches

    categories = scores.get("category", scores.get("tournament", []))
    if isinstance(categories, dict):
        categories = [categories]

    for cat in categories:
        cat_matches = cat.get("match", [])
        if isinstance(cat_matches, dict):
            cat_matches = [cat_matches]
        for m in cat_matches:
            match_id = m.get("id", m.get("static_id", ""))
            if match_id:
                matches.append(m)

    return matches


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi Soccer Quant — Data Collector")
    parser.add_argument("--backfill", action="store_true",
                        help="Run historical backfill (results only)")
    parser.add_argument("--backfill-stats", action="store_true",
                        help="Backfill match stats for matches missing stats")
    parser.add_argument("--backfill-odds", action="store_true",
                        help="Backfill pregame odds for matches missing odds")
    parser.add_argument("--daily", action="store_true",
                        help="Run daily collection (yesterday)")
    parser.add_argument("--leagues", type=str, default="",
                        help="Comma-separated league IDs (overrides config)")
    parser.add_argument("--seasons", type=str, default="2020,2021,2022,2023,2024",
                        help="Comma-separated season years for backfill")
    parser.add_argument("--config", type=str, default="config/system.yaml",
                        help="Path to config file")
    args = parser.parse_args()

    setup_logging(level="INFO")

    config = SystemConfig.load(args.config)
    collector = DataCollector(config)
    await collector.start()

    try:
        leagues = args.leagues.split(",") if args.leagues else config.target_leagues
        seasons = args.seasons.split(",")

        if args.backfill:
            await collector.backfill_historical(leagues, seasons)
        elif args.backfill_stats:
            report = await collector.backfill_stats()
            print(f"\nStats backfill complete: {report}")
        elif args.backfill_odds:
            report = await collector.backfill_odds()
            print(f"\nOdds backfill complete: {report}")
        elif args.daily:
            await collector.collect_yesterday_results()
        else:
            log.info("no_action_specified",
                     hint="Use --backfill, --backfill-stats, --backfill-odds, or --daily")
    finally:
        await collector.stop()


if __name__ == "__main__":
    asyncio.run(_main())
