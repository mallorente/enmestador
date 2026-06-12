"""CLI: build the field-organized wiki from the enriched vault.

Usage:
    python -m scripts.build_wiki
    python -m scripts.build_wiki --output ~/enmestador-wiki
"""

import argparse
import logging
from pathlib import Path

from analysis.wiki import build_wiki
from config import DEFAULT_OUTPUT_DIR, WIKI_FIELDS, WIKI_OUTPUT_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the wiki from the vault")
    parser.add_argument("--bookmarks-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--output", type=Path, default=Path(WIKI_OUTPUT_DIR),
        help="Output wiki directory (default: from WIKI_OUTPUT_DIR)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    index = build_wiki(args.bookmarks_dir, args.output, WIKI_FIELDS)
    print(f"Wiki written to {index.parent} (index: {index})")


if __name__ == "__main__":
    main()
