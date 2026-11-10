"""Where everything lives. One place, so nothing is hardcoded twice."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")

DATA_DIR = Path(os.environ.get("SHIFTLOAD_DATA", Path.home() / "shiftload-data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "shiftload.db"

GARMIN_API_KEY = os.environ.get("GARMIN_API_KEY", "").strip()

# The FIRE calendar, read live over CalDAV. The local vdirsyncer mirror is stale.
CALDAV_BASE = "https://caldav.fastmail.com/dav/calendars/user/jimmybowman@fastmail.com/"
FIRE_CALENDAR = "2733c6be-e7d9-4650-a3db-2d6ae491862e"
VDIRSYNCER_CONFIG = Path.home() / ".config" / "vdirsyncer" / "config"

# 48/96 cycle: a shift starts on the anchor date and runs two days, then four off.
CYCLE_ANCHOR = "2026-01-29"
CYCLE_PERIOD = 6
SHIFT_DAYS = 2

BACKFILL_START = "2026-01-01"
SHIFT_HORIZON_DAYS = 60


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR):
        d.mkdir(parents=True, exist_ok=True)
