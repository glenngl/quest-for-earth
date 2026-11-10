"""SQLite persistence: accounts, characters, sessions.

Stdlib sqlite3, no ORM (D3). Every function opens its own short-lived
connection so calls from the event loop and the thread pool interleave
safely; WAL keeps readers unblocked. Password hashes and session tokens
are secrets: they never appear in logs or event payloads.
"""

import datetime as dt
import pathlib
import secrets
import sqlite3

from . import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    gender TEXT NOT NULL,
    race TEXT NOT NULL,
    class1 TEXT NOT NULL,
    class2 TEXT NOT NULL,
    strength INTEGER NOT NULL,
    intelligence INTEGER NOT NULL,
    wisdom INTEGER NOT NULL,
    dexterity INTEGER NOT NULL,
    constitution INTEGER NOT NULL,
    hp INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    room_id TEXT NOT NULL,
    weapon TEXT,
    armor TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    character_id INTEGER REFERENCES characters(id),
    created TEXT NOT NULL,
    expires TEXT NOT NULL
);
"""

_CHAR_COLS = ("id", "account_id", "name", "gender", "race", "class1", "class2",
              "strength", "intelligence", "wisdom", "dexterity", "constitution",
              "hp", "xp", "level", "room_id", "weapon", "armor", "created")

_SAVEABLE = ("name", "gender", "race", "class1", "class2", "strength",
             "intelligence", "wisdom", "dexterity", "constitution", "hp",
             "xp", "level", "room_id", "weapon", "armor")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    path = pathlib.Path(settings.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


# ---- accounts ---------------------------------------------------------------

def create_account(username: str, pw_hash: str) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (username, pw_hash, created) VALUES (?, ?, ?)",
            (username, pw_hash, _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def account_by_username(username: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---- sessions ---------------------------------------------------------------

def create_session(account_id: int, character_id, ttl_days: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc)
               + dt.timedelta(days=ttl_days)).isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token, account_id, character_id, created, expires)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, account_id, character_id, _now(), expires))
        conn.commit()
    finally:
        conn.close()
    return token


def session(token: str):
    if not token:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row or row["expires"] < _now():
            return None
        return dict(row)
    finally:
        conn.close()


def bind_character(token: str, character_id: int) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET character_id = ? WHERE token = ?",
                     (character_id, token))
        conn.commit()
    finally:
        conn.close()


# ---- characters -------------------------------------------------------------

def create_character(fields: dict) -> int:
    cols = ["account_id"] + list(_SAVEABLE) + ["created"]
    values = [fields[c] for c in cols]
    conn = connect()
    try:
        # identifiers come from the fixed _SAVEABLE whitelist, values are
        # bound parameters — nothing user-controlled reaches the SQL text
        cur = conn.execute(
            f"INSERT INTO characters ({', '.join(cols)}) "  # nosec B608
            f"VALUES ({', '.join('?' * len(cols))})", values)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def character_by_id(char_id: int):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def character_by_name(name: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM characters WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_character(char_id: int, **cols) -> None:
    updates = {k: v for k, v in cols.items() if k in _SAVEABLE}
    if not updates:
        return
    conn = connect()
    try:
        # identifiers come from the fixed _SAVEABLE whitelist, values are
        # bound parameters — nothing user-controlled reaches the SQL text
        conn.execute(
            f"UPDATE characters SET {', '.join(f'{c} = ?' for c in updates)} "  # nosec B608
            "WHERE id = ?",
            list(updates.values()) + [char_id])
        conn.commit()
    finally:
        conn.close()
