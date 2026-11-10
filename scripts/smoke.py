"""Offline smoke test: every endpoint + the live game loop, zero network.

Run before pushing:  ``python scripts/smoke.py`` — CI runs the same file
and blocks the image build on it.

Like its company-sleuth ancestor, this starts the real app on a local
port and drives every endpoint through real handler code. There is no
outbound I/O to stub (the world is local JSON, the DB is throwaway), so
the harness instead drives the *game loop*: the background tick is
stopped and ``engine.tick_once()`` is called explicitly so combat,
aggro and XP are deterministic. Two WebSocket clients (a warrior and a
cleric) play a real session: register → create → move → fight → vigor →
disconnect → reconnect right where they left off (the D8 invariant).
"""

import json
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# Isolate the world state BEFORE any import: throwaway DB, no env.
_TMP = tempfile.mkdtemp(prefix="r2m-smoke-")
os.environ["R2M_DB"] = os.path.join(_TMP, "smoke.sqlite3")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from return_to_mordor import engine  # noqa: E402
from return_to_mordor.web import app  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

PORT = 8399


def start_server():
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("smoke server did not start")


def get(path, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as r:
        return r.status, r.read()


def post(path, payload, headers=None, expect_error=False):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data,
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return exc.code, json.loads(exc.read())


def http(method, path, payload, headers=None, want_error=False):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data,
        headers={"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if not want_error:
            raise
        return exc.code, json.loads(exc.read())


FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class Game:
    """A WebSocket player session."""

    def __init__(self, token):
        self.token = token
        self.sock = ws_connect(
            f"ws://127.0.0.1:{PORT}/ws?token={token}", open_timeout=5)

    def send(self, obj):
        self.sock.send(json.dumps(obj))

    def recv(self, timeout=5):
        return json.loads(self.sock.recv(timeout=timeout))

    def wait_for(self, pred, attempts=80, pump=False):
        for _ in range(attempts):
            if pump:
                engine.tick_once()
            try:
                msg = self.recv(timeout=0.1)
            except TimeoutError:
                continue
            if pred(msg):
                return msg
        return None

    def close(self):
        self.sock.close()


def run_checks():
    # ---- static client + health ----
    status, body = get("/")
    html = body.decode()
    check("GET / serves the game UI", status == 200 and "Return to Mordor" in html)
    status, body = get("/health")
    check("GET /health", status == 200 and json.loads(body)["ok"] is True)
    status, body = get("/static/vendor/three.module.js")
    check("GET /static/vendor/three.module.js", status == 200 and b"Three.js" in body[:100])
    for path in ("/static/style.css", "/static/game.js"):
        status, _ = get(path)
        check(f"GET {path}", status == 200)

    # ---- creation catalogue ----
    status, body = get("/api/races")
    races = json.loads(body)["races"]
    check("GET /api/races (30 races, Mordor's table)", status == 200 and len(races) == 30)
    dwarf = next(r for r in races if r["name"] == "Dwarf")
    check("race table matches the classic stats",
          dwarf["stats"]["str"] == 19 and dwarf["stats"]["dex"] == 16)
    status, body = get("/api/classes")
    classes = json.loads(body)
    check("GET /api/classes (5 primary classes)",
          status == 200 and len(classes["primary"]) == 5
          and any(c["id"] == "cleric" for c in classes["primary"]))

    # ---- accounts ----
    status, d = post("/api/register", {"username": "waldo", "password": "hunter22"})
    token = d["token"]
    check("POST /api/register", status == 200 and token and d["character"] is None)
    status, d = http("POST", "/api/register", {"username": "WALDO", "password": "x" * 9}, {},
                     want_error=True)
    check("duplicate account rejected", d.get("detail") == "that name is already taken")
    status, d = http("POST", "/api/register", {"username": "ok-name", "password": "abc"}, {},
                     want_error=True)
    check("weak password rejected", d.get("detail") == "passwords are at least 6 characters")
    status, d = http("POST", "/api/login", {"username": "waldo", "password": "wrong!"}, {},
                     want_error=True)
    check("bad login rejected", d.get("detail") == "wrong name or password")
    status, d = http("POST", "/api/login", {"username": "waldo", "password": "hunter22"}, {})
    check("POST /api/login", status == 200 and d["token"] and d["character"] is None)

    # ---- character creation ----
    status, d = http("POST", "/api/character", {"name": "x", "race": "Goblin"},
                     {"X-Token": token}, want_error=True)
    check("creation needs full fields", status == 400)
    status, d = http("POST", "/api/character",
                     {"name": "Herkn", "gender": "male", "race": "Gondorian",
                      "class1": "warrior", "class2": "ranger"},
                     {"X-Token": token})
    char = d["character"]
    check("POST /api/character starts in the newbie school",
          status == 200 and char["room_id"] == "school")
    check("race stat + primary-class bonus (Gondorian str 16 +1 warrior)",
          char["strength"] == 17)
    status, d = get("/api/me", headers={"X-Token": token})
    check("GET /api/me returns the character", status == 200
          and json.loads(d)["character"]["name"] == "Herkn")

    status, d2 = post("/api/register", {"username": "brilwen", "password": "hunter22"})
    token2 = d2["token"]
    status, d = http("POST", "/api/character",
                     {"name": "Brilwen", "gender": "female", "race": "Avari",
                      "class1": "cleric", "class2": "mage"},
                     {"X-Token": token2})
    check("cleric creation (+1 wisdom)", d["character"]["wisdom"] == 20)

    # ---- game session (the real loop) ----
    engine.stop_tick()          # determinism: ticks only when we say so
    engine.rng = random.Random(42)

    herkn = Game(token)
    welcome = herkn.wait_for(lambda m: m["t"] == "welcome")
    check("WS welcome: name + newbie school",
          welcome and welcome["you"]["name"] == "Herkn"
          and welcome["room"]["id"] == "school")

    herkn.send({"t": "move", "dir": "north"})   # school has no north exit
    toast = herkn.wait_for(lambda m: m["t"] == "toast")
    check("blocked move → classic toast", toast
          and toast["text"] == "You can't go that way.")

    herkn.send({"t": "move", "dir": "south"})
    room = herkn.wait_for(lambda m: m["t"] == "room")
    check("move south → the byway", room and room["room"]["id"] == "byway")

    # a goblin wanders into the byway (spawn point injected for determinism)
    entry = {"room": "byway", "mob": "goblin", "respawn": 45, "next": 0.0, "inst": None}
    engine.SPAWNS.append(entry)
    engine._spawn_instance(entry)
    mob_id = entry["inst"]

    herkn.send({"t": "attack", "target": "goblin"})
    events = herkn.wait_for(lambda m: m["t"] == "combat" and any(
        e["kind"] in ("swing", "miss") for e in m["events"]), attempts=40)
    check("combat starts (swing or miss broadcast)",
          events is not None)

    dead = None
    swings = 0
    for _ in range(60):
        engine.tick_once()
        try:
            msg = herkn.recv(timeout=0.05)
        except TimeoutError:
            continue
        if msg["t"] == "combat":
            for ev in msg["events"]:
                if ev["kind"] in ("swing", "miss"):
                    swings += 1
                if ev["kind"] == "die" and ev["who"] == "Goblin":
                    dead = ev
        if dead:
            break
    check("goblin dies after a few rounds", dead is not None, f"swings={swings}")
    check("xp awarded for the kill", dead and dead["xp"] >= 35, str(dead and dead["xp"]))

    # ---- the cleric joins: vigor on the teammate ----
    brilwen = Game(token2)
    brilwen.wait_for(lambda m: m["t"] == "welcome")
    brilwen.send({"t": "move", "dir": "south"})
    brilwen.wait_for(lambda m: m["t"] == "room" and m["room"]["id"] == "byway")

    player = next(a for a in engine.PLAYERS.values() if a["name"] == "Herkn")
    player["hp"] = 10
    brilwen.send({"t": "vigor", "target": "Herkn"})
    cast = herkn.wait_for(lambda m: m["t"] == "combat" and any(
        e["kind"] == "cast" for e in m["events"]), attempts=40)
    check("vigor heals the teammate", cast is not None and player["hp"] > 10,
          f"hp={player.get('hp')}")

    brilwen.send({"t": "vigor", "target": "Herkn"})
    cooldown = brilwen.wait_for(lambda m: m["t"] == "toast"
                                and "not ready" in m["text"], attempts=20)
    check("vigor has a cooldown", cooldown is not None)

    # a warrior cannot cast vigor
    herkn.send({"t": "vigor", "target": "Herkn"})
    denied = herkn.wait_for(lambda m: m["t"] == "toast" and "Only clerics" in m["text"],
                            attempts=20)
    check("vigor is cleric-only", denied is not None)

    # ---- chat ----
    herkn.send({"t": "say", "text": "hallo, Middle-earth"})
    heard = brilwen.wait_for(lambda m: m["t"] == "say", attempts=20)
    check("say reaches the room", heard and heard["text"] == "hallo, Middle-earth")

    # ---- persistence: leave and come back ----
    herkn.close()
    brilwen.close()
    time.sleep(0.3)  # let the disconnect save land

    again = Game(token)
    welcome = again.wait_for(lambda m: m["t"] == "welcome")
    check("reconnect resumes where you left off",
          welcome and welcome["room"]["id"] == "byway")
    check("hp persisted across the reconnect", welcome["you"]["hp"] > 10)
    again.close()


def main():
    print(f"smoke: world state isolated in {_TMP}")
    start_server()
    try:
        run_checks()
    finally:
        engine.stop_tick()
    if FAILURES:
        print(f"\nsmoke FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        sys.exit(1)
    print("\nsmoke PASSED — every endpoint and the live game loop ran through real code")


if __name__ == "__main__":
    main()
