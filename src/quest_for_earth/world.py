"""World content loader: everything about the world is data (D1).

Rooms (the solar-system graph), jump points, spawns, mobs, items, ship
classes/types and combat balance live in JSON files under the package
``data/`` directory; adding content means editing JSON, never code.
Files are re-read when their mtime changes, so a sysop can hot-edit a
data file and the next call picks it up without a container restart.
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
    """Client-facing room view."""
    data = room(room_id)
    return {
        "id": data["id"],
        "name": data["name"],
        "kind": data.get("kind", "lane"),
        "system": data.get("system", ""),
        "body": data.get("body", ""),
        "desc": data.get("desc", ""),
        "exits": data.get("exits", {}),
        "safe": data.get("safe", False),
        "tint": data.get("tint", "#141b26"),
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


def item_name(item_id: str) -> str:
    it = items().get(item_id)
    return it["name"] if it else item_id


def ships() -> dict:
    return _load("ships")


def ship_class(cls_id: str):
    data = ships()["classes"].get(cls_id.lower())
    if data:
        return {"id": cls_id.lower(), **data}
    return None


def ship_type(type_id: str):
    data = ships()["types"].get(type_id.lower())
    if data:
        return {"id": type_id.lower(), **data}
    return None


def balance() -> dict:
    return _load("balance")


def lighting() -> dict:
    """Per-vertex WebGL light model (D1: renderer visuals are data too)."""
    return _load("lighting")


def max_hull(level: int) -> int:
    hp = balance()["hull"]
    return hp["base"] + (level - 1) * hp["per_level"]


def xp_next(level: int) -> int:
    return balance()["xp"]["per_level"] * level


def xp_reward(mob_template: dict) -> int:
    xp = balance()["xp"]
    return xp["per_kill_base"] + mob_template["level"] * xp["per_kill_level"]
