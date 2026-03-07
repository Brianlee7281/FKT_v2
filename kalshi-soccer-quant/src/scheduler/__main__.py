"""Entry point: python -m src.scheduler → runs MatchScheduler."""

import asyncio
import signal

import redis.asyncio as aioredis

from src.common.config import SystemConfig
from src.scheduler.main import MatchScheduler


def main() -> None:
    config = SystemConfig.load()

    redis_client = aioredis.from_url(config.redis_url) if config.redis_url else None
    scheduler = MatchScheduler(config, redis_client=redis_client)

    loop = asyncio.new_event_loop()

    # Graceful shutdown on SIGTERM/SIGINT
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(scheduler.stop()))

    try:
        loop.run_until_complete(scheduler.start())
    except KeyboardInterrupt:
        loop.run_until_complete(scheduler.stop())
    finally:
        if redis_client:
            loop.run_until_complete(redis_client.aclose())
        loop.close()


if __name__ == "__main__":
    main()
