"""Gameplay playtest for the Level 1 WebGL game.

Unlike ``smoke.py`` (which proves the surface works), this script exercises
*gameplay quality*: resource pacing, store caps, nearby co-op repair, thrust
vectors, rock gravity, death rules and a dive into the wormhole from above.
It drives the real FastAPI app and engine through real handler code with an
isolated world state, and prints a short report of checks and timings.

Run:  venv/bin/python scripts/playtest.py
"""

import json
import math
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import urllib.request

from websockets.sync.client import connect as ws_connect

_TMP = tempfile.mkdtemp(prefix="qfe-playtest-")
os.environ["QFE_DB"] = os.path.join(_TMP, "playtest.sqlite3")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from quest_for_earth.engine import engine  # noqa: E402
from quest_for_earth.web import app  # noqa: E402

PORT = 8403
BASE = f"http://127.0.0.1:{PORT}"
FAILURES = []
NOTES = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def note(text):
    NOTES.append(text)
    print(f"  [note] {text}")


class Pilot:
    def __init__(self, name):
        self.name = name
        self.sock = ws_connect(f"ws://127.0.0.1:{PORT}/ws", open_timeout=5, legacy=True)
        self.pending = []
        self.send({"t": "set_name", "name": name})

    def send(self, obj):
        self.sock.send(json.dumps(obj))

    def wait_for(self, pred, attempts=100, timeout=0.05):
        for _ in range(attempts):
            for i, msg in enumerate(self.pending):
                if pred(msg):
                    return self.pending.pop(i)
            try:
                self.pending.append(json.loads(self.sock.recv(timeout=timeout)))
            except TimeoutError:
                continue
        return None

    def close(self):
        self.sock.close()


def start_server():
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("playtest server did not start")


def tick_seconds(seconds):
    for _ in range(int(seconds * 1000 / engine.tick_ms)):
        engine.tick_once()


