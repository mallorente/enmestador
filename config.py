import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_STATE_DIR = os.getenv("STATE_DIR", "state")
DEFAULT_OUTPUT_DIR = os.getenv("OUTPUT_DIR", "volumes/llm_wiki_seed/Bookmarks/bookmarks")
DEFAULT_USER_DATA_DIR = os.getenv("USER_DATA_DIR", "volumes/user_data")
MAX_BOOKMARKS_PER_SOURCE = int(os.getenv("MAX_BOOKMARKS", "500"))
MAX_EXTERNAL_ARTICLES = int(os.getenv("MAX_EXTERNAL_ARTICLES", "3"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
DELTA_STOP_AFTER_KNOWN = int(os.getenv("DELTA_STOP_AFTER_KNOWN", "2"))
DELTA_FRONTIER_SIZE = int(os.getenv("DELTA_FRONTIER_SIZE", "20"))

FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "30.0"))
FETCH_MAX_RETRIES = int(os.getenv("FETCH_MAX_RETRIES", "3"))
FETCH_BACKOFF_BASE = float(os.getenv("FETCH_BACKOFF_BASE", "1.0"))
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "5"))

LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RATE_LIMIT_SEC = float(os.getenv("LLM_RATE_LIMIT_SEC", "1.0"))
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_MODEL_FALLBACK_1 = os.getenv("LLM_MODEL_FALLBACK_1", "")
LLM_MODEL_FALLBACK_2 = os.getenv("LLM_MODEL_FALLBACK_2", "")

# Extra providers (different base_url + api_key)
LLM_FALLBACK_3_BASE_URL = os.getenv("LLM_FALLBACK_3_BASE_URL", "")
LLM_FALLBACK_3_API_KEY = os.getenv("LLM_FALLBACK_3_API_KEY", "")
LLM_FALLBACK_3_MODEL = os.getenv("LLM_FALLBACK_3_MODEL", "")

LLM_FALLBACK_4_BASE_URL = os.getenv("LLM_FALLBACK_4_BASE_URL", "")
LLM_FALLBACK_4_API_KEY = os.getenv("LLM_FALLBACK_4_API_KEY", "")
LLM_FALLBACK_4_MODEL = os.getenv("LLM_FALLBACK_4_MODEL", "")

LOCK_STALE_HOURS = float(os.getenv("LOCK_STALE_HOURS", "4.0"))

# Life-area "fields" used to classify each note for the wiki. Override with a
# comma-separated WIKI_FIELDS env var to fit your own areas of interest.
WIKI_FIELDS = [
    f.strip()
    for f in os.getenv(
        "WIKI_FIELDS", "Home Automation,Personal Agents,Legal Tech,Other Tech"
    ).split(",")
    if f.strip()
]

# Where the wiki and the interest-map HTML are written.
WIKI_OUTPUT_DIR = os.getenv("WIKI_OUTPUT_DIR", str(Path.home() / "enmestador-wiki"))
INTEREST_MAP_HTML = os.getenv(
    "INTEREST_MAP_HTML", str(Path.home() / "enmestador" / "interest-map.html")
)
