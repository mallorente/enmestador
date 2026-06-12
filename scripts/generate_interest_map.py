"""CLI: build the interest map from the enriched vault.

Usage:
    python -m scripts.generate_interest_map
    python -m scripts.generate_interest_map --bookmarks-dir DIR --output FILE
"""

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from analysis.interest_map import generate_interest_map
from config import DEFAULT_OUTPUT_DIR, INTEREST_MAP_HTML

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the interest map from the vault")
    parser.add_argument(
        "--bookmarks-dir",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help=f"Directory with the .json note sidecars (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output Markdown file (default: <vault>/wiki/interest-map.md)",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=Path(INTEREST_MAP_HTML),
        help="Output HTML file (default: from INTEREST_MAP_HTML)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    output = args.output
    if output is None:
        # <vault>/Bookmarks/bookmarks → <vault>/wiki/interest-map.md
        vault_root = args.bookmarks_dir.parent.parent
        output = vault_root / "wiki" / "interest-map.md"

    path = asyncio.run(generate_interest_map(args.bookmarks_dir, output, html_path=args.html))
    print(f"Interest map written to {path}")
    print(f"Interest map HTML written to {args.html}")


if __name__ == "__main__":
    main()