def run():
    start_server()
    engine.stop_tick()  # deterministic, explicit ticks
    engine.rng = random.Random(99)
    a, b = Pilot("Scout"), Pilot("Wingman")
    for p in (a, b):
        p.wait_for(lambda m: m["t"] == "welcome", attempts=20)
    A, B = engine.PLAYERS["Scout"], engine.PLAYERS["Wingman"]

    print("\n— resource pacing —")
    A.update(methane=5, energy=50, shields=50, metal=5, hp=50)
    # Keep the wingman's energy at zero so it cannot donate surplus energy to
    # the scout's shields during this pacing test.
    B.update(methane=0, energy=0, shields=100, metal=0, hp=100)
    tick_seconds(5)
    check("methane -> energy -> shields is 1/sec",
          A["methane"] == 0 and A["energy"] == 50 and A["shields"] == 55,
          f"ch4={A['methane']} en={A['energy']} sh={A['shields']}")
    check("metal -> HP is 1/sec",
          A["metal"] == 0 and A["hp"] == 55, f"metal={A['metal']} hp={A['hp']}")

    print("\n— tank caps and harvest —")
    methane = next(d for d in engine.DEBRIS.values() if d["material"] == "methane")
    A["pos"] = list(methane["pos"])
    A["methane"] = A["max_methane"]
    ok, reason = engine.harvest_debris("Scout", methane["id"])
    check("full methane tank refuses harvest and keeps the chunk",
          not ok and not methane["harvested"], reason)
    rock = next(d for d in engine.DEBRIS.values() if d["material"] == "rock")
    A["pos"] = list(rock["pos"])
    ok, reason = engine.harvest_debris("Scout", rock["id"])
    check("rock is never harvestable", not ok and not rock["harvested"], reason)

    print("\n— co-op repair —")
    A["pos"] = [30, 20, 30]
    B["pos"] = [30, 20, 32]
    A.update(energy=80, methane=0, shields=A["max_shields"])
    B["shields"] = 40
    ok, reason = engine.repair_shields("Scout", "Wingman", 10)
    check("nearby repair spends energy for shields",
          ok and A["energy"] == 70 and B["shields"] == 50, reason)
    B["pos"] = [30, 20, 50]
    ok, reason = engine.repair_shields("Scout", "Wingman", 1)
    check("repair refuses an out-of-range wingman", not ok, reason)

    print("\n— firing on the other pilot —")
    A["pos"], B["pos"] = [30, 20, 30], [30, 20, 32]
    A["status"] = B["status"] = "alive"
    B.update(shields=50, hp=100)
    engine.LAST_FIRE.clear()
    a.send({"t": "fire", "target": "Wingman", "name": "Scout"})
    shot = a.wait_for(lambda m: m["t"] == "combat" and m["kind"] == "shot", attempts=20)
    check("a shot produces an authoritative hit event",
          shot is not None and shot["target"] == "Wingman")
    check("shields take the damage first",
          B["shields"] == 44 and B["hp"] == 100, f"sh={B['shields']} hp={B['hp']}")
    a.send({"t": "fire", "target": "Wingman", "name": "Scout"})
    cycling = a.wait_for(lambda m: m["t"] == "toast" and "cycling" in m["text"], attempts=20)
    check("cooldown blocks an immediate second shot", cycling is not None)
    B["pos"] = [30, 20, 60]
    engine.LAST_FIRE.clear()
    a.send({"t": "fire", "target": "Wingman", "name": "Scout"})
    too_far = a.wait_for(lambda m: m["t"] == "toast" and "out of range" in m["text"], attempts=20)
    check("range is enforced server-side", too_far is not None)

    print("\n— thrust vectors —")
    A["pos"] = [20, 40, 20]
    a.send({"t": "move", "vec": [0, -1, 0], "name": "Scout"})
    moved = a.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Scout"
                       and m["pos"][1] < 39.9, attempts=20)
    check("downward thrust changes Y, not Z",
          moved is not None and abs(A["pos"][2] - 20) < 0.01, str(A["pos"]))
    A["pos"] = [63, 63, 63]
    a.send({"t": "move", "vec": [1, 1, 1], "name": "Scout"})
    a.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Scout", attempts=20)
    check("thrust clamps at the cube boundary", A["pos"] == [63, 63, 63], str(A["pos"]))

    print("\n— gravity —")
    rp = rock["pos"]
    A["pos"] = [rp[0] + 3, rp[1], rp[2]]
    before = list(A["pos"])
    status = engine.tick_once()
    drift = math.dist(A["pos"], rp)
    check("rock pulls a passing pilot toward it",
          drift < math.dist(before, rp), f"{before} -> {A['pos']}")
    note(f"one-tick drift {math.dist(A['pos'], before):.3f} units at distance 3")

    print("\n— death rules —")
    engine.cause_death("Scout")
    dead_pos = list(A["pos"])
    a.send({"t": "move", "dir": "north", "name": "Scout"})
    idle = a.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Scout", attempts=8)
    check("a downed pilot cannot fly", idle is None
          and A["pos"] == dead_pos, str(A["pos"]))

    print("\n— portal approach from above —")
    engine.respawn_player("Scout")
    pp = engine.PORTAL_POS
    A["pos"] = [pp[0], pp[1] + 14, pp[2] - 3]
    dive = 0
    while A["pos"][1] > pp[1] + 2 and dive < 40:
        a.send({"t": "move", "vec": [0, -1, 0], "name": "Scout"})
        a.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Scout"
                   and m["pos"][1] < A["pos"][1], attempts=20)
        dive += 1
    ok, reason = engine.can_enter_portal("Scout")
    check("a vertical dive can line up with the wormhole aperture", ok, reason)
    note(f"dive took {dive} thrust steps")

    print("\n— npc combat —")
    engine.add_mob("play_mob")
    nm = engine.MOBS["play_mob"]
    nm["hp"], nm["shields"] = 20, 10
    nm["alive"] = True
    engine.MOB_ATTACK_TICKS.clear()
    engine.LAST_FIRE.clear()
    B["status"] = "alive"
    B["pos"] = [30, 20, 30]
    A["status"] = "alive"
    A["pos"] = [30, 60, 30]
    nm["pos"] = [30, 20, 31]
    status = engine.tick_once()
    check("a living mob within aggro range attacks the pilot",
          len(status["attacks"]) == 1 and status["attacks"][0]["mob"] == "play_mob",
          str(status["attacks"]))
    check("the mob drains the pilot's shields", B["shields"] < B["max_shields"],
          f"sh={B['shields']}")
    # The pilot answers the mob: a killing blow drops salvage for the killer.
    nm["shields"] = 0
    nm["hp"] = 5
    ok, reason, hit = engine.fire_at("Wingman", "play_mob")
    check("a pilot can fire at a living mob", ok and hit is not None, reason)
    check("a killing blow drops mob salvage",
          not nm["alive"]
          and len(engine.ROOM_LOOT.get(nm.get("room") or "spawn_cube", [])) > 0)

    print("\n— pilot vs pilot kill loop —")
    A["pos"], B["pos"] = [30, 20, 30], [30, 20, 30]
    A["status"] = B["status"] = "alive"
    A.update(hp=100, shields=100, energy=100)
    B.update(hp=2, shields=3, energy=0)
    A["xp"] = 10
    engine.LAST_FIRE.clear()
    ok, reason, hit = engine.fire_at("Scout", "Wingman")
    check("a killing blow on a pilot lands", ok and hit is not None and hit.get("dead"), reason)
    check("the killer takes the victim's shields on kill",
          A["shields"] == A["max_shields"], f"sh={A['shields']}")
    check("the death penalty takes a slice of the victim's xp",
          B["xp"] == 9, f"xp={B['xp']}")

    print("\n— evade —")
    A["pos"] = [30, 20, 30]
    B["pos"] = [30, 20, 32]
    A["status"] = B["status"] = "alive"
    ok, reason = engine.evade("Scout")
    check("evade bursts away from a nearby threat", ok, reason)
    check("evade moved the pilot away from the threat",
          math.dist(A["pos"], B["pos"]) > math.dist([30, 20, 30], [30, 20, 32]) - 0.001,
          f"{A['pos']}")
    ok2, reason2 = engine.evade("Scout")
    check("evade respects its cooldown", not ok2, reason2)

    a.close()
    b.close()


def main():
    print(f"playtest: isolated world in {_TMP}")
    start = time.time()
    try:
        run()
    finally:
        engine.stop_tick()
    if FAILURES:
        print(f"\nplaytest FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        sys.exit(1)
    print(f"\nplaytest PASSED in {time.time() - start:.1f}s — "
          f"{len(NOTES)} gameplay notes recorded")


if __name__ == "__main__":
    main()
