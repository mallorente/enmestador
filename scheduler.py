"""Polling scheduler for the PKM ingestion pipeline.

Runs the pipeline once immediately, then sleeps INTERVAL_HOURS before
running again. Designed to run as a long-lived Docker service.

Usage:
    python scheduler.py [--interval-hours N]
"""

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def _run_pipeline_once() -> None:
    from main import run_pipeline
    logger.info("[scheduler] Pipeline start at %s", _now_utc())
    try:
        result = await run_pipeline()
        logger.info(
            "[scheduler] Pipeline done at %s — processed=%d enriched=%d dead=%d",
            _now_utc(), result.processed, result.enriched, result.dead_letter,
        )
    except Exception:
        logger.exception("[scheduler] Pipeline crashed at %s", _now_utc())


def main() -> None:
    parser = argparse.ArgumentParser(description="PKM pipeline scheduler")
    parser.add_argument(
        "--interval-hours", type=float, default=6.0, metavar="N",
        help="Hours between pipeline runs (default: 6)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    interval_sec = args.interval_hours * 3600
    logger.info("[scheduler] Started — interval=%.1fh", args.interval_hours)

    while True:
        asyncio.run(_run_pipeline_once())
        logger.info("[scheduler] Sleeping %.1fh until next run", args.interval_hours)
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
