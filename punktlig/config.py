"""Configuration via environment variables, with sensible defaults for local collection."""

import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("PUNKTLIG_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("PUNKTLIG_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("PUNKTLIG_DB", DATA_DIR / "punktlig.db"))
RAW_DIR = Path(os.environ.get("PUNKTLIG_RAW", DATA_DIR / "raw"))

# Entur requires an identifying client name header on all requests.
CLIENT_NAME = os.environ.get("PUNKTLIG_CLIENT_NAME", "punktlig-collector")

# Which codespace/operator to collect, and which transport modes to keep.
# Start small (Oslo tram + metro); widen to more modes/operators by config only.
# Several codespaces can be polled in one cycle, each with its own delta
# stream. Trains live under NSB rather than the local authority, so covering
# a region's rail means naming both.
DATASETS = [d.strip() for d in os.environ.get("PUNKTLIG_DATASET", "RUT").split(",") if d.strip()]
AUTHORITIES = [
    a.strip()
    for a in os.environ.get("PUNKTLIG_AUTHORITY", "RUT:Authority:RUT").split(",")
    if a.strip()
]
MODES = [m.strip() for m in os.environ.get("PUNKTLIG_MODES", "tram,metro").split(",") if m.strip()]

# The first codespace is the one the project is built around and is polled
# every cycle. The rest share the remaining request budget on a slower
# cadence, because the feed rate limits a client across all of them.
SECONDARY_EVERY = int(os.environ.get("PUNKTLIG_SECONDARY_EVERY", "120"))

# Kept for the modules that only ever describe one poll at a time.
DATASET = DATASETS[0] if DATASETS else ""

# Weather point for feature collection (default: central Oslo).
WEATHER_LAT = float(os.environ.get("PUNKTLIG_LAT", "59.9139"))
WEATHER_LON = float(os.environ.get("PUNKTLIG_LON", "10.7522"))
# MET Norway requires a User-Agent identifying the application.
MET_USER_AGENT = os.environ.get(
    "PUNKTLIG_MET_UA", "punktlig-collector/0.1 (https://github.com/your-user/punktlig)"
)

ET_URL = "https://api.entur.io/realtime/v1/rest/et"
SX_URL = "https://api.entur.io/realtime/v1/rest/sx"
GRAPHQL_URL = "https://api.entur.io/journey-planner/v3/graphql"

# Journeys per SIRI page; the poller follows MoreData until the delta is exhausted.
PAGE_SIZE = int(os.environ.get("PUNKTLIG_PAGE_SIZE", "1500"))
MAX_PAGES = int(os.environ.get("PUNKTLIG_MAX_PAGES", "20"))

# Secondary feeds are polled at most this often (seconds).
WEATHER_EVERY = int(os.environ.get("PUNKTLIG_WEATHER_EVERY", "3300"))
SX_EVERY = int(os.environ.get("PUNKTLIG_SX_EVERY", "3300"))
LINES_EVERY = int(os.environ.get("PUNKTLIG_LINES_EVERY", str(24 * 3600)))

# Storage tiering: completed days move from hot SQLite to day-partitioned
# Parquet, and raw XML is pruned after a retention window.
PARQUET_DIR = Path(os.environ.get("PUNKTLIG_PARQUET", DATA_DIR / "parquet"))
HOT_KEEP_DAYS = int(os.environ.get("PUNKTLIG_HOT_KEEP_DAYS", "7"))
RAW_KEEP_DAYS = int(os.environ.get("PUNKTLIG_RAW_KEEP_DAYS", "30"))
