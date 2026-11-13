"""Account, session and ship-creation rules.

Passwords are bcrypt-hashed; session tokens are opaque random strings.
Neither is ever logged or echoed back in an event. Ship creation
follows the classic pattern (D6, D14): the class sets the stat base,
the type modifies agility and rack sizes, and the character starts in
the game's only spawning point (Titan).
"""

import json
import re
import sqlite3

import bcrypt

from . import db

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,19}$")


class AuthError(Exception):
    """Raised for expected auth/creation failures; surfaces as HTTP 4xx."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), pw_hash.encode())
    except ValueError:
        return False


def register(username: str, password: str) -> str:
    if not USERNAME_RE.match(username or ""):
        raise AuthError("names start with a letter and are 2-20 characters")
    if not password or len(password) < 6:
        raise AuthError("passwords are at least 6 characters")
    try:
        account_id = db.create_account(username, hash_password(password))
    except sqlite3.IntegrityError:
        raise AuthError("that name is already taken") from None
    return db.create_session(account_id, None, int(_ttl_days()))


def login(username: str, password: str) -> str:
    account = db.account_by_username(username or "")
    if not account or not verify_password(password or "", account["pw_hash"]):
        raise AuthError("wrong name or password")
    return db.create_session(account["id"], None, int(_ttl_days()))


def _ttl_days() -> int:
    from . import settings  # call-time read (D3)
    return settings.get_int("session_ttl_days")


def session(token: str):
    return db.session(token)


def create_character(session_row: dict, fields: dict) -> dict:
    """Validate creation fields and store the starting fighter."""
    from . import settings, world  # call-time read (D3)

    name = (fields.get("name") or "").strip()
    if not USERNAME_RE.match(name):
        raise AuthError("character names start with a letter, 2-20 characters")
    if db.character_by_name(name):
        raise AuthError("that character name is already taken")

    cls = world.ship_class(fields.get("cls") or "")
    if not cls:
        raise AuthError("pick a ship class: bug, human or hybrid")
    ship_type = world.ship_type(fields.get("type") or "")
    if not ship_type:
        raise AuthError("pick a ship type: fighter, frigate or warship")
    if ship_type["min_level"] > 1:
        raise AuthError(f"the {ship_type['name']} hull requires level "
                        f"{ship_type['min_level']} — start with a fighter")

    stats = dict(cls["stats"])
    stats["agi"] = max(1, min(10, stats["agi"] + ship_type["agi_mod"]))
    starting_room = settings.get("starting_room")
    if starting_room not in world.rooms():
        raise AuthError("world is missing the starting room")
    try:
        char_id = db.create_character({
            "account_id": session_row["account_id"],
            "name": name,
            "cls": cls["id"],
            "type": ship_type["id"],
            "stats": json.dumps(stats),
            "hp": world.max_hull(1),
            "xp": 0,
            "level": 1,
            "room_id": starting_room,
            "guns": json.dumps(["l1-kinetic"]),
            "plates": json.dumps(["l1-plate"]),
            "artifacts": json.dumps([]),
            "energy": ship_type["energy_cap"],
            "chart": json.dumps({}),
            "created": "now",
        })
    except sqlite3.IntegrityError:
        raise AuthError("that character name is already taken") from None
    db.bind_character(session_row["token"], char_id)
    return db.character_by_id(char_id)
