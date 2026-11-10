"""Account, session and character-creation rules.

Passwords are bcrypt-hashed; session tokens are opaque random strings.
Neither is ever logged or echoed back in an event. Race/class/stat
validations here mirror the classic Mordor rules (D6, D7):
race determines the attribute base, the primary class grants +1 to its
primary attribute, and the character starts in the newbie school.
"""

import re
import sqlite3

import bcrypt

from . import db

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,19}$")
GENDERS = {"male", "female", "other"}


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
    return db.create_session(
        account_id, None, int(_ttl_days()))


def login(username: str, password: str) -> str:
    account = db.account_by_username(username or "")
    if not account or not verify_password(password or "", account["pw_hash"]):
        raise AuthError("wrong name or password")
    return db.create_session(account["id"], None, int(_ttl_days()))


def _ttl_days() -> int:
    from . import settings  # call-time read (D2)
    return settings.get_int("session_ttl_days")


def session(token: str):
    return db.session(token)


def create_character(session_row: dict, fields: dict) -> dict:
    """Validate creation fields against the classic rules and store them."""
    from . import settings, world  # call-time read (D2)

    name = (fields.get("name") or "").strip()
    if not USERNAME_RE.match(name):
        raise AuthError("character names start with a letter, 2-20 characters")
    if db.character_by_name(name):
        raise AuthError("that character name is already taken")

    gender = fields.get("gender")
    if gender not in GENDERS:
        raise AuthError("pick a gender")

    race = world.race(fields.get("race") or "")
    if not race:
        raise AuthError("pick a valid race")

    class1, class2 = fields.get("class1"), fields.get("class2")
    if class1 not in world.primary_class_ids():
        raise AuthError("pick a primary class")
    if class2 not in world.primary_class_ids():
        raise AuthError("pick a secondary class")

    stats = dict(race["stats"])
    primary_stat = world.primary_stat(class1)
    stats[primary_stat] += 1  # the classic +1 to the primary class's stat
    starting_room = settings.get("starting_room")
    if starting_room not in world.rooms():
        raise AuthError("world is missing the starting room")
    try:
        char_id = db.create_character({
            "account_id": session_row["account_id"],
            "name": name,
            "gender": gender,
            "race": race["name"],
            "class1": class1,
            "class2": class2,
            "strength": stats["str"],
            "intelligence": stats["int"],
            "wisdom": stats["wis"],
            "dexterity": stats["dex"],
            "constitution": stats["con"],
            "hp": world.max_hp(stats, 1),
            "xp": 0,
            "level": 1,
            "room_id": starting_room,
            "weapon": None,
            "armor": None,
            "created": "now",
        })
    except sqlite3.IntegrityError:
        raise AuthError("that character name is already taken") from None
    db.bind_character(session_row["token"], char_id)
    return db.character_by_id(char_id)
