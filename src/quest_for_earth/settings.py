"""Runtime settings — environment first, then defaults.

Read through ``settings.get(...)`` inside the function that needs a
value, never at module import time (D2 in docs/DECISIONS.md). Nothing
user-configurable is cached, so a restarted or re-read setting applies
live. The only bootstrap facts read eagerly are bind address/port,
resolved at process start in ``__main__``.
"""

import os
import pathlib

DEFAULTS = {
    "port": 8000,
    "tick_ms": 1500,
    "starting_room": "titan_base",
    "session_ttl_days": 30,
}


def get(key: str):
    """Effective value of a setting: QFE_<KEY> env var, else default."""
    env = os.environ.get("QFE_" + key.upper())
    if env not in (None, ""):
        return env
    return DEFAULTS.get(key)


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, 0))


def db_path() -> str:
    """SQLite file: QFE_DB, else the /data volume (container), else ./.data."""
    env = os.environ.get("QFE_DB")
    if env:
        return env
    if os.path.isdir("/data"):
        return "/data/quest-for-earth.sqlite3"
    return str(pathlib.Path(".data") / "quest-for-earth.sqlite3")


def data_dir() -> str:
    """Directory holding the world JSON files (rooms, mobs, items, ships)."""
    env = os.environ.get("QFE_DATA")
    if env:
        return env
    return str(pathlib.Path(__file__).with_name("data"))
