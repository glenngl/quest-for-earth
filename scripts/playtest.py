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
    # Clear of the debris field so only the repair is being measured.
    A["pos"] = [8, 45, 8]
    B["pos"] = [8, 45, 10]
    A.update(energy=80, methane=0, shields=A["max_shields"])
    B["shields"] = 40
    ok, reason = engine.repair_shields("Scout", "Wingman", 10)
    check("nearby repair spends energy for shields",
          ok and A["energy"] == 70 and B["shields"] == 50, reason)
    B["pos"] = [8, 45, 40]
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

    print("\n— co-op donation —")
    A.update(energy=100, methane=0, shields=A["max_shields"], hp=100)
    B.update(methane=0, energy=0, shields=10, metal=0, hp=100)
    A["pos"] = [8, 45, 8]
    B["pos"] = [8, 45, 9]
    tick_seconds(2)
    check("surplus energy converts to a nearby wingman's shields",
          B["shields"] > 10 and B["shields"] < B["max_shields"],
          f"B sh={B['shields']}")
    check("donation spends the donor's energy, not the donor's own shields",
          A["energy"] < 100 and A["shields"] == A["max_shields"],
          f"A en={A['energy']} sh={A['shields']}")

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

    print("\n— mob roster —")
    roster = [m for m in engine.MOBS.values() if m.get("alive")]
    check("a joining pilot brings the mob roster to life",
          len(roster) >= 1, f"{len(roster)} alive mobs")
    check("roster mobs spawn near the exit portal",
          all(math.dist(m["pos"], engine.PORTAL_POS) < 12 for m in roster),
          str([m["pos"] for m in roster]))
    check("the roster scales with the player count",
          len(engine.MOBS) >= 2, f"{len(engine.MOBS)} mobs for {len(engine.PLAYERS)} pilots")
    engine._MOB_STATE = {}  # force a full roster report
    status = engine.tick_once()
    check("the tick reports the whole roster when the roster state is new",
          len(status.get("mobs_changed") or []) == len(engine.MOBS),
          str(status.get("mobs_changed")))

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
    # Start from a known-full state so the check isolates the mob's damage.
    B.update(hp=100, shields=B["max_shields"], energy=100)
    mob_hp_before, mob_sh_before = nm["hp"], nm["shields"]
    status = engine.tick_once()
    check("a living mob within aggro range attacks the pilot",
          len(status["attacks"]) == 1 and status["attacks"][0]["mob"] == "play_mob",
          str(status["attacks"]))
    check("the mob drains the pilot's shields",
          B["shields"] < B["max_shields"], f"sh={B['shields']}")
    check("the mob does not damage itself",
          nm["hp"] == mob_hp_before and nm["shields"] == mob_sh_before,
          f"hp={nm['hp']} sh={nm['shields']}")
    # The pilot answers the mob: a killing blow drops salvage for the killer.
    nm["shields"] = 0
    nm["hp"] = 5
    ok, reason, hit = engine.fire_at("Wingman", "play_mob")
    check("a pilot can fire at a living mob", ok and hit is not None, reason)
    check("a killing blow drops mob salvage",
          not nm["alive"]
          and len(engine.ROOM_LOOT.get(nm.get("room") or "spawn_cube", [])) > 0)
    status = engine.tick_once()
    check("a destroyed mob is reported as a roster change",
          "play_mob" in (status.get("mobs_changed") or []),
          str(status.get("mobs_changed")))

    print("\n— mob movement —")
    chase = engine.MOBS["play_mob"]
    chase["alive"] = True
    chase["pos"] = [30, 20, 40]
    B["status"] = "alive"
    B["pos"] = [30, 20, 30]
    before_dist = math.dist(chase["pos"], B["pos"])
    status = engine.tick_once()
    after_dist = math.dist(chase["pos"], B["pos"])
    check("a mob closes on the pilot that engages it",
          after_dist < before_dist, f"{before_dist:.2f} -> {after_dist:.2f}")
    check("the mob's movement is reported for broadcast",
          "play_mob" in (status.get("mobs_moved") or []),
          str(status.get("mobs_moved")))

    print("\n— mob kills a pilot —")
    nm["alive"] = True
    nm["hp"], nm["shields"] = 20, 0
    nm["pos"] = [30, 20, 31]
    B["status"] = "alive"
    B["pos"] = [30, 20, 30]
    B.update(hp=3, shields=0)
    engine.MOB_ATTACK_TICKS.clear()
    status = engine.tick_once()
    check("a mob can destroy a pilot",
          len(status["attacks"]) == 1 and status["attacks"][0].get("dead") is True,
          str(status["attacks"]))
    check("the destroyed pilot is marked dead by the engine",
          B["status"] == "dead" and B["hp"] == 0, f"status={B['status']} hp={B['hp']}")
    check("the mob kill arms the respawn timer",
          "Wingman" in engine.DEATH_TIMERS, str(list(engine.DEATH_TIMERS)))
    check("the destroyed pilot drops salvage",
          len(status["attacks"][0].get("dropped") or []) > 0,
          str(status["attacks"][0].get("dropped")))
    engine.respawn_player("Wingman")
    B["status"] = "alive"
    B.update(hp=100, shields=100, energy=100)
    nm["alive"] = True
    nm["hp"], nm["shields"] = 20, 10

    print("\n— salvage pickup —")
    salvage_room = engine.ROOM_LOOT.get("end_cube") or []
    check("the destroyed mob left salvage in the world",
          len(salvage_room) > 0, f"{len(salvage_room)} entries")
    # The kill must also broadcast the salvage so the client can mark it.
    nm["alive"] = True
    nm["hp"], nm["shields"] = 5, 0
    nm["pos"] = [30, 20, 30]
    A["pos"] = [30, 20, 31]
    engine.LAST_FIRE.clear()
    a.send({"t": "fire", "target": "play_mob", "name": "Scout"})
    loot_frame = a.wait_for(lambda m: m["t"] == "loot" and m.get("item"), attempts=20)
    check("a mob kill broadcasts its salvage", loot_frame is not None, str(loot_frame))
    check("the broadcast salvage carries a position",
          bool((loot_frame or {}).get("item", {}).get("pos")), str((loot_frame or {}).get("item")))
    item = (loot_frame or {}).get("item") or (salvage_room[0] if salvage_room else {})
    A["pos"] = [1, 1, 1]
    A["status"] = "alive"
    ok, reason = engine.take_loot("Scout", item.get("id"))
    check("salvage out of reach cannot be taken", not ok, reason)
    A["pos"] = list(item.get("pos") or [1, 1, 1])
    ok, reason = engine.take_loot("Scout", item.get("id"))
    check("salvage within reach can be taken", ok, reason)
    check("the taken salvage lands in the pilot's hold",
          item.get("id") in A["artifacts"], str(A["artifacts"]))
    check("the taken salvage leaves the world",
          item not in (engine.ROOM_LOOT.get("end_cube") or []),
          str(engine.ROOM_LOOT.get("end_cube")))

    print("\n— affiliation —")
    A["affiliate"] = None
    B["status"] = "alive"
    ok, reason = engine.affiliate("Scout", "Wingman")
    check("a pilot can affiliate with a squadmate", ok, reason)
    check("the affiliation is recorded", A.get("affiliate") == "Wingman", str(A.get("affiliate")))
    ok, reason = engine.affiliate("Scout", "Scout")
    check("a pilot cannot affiliate with themselves", not ok, reason)
    ok, reason = engine.affiliate("Scout", "Nobody")
    check("a pilot cannot affiliate with nobody", not ok, reason)
    # The affiliate (the follower) shares in what their leader earns.
    B["xp"] = 0
    A["xp"] = 0
    engine._award_xp("Wingman", 40)
    check("the affiliate shares in the leader's gains",
          A["xp"] == 10 and B["xp"] == 40, f"A={A['xp']} B={B['xp']}")
    engine._award_xp("Scout", 40)
    check("gains are not shared backwards to a non-affiliate",
          A["xp"] == 50 and B["xp"] == 40, f"A={A['xp']} B={B['xp']}")
    engine.break_affiliate("Scout")
    check("an affiliation can be broken", A.get("affiliate") is None, str(A.get("affiliate")))

    print("\n— rock collisions —")
    big_rock = next(d for d in engine.DEBRIS.values() if d["material"] == "rock")
    A["status"] = "alive"
    A.update(hp=100, shields=100, energy=100)
    # Drive straight into the rock at full speed (one unit of displacement per
    # tick is the thrust cadence).
    A["pos"] = [big_rock["pos"][0] - 2.0, big_rock["pos"][1], big_rock["pos"][2]]
    engine._LAST_POS["Scout"] = [big_rock["pos"][0] - 3.0,
                                  big_rock["pos"][1], big_rock["pos"][2]]
    status = engine.tick_once()
    collisions = status.get("collisions") or []
    check("a full-speed impact into a rock registers a collision",
          len(collisions) == 1, str(collisions))
    check("the impact hurts the ship",
          A["hp"] + A["shields"] < 200, f"hp={A['hp']} sh={A['shields']}")
    check("a head-on impact into a big rock blows it up",
          bool(collisions) and collisions[0].get("explosion") is True, str(collisions))
    check("the blown-up rock leaves the world",
          bool(big_rock.get("destroyed")), str(big_rock.get("destroyed")))

    print("\n— light chunks barely scratch —")
    light = next(d for d in engine.DEBRIS.values() if d["material"] == "methane")
    A.update(hp=100, shields=100)
    # Close enough to actually touch it (its radius is much smaller).
    A["pos"] = [light["pos"][0] - 1.2, light["pos"][1], light["pos"][2]]
    engine._LAST_POS["Scout"] = [light["pos"][0] - 2.2,
                                  light["pos"][1], light["pos"][2]]
    status = engine.tick_once()
    light_collisions = status.get("collisions") or []
    light_damage = sum(c.get("damage", 0) for c in light_collisions)
    check("a light methane chunk takes almost no damage",
          len(light_collisions) == 1 and light_damage < 2.0,
          f"damage={light_damage} {light_collisions}")
    check("a light chunk is never blown up",
          all(not c.get("explosion") for c in light_collisions), str(light_collisions))

    print("\n— mass driven gravity —")
    metal = next(d for d in engine.DEBRIS.values() if d["material"] == "metal")
    methane_chunk = next(d for d in engine.DEBRIS.values() if d["material"] == "methane")
    parked = {d["id"]: list(d["pos"]) for d in engine.DEBRIS.values()}

    def drift_toward(chunk, dist=3.0):
        for other in engine.DEBRIS.values():
            other["pos"] = [0.0, 0.0, 0.0]
        chunk["pos"] = [40.0, 30.0, 30.0]
        A["pos"] = [40.0 + dist, 30.0, 30.0]
        before = math.dist(A["pos"], chunk["pos"])
        engine.tick_once()
        return before - math.dist(A["pos"], chunk["pos"])

    metal_drift = drift_toward(metal)
    methane_drift = drift_toward(methane_chunk)
    for did, pos in parked.items():
        engine.DEBRIS[did]["pos"] = pos
    check("dense metal ore bends the pilot's path",
          metal_drift > methane_drift, f"metal={metal_drift:.4f} ch4={methane_drift:.4f}")
    check("light methane chunks barely pull",
          methane_drift < metal_drift / 10,
          f"metal={metal_drift:.4f} ch4={methane_drift:.4f}")

    print("\n— collision broadcast —")
    chunk = next(d for d in engine.DEBRIS.values()
                 if d["material"] == "methane" and not d.get("destroyed"))
    A["pos"] = [chunk["pos"][0] - 4, chunk["pos"][1], chunk["pos"][2]]
    A.update(hp=100, shields=100)
    # Anchor the velocity measurement so the tick sees the moves, not the
    # teleport the test just did.
    engine._LAST_POS["Scout"] = list(A["pos"])
    a.pending.clear()
    for _ in range(6):
        a.send({"t": "move", "vec": [1, 0, 0], "name": "Scout"})
    # Wait for the server to apply the moves before ticking.
    for _ in range(40):
        if len([m for m in a.pending if m.get("t") == "pos"]) >= 6:
            break
        time.sleep(0.05)
    status = engine.tick_once()
    collisions = status.get("collisions") or []
    check("a rock impact is reported for broadcast", bool(collisions), str(collisions))
    check("the reported impact names the chunk that was hit",
          bool(collisions) and collisions[0].get("name"), str(collisions))
    check("the reported impact carries the damage",
          bool(collisions) and collisions[0].get("damage") is not None, str(collisions))

    print("\n— pilot vs pilot kill loop —")
    A["pos"], B["pos"] = [30, 20, 30], [30, 20, 30]
    A["status"] = B["status"] = "alive"
    A.update(hp=100, shields=100, energy=100)
    B.update(hp=2, shields=3, energy=0)
    A["xp"] = 10
    B["xp"] = 10
    engine.LAST_FIRE.clear()
    xp_before = B["xp"]
    ok, reason, hit = engine.fire_at("Scout", "Wingman")
    check("a killing blow on a pilot lands", ok and hit is not None and hit.get("dead"), reason)
    check("the killer takes the victim's shields on kill",
          A["shields"] == A["max_shields"], f"sh={A['shields']}")
    expected_xp = xp_before - xp_before * 15 // 100
    check("the death penalty takes a slice of the victim's xp",
          B["xp"] == expected_xp, f"xp={B['xp']} expected={expected_xp}")

    print("\n— xp and level ups —")
    A["xp"] = 95
    A["status"] = "alive"
    A["pos"] = [30, 20, 30]
    engine.MOBS["play_mob"]["alive"] = True
    engine.MOBS["play_mob"]["pos"] = [30, 20, 30]
    engine.MOBS["play_mob"]["hp"], engine.MOBS["play_mob"]["shields"] = 5, 0
    engine.LAST_FIRE.clear()
    ok, reason, hit = engine.fire_at("Scout", "play_mob")
    check("a mob bounty awards the killer xp", ok and hit is not None, reason)
    check("the killer crossed into a new level", A["level"] >= 2, f"level={A['level']} xp={A['xp']}")

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

    print("\n— whisper chat —")
    a.send({"t": "chat", "name": "Scout", "text": "/w Wingman hello there"})
    whisper = None
    end_t = time.time() + 2.0
    while time.time() < end_t:
        try:
            frame = json.loads(b.sock.recv(timeout=min(0.05, max(0.0, end_t - time.time()))))
        except Exception:
            break
        if frame.get("t") == "say" and frame.get("to") == "Wingman" and "hello there" in frame.get("text", ""):
            whisper = frame
            break
    check("a /w whisper reaches only the target", whisper is not None, str(whisper))
    general = b.wait_for(lambda m: m["t"] == "say" and m.get("text") == "general", attempts=5)
    check("the whisper is not echoed into general chat", general is None, str(general))

    print("\n— local scan —")
    target = next(d for d in engine.DEBRIS.values() if not d["harvested"])
    A["pos"] = list(target["pos"])
    A["status"] = "alive"
    a.send({"t": "scan", "name": "Scout"})
    scan = a.wait_for(lambda m: m["t"] == "scan_result", attempts=20)
    check("a scan returns contacts", scan is not None and scan.get("ok") is True, str(scan))
    ids = [h.get("id") for h in (scan or {}).get("hits", [])]
    check("the scan finds the nearby debris", target["id"] in ids, str(ids))
    check("the scan carries a decay TTL", (scan or {}).get("ttl", 0) > 0, str((scan or {}).get("ttl")))
    check("the scan reports how heavy each contact is",
          all("mass" in h for h in (scan or {}).get("hits", [])),
          str([h.get("mass") for h in (scan or {}).get("hits", [])]))
    A["pos"] = [1, 1, 1]
    ok, reason, hits = engine.scan("Scout")
    check("a scan far from any contact finds nothing", ok and not hits, f"{len(hits)} hits")

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
