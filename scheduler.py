"""Simple polling scheduler for the PKM ingestion pipeline.

Runs the pipeline once immediately, then waits INTERVAL_HOURS before running
again. Designed to run as a long-lived Docker service.

Usage:
    python scheduler.py [--interval-hours N]
"""

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _run_pipeline() -> int:
    """Invoke main.py as a subprocess and return its exit code."""
    logger.info("[scheduler] Starting pipeline run at %s", _now_utc())
    result = subprocess.run(
        [sys.executable, "main.py"],
        check=False,
    )
    if result.returncode == 0:
        logger.info("[scheduler] Pipeline finished OK at %s", _now_utc())
    else:
        logger.error(
            "[scheduler] Pipeline exited with code %d at %s",
            result.returncode, _now_utc(),
        )
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="PKM pipeline scheduler")
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=6.0,
        metavar="N",
        help="Hours between pipeline runs (default: 6)",
    )
    args = parser.parse_args()

    interval_sec = args.interval_hours * 3600

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info(
        "[scheduler] Started. Will run pipeline every %.1f hours.", args.interval_hours
    )

    while True:
        _run_pipeline()
        logger.info(
            "[scheduler] Sleeping %.1f hours until next run.", args.interval_hours
        )
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
