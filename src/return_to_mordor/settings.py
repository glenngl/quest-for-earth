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
    "tick_ms": 900,
    "starting_room": "school",
    "session_ttl_days": 30,
}


def get(key: str):
    """Effective value of a setting: R2M_<KEY> env var, else default."""
    env = os.environ.get("R2M_" + key.upper())
    if env not in (None, ""):
        return env
    return DEFAULTS.get(key)


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, 0))


def db_path() -> str:
    """SQLite file: R2M_DB, else the /data volume (container), else ./.data."""
    env = os.environ.get("R2M_DB")
    if env:
        return env
    if os.path.isdir("/data"):
        return "/data/return-to-mordor.sqlite3"
    return str(pathlib.Path(".data") / "return-to-mordor.sqlite3")


def data_dir() -> str:
    """Directory holding the world JSON files (rooms, mobs, races, ...)."""
    env = os.environ.get("R2M_DATA")
    if env:
        return env
    return str(pathlib.Path(__file__).with_name("data"))
