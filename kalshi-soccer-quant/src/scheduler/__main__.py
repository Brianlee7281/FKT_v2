"""Entry point: python -m src.scheduler.main → runs MatchScheduler."""

import asyncio
import signal

from src.common.config import SystemConfig
from src.scheduler.main import MatchScheduler


def main() -> None:
    config = SystemConfig.load()
    scheduler = MatchScheduler(config)

    loop = asyncio.new_event_loop()

    # Graceful shutdown on SIGTERM/SIGINT
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(scheduler.stop()))

    try:
        loop.run_until_complete(scheduler.start())
    except KeyboardInterrupt:
        loop.run_until_complete(scheduler.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
