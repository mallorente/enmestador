import sys
from pathlib import Path


def healthcheck():
    state_dir = Path("/app/volumes/state")
    output_dir = Path("/app/volumes/llm_wiki_seed/Bookmarks/bookmarks")
    user_data_dir = Path("/app/volumes/user_data")

    for d in [state_dir, output_dir, user_data_dir]:
        if not d.exists():
            print(f"MISSING: {d}")
            sys.exit(1)

    lock_file = state_dir / "pipeline.lock"
    if lock_file.exists():
        print("LOCKED: pipeline currently running")
        sys.exit(1)

    print("HEALTHY")
    sys.exit(0)

if __name__ == "__main__":
    healthcheck()
