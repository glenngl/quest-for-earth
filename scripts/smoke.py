"""Offline smoke test: every endpoint + the live game loop, zero network.

Run before pushing:  ``python scripts/smoke.py`` — CI runs the same file
and blocks the image build on it.

Drives the Quest for Earth game loop through real handler code: ship
creation, movement, passive scanning, the chart broker, the shooter
overlay (aimed and wide shots), evasion, affiliates/whispers, salvage
and death. The background tick is stopped and ``engine.tick_once()`` is
called explicitly so rounds are deterministic.
"""

import json
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import urllib.request

# Isolate the world state BEFORE any import: throwaway DB, no env.
_TMP = tempfile.mkdtemp(prefix="qfe-smoke-")
os.environ["QFE_DB"] = os.path.join(_TMP, "smoke.sqlite3")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from quest_for_earth import engine  # noqa: E402
from quest_for_earth.web import app  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

PORT = 8401


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


def get_raw(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
        return r.status, r.read()


def get(path, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as r:
        return r.status, json.loads(r.read())


def http(method, path, payload, headers=None, want_error=False):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
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


class AlwaysFirst:
    """Stub rng: shots never crit, evasion always picks the first exit."""

    def random(self): return 0.0
    def uniform(self, a, b): return a
    def randrange(self, *args): return 0
    def randint(self, a, b): return a


def run_checks():
    # ---- static client + health ----
    status, html = get_raw("/")
    check("GET / serves the game UI",
          status == 200 and b"Quest for Earth" in html)
    status, body = get("/health")
    check("GET /health", status == 200 and body["ok"] is True)
    status, _ = get_raw("/static/style.css")
    check("GET /static/style.css", status == 200)
    status, _ = get_raw("/static/game.js")
    check("GET /static/game.js", status == 200)

    # ---- ship catalogue ----
    status, ships = get("/api/ships")
    check("GET /api/ships (3 classes, 3 types)",
          status == 200 and len(ships["classes"]) == 3 and len(ships["types"]) == 3)
    check("bug stats: high repair, high tracking",
          ships["classes"]["bug"]["stats"]["rep"] == 9
          and ships["classes"]["bug"]["stats"]["trk"] == 8)

    # ---- accounts ----
    status, d = http("POST", "/api/register", {"username": "riker", "password": "hunter22"})
    token = d["token"]
    check("POST /api/register", status == 200 and token and d["character"] is None)
    status, d = http("POST", "/api/register", {"username": "RIKER", "password": "x" * 9},
                     want_error=True)
    check("duplicate account rejected", d.get("detail") == "that name is already taken")
    status, d = http("POST", "/api/login", {"username": "riker", "password": "wrong!"},
                     want_error=True)
    check("bad login rejected", d.get("detail") == "wrong name or password")

    # ---- ship creation (human fighter) ----
    status, d = http("POST", "/api/character",
                     {"name": "Vesta", "cls": "human", "type": "warship"},
                     {"X-Token": token}, want_error=True)
    check("warship locked at level 1", status == 400)
    status, d = http("POST", "/api/character",
                     {"name": "Vesta", "cls": "human", "type": "fighter"},
                     {"X-Token": token})
    char = d["character"]
    check("POST /api/character starts at Titan",
          status == 200 and char["room"] == "titan_base")
    check("human fighter: agi 5+4=9, rep 2, nrg 9",
          char["stats"]["agi"] == 9 and char["stats"]["rep"] == 2
          and char["stats"]["nrg"] == 9)
    check("starter fighter: one L1 gun, one L1 plate",
          char["guns"] == ["l1-kinetic"] and char["plates"] == ["l1-plate"])

    # ---- game session ----
    engine.stop_tick()          # determinism: ticks only when we say so
    engine.rng = random.Random(42)

    vesta = Game(token)
    welcome = vesta.wait_for(lambda m: m["t"] == "welcome")
    check("WS welcome: Titan docks, own room charted",
          welcome and welcome["you"]["name"] == "Vesta"
          and welcome["room"]["id"] == "titan_base"
          and any(e["label"] == "Titan" for e in welcome["chart"]))

    vesta.send({"t": "move", "dir": "north"})
    room = vesta.wait_for(lambda m: m["t"] == "room")
    check("move north → Titan orbit", room and room["room"]["id"] == "titan_orbit")

    # passive scan reveals other bodies of the same system over rounds
    for _ in range(14):
        engine.tick_once()
    scanned = vesta.wait_for(lambda m: m["t"] == "scan", attempts=10)
    check("passive scan plotted a body", scanned is not None)

    # ---- chart broker at Titan ----
    vesta.send({"t": "move", "dir": "south"})
    vesta.wait_for(lambda m: m["t"] == "room" and m["room"]["id"] == "titan_base")
    vesta.send({"t": "talk"})
    chart = vesta.wait_for(lambda m: m["t"] == "chart", attempts=20)
    check("chart broker grants the Saturn system + Jupiter coords",
          chart is not None
          and all(e["permanent"] for e in chart["entries"])
          and any("jupiter" in e["label"].lower() for e in chart["entries"]))

    # jump mechanics off a jump point
    vesta.send({"t": "move", "dir": "north"})   # titan_orbit
    vesta.wait_for(lambda m: m["t"] == "room")
    vesta.send({"t": "jump", "destination": "jupiter"})
    toast = vesta.wait_for(lambda m: m["t"] == "toast", attempts=10)
    check("jump rejected away from a jump point",
          toast and "jump point" in toast["text"])

    # ---- the Space Tick, three moves out ----
    vesta.send({"t": "move", "dir": "east"})    # the drift
    vesta.wait_for(lambda m: m["t"] == "room")
    vesta.send({"t": "move", "dir": "east"})    # riddled asteroid
    room3 = vesta.wait_for(
        lambda m: m["t"] == "room" and m["room"]["id"] == "asteroid_tick")
    check("three moves out → the riddled asteroid", room3 is not None)

    vesta.send({"t": "attack", "target": "tick"})
    target = vesta.wait_for(lambda m: m["t"] == "target", attempts=20)
    check("attack spawns a server-driven target square", target is not None)

    # a wide shot is rejected by the server
    vesta.send({"t": "fire", "x": 0.0, "y": 0.0})
    wide = vesta.wait_for(lambda m: m["t"] == "toast" and "wide" in m["text"],
                          attempts=10)
    check("wide shot rejected by the server", wide is not None)

    # aimed shots until the tick dies (server judges x/y against its square).
    # Like the real client: after every hit the next target spawns right
    # away, so a fast clicker lands several shots per round.
    dead = None
    loot_event = None
    swings = 0
    for _ in range(80):
        engine.tick_once()
        while True:
            try:
                msg = vesta.recv(timeout=0.05)
            except TimeoutError:
                break
            if msg["t"] == "target":
                vesta.send({"t": "fire", "x": msg["x"] + msg["w"] / 2,
                            "y": msg["y"] + msg["w"] / 2})
            elif msg["t"] == "combat":
                for ev in msg["events"]:
                    if ev["kind"] == "swing" and ev["attacker"] == "Vesta":
                        swings += 1
                    if ev["kind"] == "die" and ev["who"] == "Space Tick":
                        dead = ev
            elif msg["t"] == "loot" and msg["room"] == "asteroid_tick":
                loot_event = msg
            if dead:
                break
        if dead:
            break
    check("Space Tick dies to aimed shots", dead is not None, f"swings={swings}")
    check("xp awarded for the kill", dead and dead["xp"] >= 35,
          str(dead and dead["xp"]))

    # salvage drops to the floor; taking it fills the racks
    check("bug dropped salvage on the floor",
          loot_event is not None and len(loot_event["loot"]) > 0,
          str(loot_event))
    item = loot_event["loot"][0]["item"]
    vesta.send({"t": "take", "item": item})
    taken = vesta.wait_for(lambda m: m["t"] == "you", attempts=10)
    check("salvage taken", taken is not None)

    # ---- affiliates: follow, accept, whisper, no friendly fire ----
    status, d2 = http("POST", "/api/register", {"username": "deanna", "password": "hunter22"})
    token2 = d2["token"]
    http("POST", "/api/character", {"name": "Deanna", "cls": "bug", "type": "fighter"},
         {"X-Token": token2})
    deanna = Game(token2)
    deanna.wait_for(lambda m: m["t"] == "welcome")

    for d in ("west", "west", "south"):     # back to the Titan docks
        vesta.send({"t": "move", "dir": d})
        vesta.wait_for(lambda m: m["t"] == "room")
    vesta.send({"t": "follow", "target": "Deanna"})
    req = deanna.wait_for(lambda m: m["t"] == "follow_request", attempts=20)
    check("follow request delivered", req is not None and req["from"] == "Vesta")
    deanna.send({"t": "accept", "from": "Vesta"})
    ack = vesta.wait_for(lambda m: m["t"] == "toast" and "affiliated" in m["text"],
                         attempts=20)
    check("2-way affiliation confirmed", ack is not None)

    vesta.send({"t": "whisper", "target": "Deanna", "text": "stay sharp"})
    heard = deanna.wait_for(lambda m: m["t"] == "whisper", attempts=20)
    check("affiliate whisper across rooms", heard and heard["text"] == "stay sharp")

    vesta.send({"t": "attack", "target": "Deanna"})
    denied = vesta.wait_for(
        lambda m: m["t"] == "toast" and "affiliate" in m["text"], attempts=10)
    check("affiliates cannot attack each other", denied is not None)

    # ---- evasion (forced-success stub) — from the riddled asteroid ----
    engine.rng = AlwaysFirst()
    for sp in engine.SPAWNS:            # respawn the tick instantly
        if sp["mob"] == "space-tick":
            sp["inst"] = None
            sp["next"] = 0.0
    engine.tick_once()                  # the respawn actually spawns it
    for d in ("north", "east", "east"):  # back out to the asteroid
        vesta.send({"t": "move", "dir": d})
        vesta.wait_for(lambda m: m["t"] == "room"
                       and m["room"]["id"] == "asteroid_tick")
    vesta.send({"t": "attack", "target": "tick"})
    vesta.wait_for(lambda m: m["t"] == "target", attempts=10)
    vesta.send({"t": "evade"})
    evaded = vesta.wait_for(lambda m: m["t"] == "toast" and "evade" in m["text"],
                            attempts=10)
    check("evasion burn (agility roll)",
          evaded is not None and "Burn" in evaded["text"])

    # ---- death: strips the ship, respawns at Titan ----
    vesta.send({"t": "move", "dir": "east"})
    vesta.wait_for(lambda m: m["t"] == "room"
                   and m["room"]["id"] == "asteroid_tick")
    vesta.send({"t": "attack", "target": "tick"})
    vesta.wait_for(lambda m: m["t"] == "target", attempts=10)
    for actor in engine.PLAYERS.values():
        if actor["name"] == "Vesta":
            actor["hp"] = 1
    vesta.send({"t": "attack", "target": ""})   # break off; tick still aggros
    for _ in range(6):
        engine.tick_once()
    died = vesta.wait_for(
        lambda m: m["t"] == "you" and m["you"]["room"] == "titan_base",
        attempts=40)
    check("death respawns at Titan, ship stripped",
          died is not None and died["you"]["guns"] == []
          and died["you"]["plates"] == []
          and died["you"]["hp"] == died["you"]["max_hp"])

    vesta.close()
    deanna.close()


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
