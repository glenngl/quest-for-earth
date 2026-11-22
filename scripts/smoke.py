"""Offline smoke test for the Level 1 WebGL client and its server.

Starts the real FastAPI app on a throwaway port and drives every endpoint
and the live game loop through real handler code:

  HTTP: / , /health , /api/ships , /api/lighting (incl. HTML injection)
  WS:   welcome (incl. ship stores), move (authoritative broadcast), chat,
        harvest (rock/methane/metal + caps), repair, fire, take,
        enter_portal (all three outcomes), die/respawn, shield regen;
  D25:  per-second methane/metal conversions, nearby repairs, rock gravity

World state is isolated before import; the engine is seeded so ticks are
deterministic. No auth API exists in Level 1 — that is the retired client.
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

# Isolate the world state BEFORE any import: throwaway DB, no env.
_TMP = tempfile.mkdtemp(prefix="qfe-l1smoke-")
os.environ["QFE_DB"] = os.path.join(_TMP, "smoke_l1.sqlite3")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from quest_for_earth import engine as engine_mod  # noqa: E402
from quest_for_earth import world as world_mod  # noqa: E402
from quest_for_earth.engine import engine  # noqa: E402
from quest_for_earth.web import app  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

PORT = 8402
BASE = f"http://127.0.0.1:{PORT}"
FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class Game:
    """A WebSocket pilot session. Messages already read are kept in `pending`."""

    def __init__(self, name):
        self.name = name
        self.sock = ws_connect(f"ws://127.0.0.1:{PORT}/ws", open_timeout=5, legacy=True)
        self.pending = []
        self.send({"t": "set_name", "name": name})

    def send(self, obj):
        self.sock.send(json.dumps(obj))

    def wait_for(self, pred, attempts=100, timeout=0.1):
        for _ in range(attempts):
            for i, msg in enumerate(self.pending):
                if pred(msg):
                    return self.pending.pop(i)
            try:
                msg = json.loads(self.sock.recv(timeout=timeout))
                self.pending.append(msg)
            except TimeoutError:
                continue
        return None

    def close(self):
        self.sock.close()


def http_json(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return r.status, json.loads(r.read())


def start_server():
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("smoke server did not start")


def near(a, b, tol=1.5):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def run_checks():
    # ── HTTP endpoints ──────────────────────────────────────────────────
    with urllib.request.urlopen(f"{BASE}/", timeout=5) as r:
        html = r.read().decode()
    check("GET / serves the game UI", r.status == 200 and "Quest for Earth" in html)

    status, health = http_json("/health")
    check("GET /health", status == 200 and health["ok"] is True)

    check("GET / injects lighting config from data/lighting.json",
          "window.QFE_LIGHTING" in html and "light_direction" in html)

    status, ships = http_json("/api/ships")
    stats = ships["classes"]["fighter"]["stats"]
    check("GET /api/ships (1 class, 1 type)",
          status == 200 and len(ships["classes"]) == 1 and len(ships["types"]) == 1)
    check("fighter: agi 5, rep 2, nrg 9",
          stats["agi"] == 5 and stats["rep"] == 2 and stats["nrg"] == 9)

    # ── D1: the renderer's light model is data, not baked into the client ─
    status, lighting = http_json("/api/lighting")
    check("GET /api/lighting", status == 200 and "light_direction" in lighting)
    check("lighting.json values are live data",
          len(lighting["light_direction"]) == 3
          and 0.0 < lighting["ambient"] <= 1.0
          and lighting["shininess"] > 1.0, str(lighting))

    # ── data-driven layout (D1) ─────────────────────────────────────────
    spawn, portal = engine.SPAWN_POS, engine.PORTAL_POS
    check("spawn is at the near end of the cube", spawn[2] < 16, str(spawn))
    check("portal is at the far end of the cube", portal[2] > 48, str(portal))
    check("portal front faces the spawn side", engine.PORTAL_FRONT == "-z")
    check("balance.json is live data", engine_mod.get_balance().get("tick_ms") == 100)
    balance_data = engine_mod.get_balance()
    check("D25 resource constants are live data",
          balance_data.get("energy_max") == 100
          and balance_data.get("methane_tank_max") == 100
          and balance_data.get("metal_tank_max") == 100
          and balance_data.get("nearby_radius") == 5)
    check("balance carries the hull curve", balance_data.get("hull", {}).get("base") == 100)
    check("balance carries the xp curve", balance_data.get("xp", {}).get("per_level") == 100)
    check("max hull grows per level", world_mod.max_hull(2) == 125)
    check("xp thresholds are data-driven", world_mod.xp_next(2) == 200)

    # ── two pilots connect ──────────────────────────────────────────────
    engine.stop_tick()
    engine.rng = random.Random(42)
    vesta, deanna = Game("Vesta"), Game("Deanna")
    start_pos = {}
    debris_field = []
    for pilot in (vesta, deanna):
        welcome = pilot.wait_for(lambda m: m["t"] == "welcome", attempts=20)
        start_pos[pilot.name] = welcome["you"]["pos"] if welcome else list(spawn)
        if pilot.name == "Vesta":
            debris_field = welcome.get("debris", [])
        check(f"WS welcome: {pilot.name}",
              welcome and welcome["you"]["name"] == pilot.name
              and welcome["room"]["id"] == engine.ROOM_ID)
        check(f"WS welcome carries portal for {pilot.name}",
              welcome and welcome["portal"]["pos"] == list(portal)
              and welcome["portal"]["front"] == "-z")
        check(f"WS welcome carries a wormhole exit at spawn for {pilot.name}",
              welcome and welcome["spawn_portal"]["pos"] == list(spawn)
              and welcome["spawn_portal"]["front"] == "+z")
        check(f"{pilot.name} spawns at the data spawn",
              welcome and near(welcome["you"]["pos"], spawn, 3))
        check(f"WS welcome carries ship stores for {pilot.name}",
              welcome and all(k in welcome["you"] for k in
                  ("energy", "methane", "metal", "max_energy",
                   "max_methane", "max_metal")))
        check(f"WS welcome carries the respawn delay for {pilot.name}",
              welcome and welcome.get("respawn_ms", 0) > 0,
              str(welcome.get("respawn_ms") if welcome else None))
        check(f"WS welcome lists the squad roster for {pilot.name}",
              welcome and isinstance(welcome.get("pilots"), list),
              str(welcome.get("pilots") if welcome else None))

    # ── authoritative movement broadcast to every pilot (D2) ───────────
    vesta.send({"t": "move", "dir": "north"})
    mine = vesta.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Vesta", attempts=20)
    theirs = deanna.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Vesta", attempts=20)
    check("move north -> authoritative pos to mover",
          mine and mine["pos"][2] == start_pos["Vesta"][2] + 1)
    check("pos is broadcast to the other pilot", theirs and theirs["pos"] == mine["pos"])

    vesta.send({"t": "move", "dir": "east"})
    east = vesta.wait_for(lambda m: m["t"] == "pos" and m["from"] == "Vesta"
                          and m["pos"][0] == mine["pos"][0] + 1, attempts=20)
    check("move east increments x", east is not None)

    # ── thrust vectors: look down + thrust descends (D20 amendment) ─────
    v = engine.PLAYERS["Vesta"]
    v["pos"] = [32.0, 40.0, 32.0]
    vesta.send({"t": "move", "vec": [0, -1, 0], "name": "Vesta"})
    dive = vesta.wait_for(
        lambda m: m["t"] == "pos" and m["from"] == "Vesta"
        and m["pos"][1] < 39.9, attempts=20)
    check("thrust vector descends in Y (not Z)", dive is not None
          and abs(v["pos"][2] - 32.0) < 0.01, str(v["pos"]))
    vesta.send({"t": "move", "vec": [0.6, -0.8, 0], "name": "Vesta"})
    diagonal = vesta.wait_for(
        lambda m: m["t"] == "pos" and m["from"] == "Vesta"
        and m["pos"][0] > 32.1 and m["pos"][1] < 39.3, attempts=20)
    check("diagonal thrust follows the 3-D vector", diagonal is not None,
          str(v["pos"]))

    # ── chat is room-scoped and relayed ─────────────────────────────────
    vesta.send({"t": "chat", "text": "on my way to the wormhole"})
    said = deanna.wait_for(lambda m: m["t"] == "say" and m["from"] == "Vesta", attempts=20)
    check("chat relayed to other pilot", said and said["text"] == "on my way to the wormhole")

    # ── debris field & harvesting (D25: rock / methane / metal) ────────
    check("welcome carries the data-defined debris field", len(debris_field) == 11,
          f"count={len(debris_field)}")
    counts = {}
    for chunk in debris_field:
        counts[chunk["material"]] = counts.get(chunk["material"], 0) + 1
    check("debris counts come from world.json",
          counts == {"rock": 4, "methane": 4, "metal": 3}, str(counts))
    check("rocks are uniformly sized",
          all(d["size"] == 3 for d in debris_field if d["material"] == "rock"))
    check("debris lies between spawn and portal",
          all(min(spawn[2], portal[2]) <= d["pos"][2] <= max(spawn[2], portal[2])
              for d in debris_field))
    types = {d["type"] for d in debris_field}
    check("debris has space-appropriate types",
          {"asteroid", "ice_chunk", "scrap_metal"} <= types, str(types))
    materials = {d["material"] for d in debris_field}
    check("debris carries the three resource materials",
          {"rock", "methane", "metal"} <= materials, str(materials))
    check("rocks have a gravity size",
          all(1 <= d["size"] <= 3 for d in debris_field if d["material"] == "rock"))

    # Distance is checked before anything else (shared with repair radius).
    first = debris_field[0]
    engine.PLAYERS["Vesta"]["pos"] = list(spawn)
    vesta.send({"t": "harvest", "id": first["id"], "name": "Vesta"})
    too_far = vesta.wait_for(lambda m: m["t"] == "toast" and "Fly closer" in m["text"], attempts=20)
    check("harvest rejects a distant pilot", too_far is not None)

    # Inert rock is never consumed and has nothing to extract.
    rock = next(d for d in debris_field if d["material"] == "rock")
    engine.PLAYERS["Vesta"]["pos"] = list(rock["pos"])
    vesta.send({"t": "harvest", "id": rock["id"], "name": "Vesta"})
    refused = vesta.wait_for(lambda m: m["t"] == "toast" and "Inert rock" in m["text"], attempts=20)
    check("rock harvesting is refused", refused is not None)
    check("rock stays in the world", not engine.DEBRIS[rock["id"]]["harvested"])

    # Methane fills the methane tank.
    methane = next(d for d in debris_field if d["material"] == "methane")
    v = engine.PLAYERS["Vesta"]
    v["pos"] = list(methane["pos"])
    v["methane"] = 0
    vesta.send({"t": "harvest", "id": methane["id"], "name": "Vesta"})
    harvested = vesta.wait_for(lambda m: m["t"] == "toast" and "Harvested" in m["text"], attempts=20)
    check("methane harvest succeeds", harvested is not None)
    check("harvest fills the methane tank", v["methane"] == methane["yield"])
    gone = deanna.wait_for(lambda m: m["t"] == "debris_gone" and m["id"] == methane["id"], attempts=20)
    check("debris removal broadcast to other pilot", gone is not None and "vitals" in gone)
    check("debris marked harvested", engine.DEBRIS[methane["id"]]["harvested"])

    # Metal fills the metal tank (HP comes later, from the conversion loop).
    ore = next(d for d in debris_field if d["material"] == "metal")
    v["pos"] = list(ore["pos"])
    v["metal"] = 0
    vesta.send({"t": "harvest", "id": ore["id"], "name": "Vesta"})
    got_metal = vesta.wait_for(lambda m: m["t"] == "toast" and "Harvested" in m["text"], attempts=20)
    check("metal harvest succeeds", got_metal is not None)
    check("harvest fills the metal tank", v["metal"] == ore["yield"])

    # A full tank refuses the harvest and leaves the chunk in place.
    v["methane"] = v["max_methane"]
    full = next(d for d in debris_field
                if d["material"] == "methane" and not engine.DEBRIS[d["id"]]["harvested"])
    v["pos"] = list(full["pos"])
    vesta.send({"t": "harvest", "id": full["id"], "name": "Vesta"})
    capped = vesta.wait_for(lambda m: m["t"] == "toast" and "tank is full" in m["text"], attempts=20)
    check("full tank refuses harvest", capped is not None)
    check("refused chunk stays harvestable", not engine.DEBRIS[full["id"]]["harvested"])

    # ── repair: energy -> nearby teammate shields (D25) ─────────────────
    mate = engine.PLAYERS["Deanna"]
    mate["shields"] = 60
    v["energy"] = 100
    v["pos"] = list(mate["pos"])
    vesta.send({"t": "repair", "target": "Deanna", "amount": 5})
    result = vesta.wait_for(lambda m: m["t"] == "repair_result", attempts=20)
    check("energy-to-shields repair succeeds", result and result["success"] is True)
    check("repair raises teammate shields", mate["shields"] == 65)
    check("repair spends healer energy", v["energy"] == 95)
    v["pos"] = [mate["pos"][0], mate["pos"][1], mate["pos"][2] + 20]
    vesta.send({"t": "repair", "target": "Deanna", "amount": 1})
    far_repair = vesta.wait_for(lambda m: m["t"] == "repair_result" and m["success"] is False,
                                attempts=20)
    check("repair refuses a distant teammate", far_repair is not None)

    # ── per-second conversions (D25) ────────────────────────────────────
    v = engine.PLAYERS["Vesta"]
    mate = engine.PLAYERS["Deanna"]
    v["methane"], v["energy"], v["shields"] = 10, 10, 10
    v["metal"], v["hp"] = 10, 10
    engine._convert_resources()
    check("methane -> energy -> shields is 1:1",
          v["methane"] == 9 and v["energy"] == 10 and v["shields"] == 11,
          f"ch4={v['methane']} en={v['energy']} sh={v['shields']}")
    check("metal -> HP is 1:1", v["metal"] == 9 and v["hp"] == 11)

    v["energy"], v["shields"], v["methane"] = 5, v["max_shields"], 0
    mate["shields"], mate["energy"], mate["methane"] = 40, 0, 0
    mate["pos"] = list(v["pos"])
    engine._convert_resources()
    check("excess energy repairs a nearby teammate's shields",
          mate["shields"] == 41 and v["energy"] == 4,
          f"shields={mate['shields']} energy={v['energy']}")
    v["metal"], v["hp"] = 5, v["max_hp"]
    mate["hp"], mate["metal"] = 40, 0
    engine._convert_resources()
    check("excess metal repairs a nearby teammate's HP",
          mate["hp"] == 41 and v["metal"] == 4,
          f"hp={mate['hp']} metal={v['metal']}")

    # The driver schedules one conversion step per second of ticks.
    engine._conversion_accum = 0
    scheduled = engine.tick_once()
    check("tick_once schedules conversions and reports deltas",
          engine._conversion_accum == 1
          and "moved" in scheduled and "vitals_changed" in scheduled)

    # ── shield regeneration via real ticks ─────────────────────────────
    v["shields"] = v["max_shields"] - 2
    v["methane"] = v["metal"] = 0
    ticks_per_regen = max(1, int(engine.shield_regen_ms / engine.tick_ms))
    for _ in range(ticks_per_regen * 2 + 5):
        engine.tick_once()
    check(f"shield regen after 2 x {ticks_per_regen} ticks",
          v["shields"] == v["max_shields"], f"shields={v['shields']}")

    # ── inert-rock gravity (D25) ────────────────────────────────────────
    all_debris = dict(engine.DEBRIS)
    engine.DEBRIS = {rock["id"]: rock}
    try:
        rp = rock["pos"]
        v["pos"] = [rp[0] + 3, rp[1], rp[2]]
        before = list(v["pos"])
        drifted = engine.tick_once()
        check("rock gravity pulls a pilot toward the rock",
              math.dist(v["pos"], rp) < math.dist(before, rp),
              f"{before} -> {v['pos']}")
        check("gravity drift is reported for broadcast", "Vesta" in drifted["moved"])
    finally:
        engine.DEBRIS = all_debris

    # ── one-way wormhole (all three outcomes) ───────────────────────────
    # The wormhole is sealed while a guardian stands at it, so clear the
    # field first: that is the level's arc.
    engine.PORTALS["end_portal"]["enemies_remaining"] = 0
    engine.PLAYERS["Vesta"]["pos"] = list(spawn)
    vesta.send({"t": "enter_portal", "name": "Vesta"})
    far = vesta.wait_for(lambda m: m["t"] == "toast" and "Too far" in m["text"], attempts=20)
    check("portal rejects a pilot far away", far is not None)

    engine.PORTALS["end_portal"]["enemies_remaining"] = 1
    engine.PLAYERS["Vesta"]["pos"] = [portal[0], portal[1], portal[2] - 3]
    vesta.send({"t": "enter_portal", "name": "Vesta"})
    sealed = vesta.wait_for(lambda m: m["t"] == "toast" and "sealed" in m["text"], attempts=20)
    check("a sealed wormhole refuses the pilot", sealed is not None)

    engine.PORTALS["end_portal"]["enemies_remaining"] = 0
    engine.PLAYERS["Vesta"]["pos"] = [portal[0], portal[1], portal[2] - 3]
    level_before = engine.PLAYERS["Vesta"]["level"]
    vesta.send({"t": "enter_portal", "name": "Vesta"})
    win = vesta.wait_for(lambda m: m["t"] == "toast" and "slipped through the wormhole" in m["text"], attempts=20)
    check("portal accepts a front-side approach", win is not None)
    check("entry leaves level unchanged for now",
          engine.PLAYERS["Vesta"]["level"] == level_before)
    check("entry returns the pilot to spawn", near(engine.PLAYERS["Vesta"]["pos"], spawn, 3))

    engine.PLAYERS["Vesta"]["pos"] = [portal[0], portal[1], portal[2] + 2]
    vesta.send({"t": "enter_portal", "name": "Vesta"})
    back = vesta.wait_for(lambda m: m["t"] == "toast" and "spawn-facing side" in m["text"], attempts=20)
    check("portal rejects a back-side approach", back is not None)

    # ── remaining WS messages ───────────────────────────────────────────
    vesta.send({"t": "fire", "x": 0.5, "y": 0.5})
    fired = vesta.wait_for(lambda m: m["t"] == "toast" and "Server judges" in m["text"], attempts=20)
    check("fire receives a server toast", fired is not None)

    # Targeted prototype fire (D14 simplified): the engine judges the shot.
    # Move the shooter back in range after the portal-approach tests above.
    engine.PLAYERS["Vesta"]["pos"] = list(engine.PLAYERS["Deanna"]["pos"])
    engine.PLAYERS["Deanna"]["shields"] = 50
    engine.PLAYERS["Deanna"]["hp"] = 80
    vesta.send({"t": "fire", "target": "Deanna", "name": "Vesta"})
    hit = vesta.wait_for(lambda m: m["t"] == "combat" and m["kind"] == "shot", attempts=20)
    check("targeted fire broadcasts a hit", hit is not None
          and hit["target"] == "Deanna")
    check("shields absorb damage before HP",
          engine.PLAYERS["Deanna"]["shields"] == 44
          and engine.PLAYERS["Deanna"]["hp"] == 80)
    vesta.send({"t": "fire", "target": "Deanna", "name": "Vesta"})
    cooldown = vesta.wait_for(lambda m: m["t"] == "toast" and "cycling" in m["text"], attempts=20)
    check("fire cooldown rejects a second immediate shot", cooldown is not None)

    # ── mob targeting: the engine judges shots against mob instances too ──
    engine.add_mob("smoke_mob2")
    sm = engine.MOBS["smoke_mob2"]
    sm["hp"], sm["shields"] = 5, 0
    sm["pos"] = [32, 32, 58]
    engine.PLAYERS["Vesta"]["pos"] = [32, 32, 58]
    engine.LAST_FIRE.clear()
    ok, reason, hit = engine.fire_at("Vesta", "smoke_mob2")
    check("a pilot can fire at a living mob", ok and hit is not None, reason)
    check("a killing blow on a mob drops salvage",
          not sm["alive"] and len(engine.ROOM_LOOT.get(engine.ROOM_ID, [])) > 0)

    vesta.send({"t": "die"})
    died = vesta.wait_for(lambda m: m["t"] == "you" and m["you"].get("hp") == 0, attempts=20)
    check("die reports death", died is not None)
    loot_ev = vesta.wait_for(lambda m: m["t"] == "loot", attempts=20)
    check("death broadcasts the dropped loot", loot_ev is not None
          and (loot_ev.get("item") or {}).get("id") == "energy_cell", str(loot_ev))
    engine.respawn_player("Vesta")
    check("respawn restores HP and spawn position",
          engine.PLAYERS["Vesta"]["status"] == "alive"
          and engine.PLAYERS["Vesta"]["hp"] == engine.PLAYERS["Vesta"]["max_hp"]
          and near(engine.PLAYERS["Vesta"]["pos"], spawn, 3))
    check("death arms the authoritative respawn timer", "Vesta" in engine.DEATH_TIMERS
          and engine.DEATH_TIMERS["Vesta"] > 0)
    # Now test the timer-driven respawn: kill again and let the clock do it.
    # Die away from the spawn, so this drop does not land at the pilot's
    # feet and spoil the out-of-reach check below.
    engine.PLAYERS["Vesta"]["pos"] = [32, 32, 58]
    engine.DEATH_TIMERS.pop("Vesta", None)
    engine.cause_death("Vesta")
    respawn_tick = None
    for _ in range(int(engine_mod.get_balance().get("death_respawn_ticks", 50))):
        status = engine.tick_once()
        if engine.PLAYERS["Vesta"]["status"] == "alive":
            respawn_tick = status
            break
    check("the tick loop respawns a dead pilot", engine.PLAYERS["Vesta"]["status"] == "alive")
    check("respawn clears the evade cooldown", engine.PLAYERS["Vesta"].get("evade_cooldown", -1e9) == -1e9)
    # A respawn is a teleport; the next tick must not read it as speed.
    engine.tick_once()
    check("respawn does not leave a phantom velocity",
          float(engine.PLAYERS["Vesta"].get("speed") or 0) < 1.0,
          str(engine.PLAYERS["Vesta"].get("speed")))
    check("a respawn reports the new position for broadcast",
          bool(respawn_tick) and "Vesta" in (respawn_tick.get("moved") or []),
          str((respawn_tick or {}).get("moved")))

    # Salvage dropped by the death above stays where it fell: the pilot has
    # to fly to the wreck before it can be grabbed.
    vesta.send({"t": "take", "item": "energy_cell"})
    refused = vesta.wait_for(lambda m: m["t"] == "toast" and m.get("success") is False,
                             attempts=20)
    check("salvage out of reach is refused", refused is not None,
          str(refused.get("text") if refused else None))
    dropped = (engine.ROOM_LOOT.get(engine.ROOM_ID) or [])
    if dropped and dropped[0].get("pos"):
        engine.PLAYERS["Vesta"]["pos"] = list(dropped[0]["pos"])
    vesta.send({"t": "take", "item": "energy_cell"})
    took = vesta.wait_for(lambda m: m["t"] == "toast" and m.get("success") is True
                          and "Picked up" in m.get("text", ""), attempts=20)
    check("take picks up nearby salvage", took is not None
          and "Picked up" in took.get("text", ""))

    # ── NPC welcome payload near the portal ─────────────────────────────
    # The roster owns the mob population: one guard per connected pilot,
    # stationed near the wormhole.
    roster_mob = next((m for m in engine.MOBS.values() if m.get("alive")), None)
    check("a roster mob stands near the portal",
          roster_mob is not None
          and near(roster_mob["pos"], [portal[0], portal[1], portal[2] - 2], 14),
          str(roster_mob["pos"] if roster_mob else None))
    third = Game("Troi")
    third_welcome = third.wait_for(lambda m: m["t"] == "welcome", attempts=20)
    check("welcome lists live NPCs",
          third_welcome and any(n["id"] in engine.MOBS for n in third_welcome["npcs"]),
          str(third_welcome.get("npcs") if third_welcome else None))
    check("the roster grows with the connected pilots",
          len(engine.MOBS) >= 3, f"{len(engine.MOBS)} mobs for 3 pilots")

    status = engine.tick_once()
    check("tick_once reports game status",
          status["tick"] > 0 and status["active_portal"] and status["players"] >= 3)

    for pilot in (vesta, deanna, third):
        pilot.close()


def main():
    print(f"smoke: Level 1 world state isolated in {_TMP}")
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
    print("\nsmoke PASSED — Level 1 game loop ran through real code")


if __name__ == "__main__":
    main()
