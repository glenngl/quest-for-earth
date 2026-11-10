"""World content loader: everything about the world is data (D1).

Rooms, exits, spawns, mobs, items, races and combat balance live in JSON
files under the package ``data/`` directory; adding content means
editing JSON, never code. Files are re-read when their mtime changes,
so a sysop can hot-edit a data file and the next call picks it up
without a container restart.
"""

import json
import pathlib

from . import settings

_cache: dict = {}


def _load(name: str):
    path = pathlib.Path(settings.data_dir()) / f"{name}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise RuntimeError(f"world data file missing: {path}") from exc
    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    _cache[name] = (mtime, data)
    return data


def rooms() -> dict:
    return _load("world")["rooms"]


def room(room_id: str) -> dict:
    try:
        return rooms()[room_id]
    except KeyError:
        raise KeyError(f"unknown room: {room_id}") from None


def snapshot(room_id: str) -> dict:
    """Client-facing room view: id, name, area, desc, exits, safety, tint."""
    data = room(room_id)
    return {
        "id": data["id"],
        "name": data["name"],
        "area": data.get("area", ""),
        "desc": data.get("desc", ""),
        "exits": data.get("exits", {}),
        "safe": data.get("safe", False),
        "tint": data.get("tint", "#1a1f2e"),
    }


def mobs() -> dict:
    return _load("mobs")


def mob(mob_id: str) -> dict:
    try:
        return mobs()[mob_id]
    except KeyError:
        raise KeyError(f"unknown mob: {mob_id}") from None


def items() -> dict:
    return _load("items")


def item(item_id: str):
    return items().get(item_id)


def races() -> list:
    return _load("races")


def race(name: str):
    return next((r for r in races() if r["name"].lower() == name.lower()), None)


def classes() -> dict:
    return _load("classes")


def primary_classes() -> list:
    return classes()["primary"]


def primary_class_ids() -> list:
    return [c["id"] for c in primary_classes()]


def primary_stat(class_id: str) -> str:
    cls = next(c for c in primary_classes() if c["id"] == class_id)
    return cls["primary_stat"]


def balance() -> dict:
    return _load("balance")


def max_hp(stats: dict, level: int) -> int:
    """Stats use the short keys: str, int, wis, dex, con."""
    hp = balance()["hp"]
    return (hp["base"] + stats["con"] * hp["per_con"]
            + (level - 1) * hp["per_level"])


def xp_next(level: int) -> int:
    return balance()["xp"]["per_level"] * level


def xp_reward(mob_template: dict) -> int:
    xp = balance()["xp"]
    return xp["per_kill_base"] + mob_template["level"] * xp["per_kill_level"]
