"""Game engine: connections, movement, scanning, combat, loot, affiliates.

The server is authoritative (D2): clients render events and never move
actors, roll dice, or judge their own shots. All randomness flows
through the module-global ``rng`` so the smoke test can make combat
deterministic. Events are broadcast room-scoped (D4); whispers and
affiliations are the documented exceptions.

Space conversion (D11-D17): rooms are solar-system bodies and lanes;
bug mobs aggro by default; combat happens in a server-driven
target-shooter overlay; death strips the ship and drops salvage.
"""

import asyncio
import json
import random
import sys
import time

from . import db, settings, world

rng = random.Random()

PLAYERS = {}         # character id -> player actor
MOBS = {}            # mob instance id -> mob actor
SPAWNS = []          # spawn points created at boot from world data
ROOM_SOCKETS = {}    # room id -> [websocket, ...]
PLAYER_SOCKS = {}    # character id -> websocket
SOCKET_ACTORS = {}   # websocket -> actor
ROOM_LOOT = {}       # room id -> [{"item": item_id, "count": n}]
FOLLOW_REQ = {}      # target char id -> requester char id
TARGETS = {}         # attacker char id -> live target square
TICK = None
LOOP = None
_TASKS = set()
_next_instance = [0]


# ---- derived ship numbers ---------------------------------------------------

def max_hull(level: int) -> int:
    return world.max_hull(level)


def xp_next(level: int) -> int:
    return world.xp_next(level)


def xp_reward(mob_template: dict) -> int:
    return world.xp_reward(mob_template)


def energy_cap(actor: dict) -> int:
    return world.ship_type(actor["type"])["energy_cap"]


def armor_rating(actor: dict) -> int:
    return sum(world.item(p).get("level", 1) for p in actor["plates"])


def gun_power(actor: dict) -> dict:
    """Kinetic (armor-blocked), energy and artifact damage for one shot."""
    kinetic = sum(world.item(g)["level"] for g in actor["guns"]
                  if world.item(g)["weapon"] == "kinetic")
    levels = [world.item(g)["level"] for g in actor["guns"]
              if world.item(g)["weapon"] == "kinetic"]
    b = world.balance()["combo"]
    if len(levels) >= b["same_level_for_multiplier"] \
            and len(set(levels)) == 1:
        kinetic *= b["multiplier"]  # the 10x same-level weapon combo
    energy = sum(world.item(g)["level"] for g in actor["guns"]
                 if world.item(g)["weapon"] == "energy")
    artifacts = len(actor["artifacts"])
    str_bonus = actor["stats"]["str"] // world.balance()["str_bonus_div"]
    return {"kinetic": kinetic + str_bonus, "energy": energy,
            "artifacts": artifacts}


def evade_chance(actor: dict) -> float:
    ev = world.balance()["evade"]
    return min(ev["cap"], ev["base"] + actor["stats"]["agi"] * ev["per_agi"])


def self_repair_chance(actor: dict) -> float:
    """Bug repairs fast; hybrid medium; human only with bug plate (light)."""
    if actor["cls"] == "human":
        if actor["plates"]:
            return min(0.3, actor["stats"]["rep"] / 10)
        return 0.0
    return actor["stats"]["rep"] / 10


# ---- sockets and events -----------------------------------------------------

async def _send(sock, event: dict) -> None:
    try:
        await sock.send_json(event)
    except Exception:
        pass  # socket closing; the disconnect path cleans everything up


def _queue(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if LOOP is not None and LOOP.is_running():
            asyncio.run_coroutine_threadsafe(coro, LOOP)
        return
    task = loop.create_task(coro)
    _TASKS.add(task)


def broadcast(room_id: str, event: dict) -> None:
    """Queue a room-scoped event; fails silently for closed sockets."""
    _queue(_broadcast(room_id, event))


async def _broadcast(room_id: str, event: dict) -> None:
    for sock in list(ROOM_SOCKETS.get(room_id, [])):
        await _send(sock, event)


def _send_self(actor: dict, event: dict) -> None:
    sock = PLAYER_SOCKS.get(actor["id"])
    if sock is not None:
        _queue(_send(sock, event))


def _toast(actor: dict, text: str) -> None:
    _send_self(actor, {"t": "toast", "text": text})


# ---- views ------------------------------------------------------------------

def _player_view(actor: dict) -> dict:
    return {"kind": "player", "id": actor["id"], "name": actor["name"],
            "level": actor["level"], "cls": actor["cls"],
            "hp": actor["hp"], "max_hp": actor["max_hp"]}


def _mob_view(mob: dict) -> dict:
    return {"kind": "mob", "id": mob["id"], "name": mob["name"],
            "mob_kind": mob["kind"], "level": mob["level"],
            "hp": mob["hp"], "max_hp": mob["max_hp"]}


def _room_view(room_id: str, for_actor=None) -> dict:
    view = world.snapshot(room_id)
    view["players"] = [_player_view(a) for a in PLAYERS.values()
                       if a["room"] == room_id]
    view["mobs"] = [_mob_view(m) for m in MOBS.values() if m["room"] == room_id]
    view["loot"] = [dict(entry, name=world.item_name(entry["item"]))
                    for entry in ROOM_LOOT.get(room_id, [])]
    if for_actor is not None:
        view["chart_ttl"] = _chart_seconds_left(for_actor, room_id)
    return view


def _self_view(actor: dict) -> dict:
    slots = world.ship_type(actor["type"])["slots"]
    return {
        "id": actor["id"], "name": actor["name"], "cls": actor["cls"],
        "type": actor["type"], "level": actor["level"],
        "stats": dict(actor["stats"]),
        "hp": actor["hp"], "max_hp": actor["max_hp"],
        "xp": actor["xp"], "xp_next": actor["xp_next"],
        "energy": actor["energy"], "energy_max": energy_cap(actor),
        "guns": list(actor["guns"]), "plates": list(actor["plates"]),
        "artifacts": list(actor["artifacts"]),
        "slots": slots,
        "room": actor["room"],
    }


def _chart_seconds_left(actor: dict, room_id: str):
    expiry = actor["chart"].get(room_id)
    if expiry is None:
        return None  # permanent
    return max(0, int(expiry - time.time()))


# ---- connect / disconnect ---------------------------------------------------

def _load_player(session_row: dict):
    char = db.character_by_id(session_row["character_id"])
    if not char:
        return None
    slots = world.ship_type(char["type"])["slots"]
    return {
        "kind": "player", "id": char["id"], "name": char["name"],
        "cls": char["cls"], "type": char["type"],
        "level": char["level"], "stats": json.loads(char["stats"]),
        "hp": min(char["hp"], world.max_hull(char["level"])),
        "max_hp": world.max_hull(char["level"]),
        "xp": char["xp"], "xp_next": world.xp_next(char["level"]),
        "guns": json.loads(char["guns"]), "plates": json.loads(char["plates"]),
        "artifacts": json.loads(char["artifacts"]),
        "energy": min(char["energy"], energy_cap({"type": char["type"]})),
        "chart": json.loads(char["chart"]),
        "affiliates": set(),
        "scan_queue": [],
        "room": char["room_id"], "target": None,
    }


def _save(actor: dict) -> None:
    db.save_character(
        actor["id"], hp=actor["hp"], xp=actor["xp"], level=actor["level"],
        room_id=actor["room"], guns=json.dumps(actor["guns"]),
        plates=json.dumps(actor["plates"]),
        artifacts=json.dumps(actor["artifacts"]),
        energy=actor["energy"], chart=json.dumps(actor["chart"]))


def _refresh_scan_queue(actor: dict) -> None:
    """Queue not-yet-charted rooms of the current system for passive scan."""
    system = world.room(actor["room"]).get("system")
    actor["scan_queue"] = [
        rid for rid, room in world.rooms().items()
        if room.get("system") == system
        and rid != actor["room"] and rid not in actor["chart"]]


def _reveal(actor: dict, room_id: str, permanent: bool = False) -> bool:
    if room_id in actor["chart"] and actor["chart"][room_id] is None:
        return False
    was_known = room_id in actor["chart"]
    actor["chart"][room_id] = None if permanent else time.time() + \
        world.balance()["scan_ttl_s"]
    return not was_known


async def handle_socket(sock, session_row: dict) -> None:
    actor = _load_player(session_row)
    if actor is None:
        await sock.send_json({"t": "error", "text": "create a character first"})
        await sock.close(code=4002)
        return

    SOCKET_ACTORS[sock] = actor
    PLAYERS[actor["id"]] = actor
    PLAYER_SOCKS[actor["id"]] = sock
    ROOM_SOCKETS.setdefault(actor["room"], []).append(sock)
    _reveal(actor, actor["room"])
    _refresh_scan_queue(actor)

    broadcast(actor["room"], {"t": "enter", "who": _player_view(actor)})
    await _send(sock, {"t": "welcome", "you": _self_view(actor),
                       "room": _room_view(actor["room"], actor),
                       "chart": _chart_view(actor)})

    try:
        while True:
            msg = await sock.receive_json()
            await _dispatch(actor, msg)
    except Exception as exc:
        print(f"engine: socket {actor['name']} ended: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        _disconnect(sock)


def _disconnect(sock) -> None:
    actor = SOCKET_ACTORS.pop(sock, None)
    if actor is None:
        return
    sockets = ROOM_SOCKETS.get(actor["room"], [])
    if sock in sockets:
        sockets.remove(sock)
    PLAYER_SOCKS.pop(actor["id"], None)
    PLAYERS.pop(actor["id"], None)
    TARGETS.pop(actor["id"], None)
    _break_combat(actor)
    for other_id in list(actor["affiliates"]):
        other = PLAYERS.get(other_id)
        if other:
            other["affiliates"].discard(actor["id"])
    actor["affiliates"].clear()
    _save(actor)
    broadcast(actor["room"], {"t": "leave", "who": actor["name"]})


def _break_combat(actor: dict) -> None:
    actor["target"] = None
    TARGETS.pop(actor["id"], None)
    for mob in MOBS.values():
        if mob["target"] == actor["id"]:
            mob["target"] = None
    for other in PLAYERS.values():
        if other["target"] == actor["id"]:
            other["target"] = None


# ---- combat targets (the shooter overlay) -----------------------------------

def _spawn_target(actor: dict) -> None:
    """Server-driven target square over the enemy, tracking-scaled."""
    if actor["id"] in TARGETS:
        return
    shot = world.balance()["shot"]
    size = shot["size_base"] + actor["stats"]["trk"] * shot["size_per_trk"]
    dwells = shot["dwell_base_s"] + actor["stats"]["trk"] * shot["dwell_per_trk"]
    square = {
        "x": round(rng.uniform(0.15, 0.85), 3),   # fractions of the view
        "y": round(rng.uniform(0.15, 0.85), 3),
        "w": round(size / 1000, 3),                # fraction of view width
        "expires": time.time() + dwells,
    }
    TARGETS[actor["id"]] = square
    _send_self(actor, {"t": "target", "x": square["x"], "y": square["y"],
                       "w": square["w"],
                       "dwell": round(dwells, 2)})


def _fire(actor: dict, x, y) -> None:
    square = TARGETS.get(actor["id"])
    if not square:
        return
    if time.time() > square["expires"] \
            or not (square["x"] <= x <= square["x"] + square["w"]) \
            or not (square["y"] <= y <= square["y"] + square["w"]):
        TARGETS.pop(actor["id"], None)
        _toast(actor, "Your shot went wide.")
        _spawn_target(actor)
        return

    TARGETS.pop(actor["id"], None)
    target = actor["target"]
    victim = MOBS.get(target) or PLAYERS.get(target)
    if victim is None or victim["room"] != actor["room"] \
            or victim["id"] == actor["id"]:
        actor["target"] = None
        return

    power = gun_power(actor)
    if victim["kind"] == "mob":
        armor = victim["armor"]
    else:
        armor = armor_rating(victim)
    kinetic = power["kinetic"] - armor
    damage = max(world.balance()["shot"]["min_damage"], kinetic) \
        + power["energy"] + power["artifacts"]
    crit = rng.random() < world.balance()["shot"]["crit_chance"]
    if crit:
        damage *= world.balance()["shot"]["crit_mult"]

    victim["hp"] = max(0, victim["hp"] - damage)
    broadcast(actor["room"], {
        "t": "combat", "events": [{
            "kind": "swing", "attacker": actor["name"],
            "attacker_id": actor["id"], "victim": victim["name"],
            "victim_id": victim["id"], "dmg": damage, "crit": crit,
            "victim_hp": victim["hp"], "victim_max": victim["max_hp"],
        }]})

    if victim["hp"] <= 0:
        if victim["kind"] == "mob":
            _on_mob_death(victim, actor)
        else:
            _on_player_death(victim, actor)
    else:
        _spawn_target(actor)


# ---- deaths and loot --------------------------------------------------------

def _add_loot(room_id: str, item_id: str) -> None:
    for entry in ROOM_LOOT.setdefault(room_id, []):
        if entry["item"] == item_id:
            entry["count"] += 1
            break
    else:
        ROOM_LOOT[room_id].append({"item": item_id, "count": 1})
    broadcast(room_id, {"t": "loot", "room": room_id,
                        "loot": [dict(e, name=world.item_name(e["item"]))
                                 for e in ROOM_LOOT[room_id]]})


def _on_mob_death(mob: dict, killer) -> None:
    entry = next((sp for sp in SPAWNS if sp["inst"] == mob["id"]), None)
    if entry is not None:
        entry["inst"] = None
        entry["next"] = time.time() + entry["respawn"]
    MOBS.pop(mob["id"], None)

    reward = 0
    if killer is not None and killer["kind"] == "player":
        reward = world.xp_reward(mob["template"])
        _award_xp(killer, reward)
        for drop in mob["template"].get("drops", []):
            if rng.random() < drop["chance"]:
                _add_loot(killer["room"], drop["item"])

    broadcast(mob["room"], {"t": "combat", "events": [{
        "kind": "die", "who": mob["name"],
        "by": killer["name"] if killer else "", "xp": reward,
    }]})


def _on_player_death(player: dict, killer) -> None:
    """Death strips the ship; some gear is left behind as salvage (D17)."""
    _break_combat(player)
    for mob in MOBS.values():
        if mob["target"] == player["id"]:
            mob["target"] = None
    half = 2 if world.balance()["death_drop_half"] else 0
    for item_id in player["guns"][::2][:len(player["guns"]) // 2 + 1] \
            if half else []:
        _add_loot(player["room"], item_id)
    for item_id in player["plates"][::2][:len(player["plates"]) // 2 + 1] \
            if half else []:
        _add_loot(player["room"], item_id)

    player["guns"] = []
    player["plates"] = []
    player["artifacts"] = []
    player["energy"] = 0
    if player["level"] > 1:
        player["level"] -= 1
        player["xp"] = 0
        player["xp_next"] = world.xp_next(player["level"])
    player["max_hp"] = world.max_hull(player["level"])
    player["hp"] = player["max_hp"]

    old_room = player["room"]
    player["room"] = settings.get("starting_room")
    _save(player)
    sockets = ROOM_SOCKETS.get(old_room, [])
    sock = PLAYER_SOCKS.get(player["id"])
    if sock in sockets:
        sockets.remove(sock)
    broadcast(old_room, {"t": "combat", "events": [{
        "kind": "die", "who": player["name"],
        "by": killer["name"] if killer else "",
    }]})
    _toast(player, "Your ship is stripped by scavengers! You wake up in the "
                   "Titan docks, one level down.")
    ROOM_SOCKETS.setdefault(player["room"], []).append(sock)
    broadcast(player["room"], {"t": "enter", "who": _player_view(player)})
    _send_self(player, {"t": "room", "room": _room_view(player["room"], player)})
    _send_self(player, {"t": "you", "you": _self_view(player)})


def _award_xp(player: dict, amount: int) -> None:
    player["xp"] += amount
    leveled = False
    while player["xp"] >= player["xp_next"]:
        player["level"] += 1
        leveled = True
    player["xp_next"] = world.xp_next(player["level"])
    player["max_hp"] = world.max_hull(player["level"])
    if leveled:
        player["hp"] = player["max_hp"]
        broadcast(player["room"],
                  {"t": "toast", "text": f"{player['name']} reached level "
                                         f"{player['level']}!"})
    _save(player)
    _send_self(player, {"t": "you", "you": _self_view(player)})


# ---- movement and scanning --------------------------------------------------

def _move(actor: dict, direction: str) -> None:
    room = world.room(actor["room"])
    destination = room.get("exits", {}).get(direction)
    if not destination or destination not in world.rooms():
        _toast(actor, "You can't go that way.")
        return

    _break_combat(actor)
    sock = PLAYER_SOCKS.get(actor["id"])
    sockets = ROOM_SOCKETS.get(actor["room"], [])
    if sock in sockets:
        sockets.remove(sock)
    broadcast(actor["room"], {"t": "leave", "who": actor["name"]})

    actor["room"] = destination
    ROOM_SOCKETS.setdefault(destination, []).append(sock)
    broadcast(destination, {"t": "enter", "who": _player_view(actor)})
    _reveal(actor, destination)
    _refresh_scan_queue(actor)
    _save(actor)
    _send_self(actor, {"t": "room", "room": _room_view(destination, actor)})

    if actor["kind"] == "player":
        for affiliate_id in actor["affiliates"]:
            follower = PLAYERS.get(affiliate_id)
            if follower and follower["room"] == room["id"]:
                _move(follower, direction)  # affiliates follow into attack


# ---- mob AI and spawns ------------------------------------------------------

def _spawn_instance(entry: dict) -> None:
    template = world.mob(entry["mob"])
    _next_instance[0] += 1
    inst = f"m{_next_instance[0]}"
    actor = {
        "kind": "mob", "id": inst, "name": template["name"],
        "mob_kind": template["kind"], "template": template,
        "room": entry["room"], "level": template["level"],
        "hp": template["hp"], "max_hp": template["hp"],
        "armor": template["armor"], "gun": template["gun"],
        "target": None,
    }
    MOBS[inst] = actor
    entry["inst"] = inst
    broadcast(entry["room"], {"t": "combat", "events": [{
        "kind": "spawn", "who": template["name"], "level": template["level"],
    }]})


def tick_once() -> None:
    """One round: mob attacks, self-repairs, passive scans, respawns."""
    for mob in list(MOBS.values()):
        if mob["target"] is not None:
            victim = PLAYERS.get(mob["target"])
            if victim is None or victim["room"] != mob["room"]:
                mob["target"] = None
                continue
            damage = max(world.balance()["shot"]["min_damage"],
                         mob["gun"] - armor_rating(victim))
            crit = rng.random() < world.balance()["shot"]["crit_chance"]
            if crit:
                damage *= world.balance()["shot"]["crit_mult"]
            victim["hp"] = max(0, victim["hp"] - damage)
            broadcast(mob["room"], {
                "t": "combat", "events": [{
                    "kind": "swing", "attacker": mob["name"],
                    "attacker_id": mob["id"], "victim": victim["name"],
                    "victim_id": victim["id"], "dmg": damage, "crit": crit,
                    "victim_hp": victim["hp"],
                    "victim_max": victim["max_hp"],
                }]})
            if victim["hp"] <= 0:
                _on_player_death(victim, mob)

    for player in list(PLAYERS.values()):
        if player["target"] is not None:
            victim = MOBS.get(player["target"]) or PLAYERS.get(player["target"])
            if victim is None or victim["room"] != player["room"]:
                player["target"] = None
                TARGETS.pop(player["id"], None)
        if player["target"] is not None and player["id"] not in TARGETS:
            _spawn_target(player)
        # passive self-repair (D14): bug fast, hybrid medium, human light
        if player["hp"] < player["max_hp"] \
                and rng.random() < self_repair_chance(player):
            player["hp"] = min(player["max_hp"], player["hp"] + 1)
        # passive scan: one body of the current system per round (D13)
        if player["scan_queue"]:
            rid = player["scan_queue"].pop(0)
            if _reveal(player, rid):
                _send_self(player, {
                    "t": "scan", "body": world.room(rid)["body"],
                    "room": world.room(rid)["name"]})

    for mob in list(MOBS.values()):
        if mob["target"] is not None or mob["template"].get("friendly"):
            continue
        if world.room(mob["room"]).get("safe"):
            continue
        for player in PLAYERS.values():
            if player["room"] == mob["room"]:
                mob["target"] = player["id"]
                broadcast(mob["room"], {"t": "combat", "events": [{
                    "kind": "aggro", "who": mob["name"], "on": player["name"],
                }]})
                break

    now = time.time()
    for entry in SPAWNS:
        if entry["inst"] is None and now >= entry["next"]:
            _spawn_instance(entry)


def spawn_all() -> None:
    SPAWNS.clear()
    MOBS.clear()
    ROOM_LOOT.clear()
    for room_id, room in world.rooms().items():
        for spec in room.get("spawns", []):
            entry = {"room": room_id, "mob": spec["mob"],
                     "respawn": spec.get("respawn", 40),
                     "next": 0.0, "inst": None}
            SPAWNS.append(entry)
    for entry in SPAWNS:
        _spawn_instance(entry)


def start_tick() -> None:
    global TICK
    TICK = asyncio.create_task(_tick_loop())


def stop_tick() -> None:
    if TICK is not None:
        TICK.cancel()


async def _tick_loop() -> None:
    while True:
        try:
            await asyncio.sleep(settings.get_int("tick_ms") / 1000)
            tick_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"engine: tick failed: {exc}", file=sys.stderr)


# ---- client actions ---------------------------------------------------------

def _find_mob(actor: dict, key: str):
    return next((m for m in MOBS.values()
                 if m["room"] == actor["room"]
                 and (m["id"].lower() == key
                      or key in m["name"].lower())), None)


async def _dispatch(actor: dict, msg: dict) -> None:
    action = (msg or {}).get("t")
    if action == "move":
        _move(actor, str(msg.get("dir", "")))
    elif action == "look":
        _send_self(actor, {"t": "room",
                           "room": _room_view(actor["room"], actor)})
    elif action == "attack":
        _attack_action(actor, str(msg.get("target", "")))
    elif action == "fire":
        _fire(actor, msg.get("x", -1), msg.get("y", -1))
    elif action == "evade":
        _evade(actor)
    elif action == "repair":
        _repair(actor, str(msg.get("target", "")))
    elif action == "take":
        _take(actor, str(msg.get("item", "")))
    elif action == "talk":
        _talk(actor)
    elif action == "chart":
        _send_self(actor, {"t": "chart", "entries": _chart_view(actor)})
    elif action == "jump":
        _jump(actor, str(msg.get("destination", "")))
    elif action == "say":
        text = str(msg.get("text", "")).strip()[:200]
        if text:
            broadcast(actor["room"],
                      {"t": "say", "from": actor["name"], "text": text})
    elif action == "whisper":
        _whisper(actor, str(msg.get("target", "")),
                 str(msg.get("text", "")).strip()[:200])
    elif action == "follow":
        _follow(actor, str(msg.get("target", "")))
    elif action == "accept":
        _accept(actor, str(msg.get("from", "")))
    elif action == "decline":
        FOLLOW_REQ.pop(actor["id"], None)
        _toast(actor, "Affiliation request declined.")
    elif action == "unfollow":
        _unfollow(actor, str(msg.get("target", "")))


def _attack_action(actor: dict, target_key: str) -> None:
    if not target_key:
        _break_combat(actor)
        _toast(actor, "You break off the attack.")
        return
    key = target_key.lower()
    if key == actor["name"].lower() or key == str(actor["id"]):
        _toast(actor, "You can't attack yourself.")
        return
    affiliate = next((a for a in actor["affiliates"]
                      if PLAYERS.get(a, {}).get("name", "").lower().startswith(key)
                      or str(a) == key), None)
    if affiliate:
        _toast(actor, "You can't attack an affiliate.")
        return

    mob = _find_mob(actor, key)
    if mob is not None:
        actor["target"] = mob["id"]
        _toast(actor, f"You attack the {mob['name']}.")
        _spawn_target(actor)
        return
    player = next((p for p in PLAYERS.values()
                   if p["room"] == actor["room"]
                   and (p["name"].lower().startswith(key)
                        or str(p["id"]) == key)), None)
    if player is not None:
        actor["target"] = player["id"]
        player["target"] = actor["id"]
        _toast(actor, f"You attack {player['name']}!")
        _spawn_target(actor)
        return
    _toast(actor, "There is nothing like that here to attack.")


def _evade(actor: dict) -> None:
    if actor["target"] is None:
        _toast(actor, "You are not under attack.")
        return
    if rng.random() < evade_chance(actor):
        exits = list(world.room(actor["room"]).get("exits", {}).items())
        if exits:
            direction, _ = exits[rng.randrange(len(exits))]
            _toast(actor, "Burn! You evade the fight.")
            _move(actor, direction)
            return
    _toast(actor, "The evasion burn fails — you are still here.")


def _repair(actor: dict, target_name: str) -> None:
    b = world.balance()["repair"]
    if actor["energy"] < b["cost"]:
        _toast(actor, "Not enough energy to run the repair drones.")
        return
    key = target_name.strip().lower()
    if not key or key == actor["name"].lower():
        target = actor
    else:
        target = next((p for p in PLAYERS.values()
                       if p["room"] == actor["room"]
                       and p["id"] in actor["affiliates"]
                       and p["name"].lower().startswith(key)), None)
        if target is None:
            _toast(actor, "No affiliate by that name is in this room.")
            return
    actor["energy"] -= b["cost"]
    target["hp"] = min(target["max_hp"], target["hp"] + b["heal"])
    _save(target)
    broadcast(actor["room"], {"t": "combat", "events": [{
        "kind": "repair", "tech": actor["name"], "target": target["name"],
        "amount": b["heal"], "target_hp": target["hp"],
        "target_max": target["max_hp"],
    }]})


def _take(actor: dict, item_key: str) -> None:
    key = item_key.strip().lower()
    loot = ROOM_LOOT.get(actor["room"], [])
    entry = next((e for e in loot if e["item"] == key
                  or world.item_name(e["item"]).lower().startswith(key)), None)
    if entry is None:
        _toast(actor, "There is no salvage like that here.")
        return
    item = world.item(entry["item"])
    slots = world.ship_type(actor["type"])["slots"]
    kind = item["kind"]
    if kind == "gun":
        if len(actor["guns"]) >= slots["guns"]:
            _toast(actor, "Your gun racks are full.")
            return
        actor["guns"].append(entry["item"])
    elif kind == "plate":
        if actor["cls"] == "bug":
            _toast(actor, "Bug hulls can't carry human-built armor.")
            return
        if len(actor["plates"]) >= slots["armor"]:
            _toast(actor, "Your armor racks are full.")
            return
        actor["plates"].append(entry["item"])
    elif kind == "artifact":
        if len(actor["artifacts"]) >= slots["artifacts"]:
            _toast(actor, "Your artifact hold is full.")
            return
        actor["artifacts"].append(entry["item"])
    elif kind == "cell":
        cap = energy_cap(actor)
        if actor["energy"] >= cap:
            _toast(actor, "Your energy banks are full.")
            return
        actor["energy"] = min(cap, actor["energy"] + item["energy"])
    else:
        return

    entry["count"] -= 1
    if entry["count"] <= 0:
        loot.remove(entry)
    _save(actor)
    broadcast(actor["room"], {"t": "loot", "room": actor["room"],
                              "loot": [dict(e, name=world.item_name(e["item"]))
                                       for e in loot]})
    _toast(actor, f"You take the {item['name']}.")
    _send_self(actor, {"t": "you", "you": _self_view(actor)})


def _talk(actor: dict) -> None:
    broker = next((m for m in MOBS.values()
                   if m["room"] == actor["room"]
                   and m["template"].get("friendly")), None)
    if broker is None:
        _toast(actor, "Nobody here wants to talk.")
        return
    talk = broker["template"]["talk"]
    if actor["chart"].get("remote:jupiter") is None:
        for rid, room in world.rooms().items():
            if room.get("system") == talk.get("grants_system"):
                _reveal(actor, rid, permanent=True)
        for remote in talk.get("grants_remote", []):
            actor["chart"][f"remote:{remote}"] = None
        _save(actor)
        for line in talk.get("lines", []):
            _send_self(actor, {"t": "say", "from": broker["name"], "text": line})
        _send_self(actor, {"t": "chart", "entries": _chart_view(actor)})
    else:
        for line in talk.get("lines", [])[:1]:
            _send_self(actor, {"t": "say", "from": broker["name"], "text": line})
        _toast(actor, "Ilsa has nothing more to teach you.")


def _chart_view(actor: dict) -> list:
    entries = []
    for rid, expiry in sorted(actor["chart"].items()):
        if rid.startswith("remote:"):
            entries.append({"label": f"{rid.split(':', 1)[1].title()} system "
                                     f"(jump coordinates)", "permanent": True})
            continue
        if rid not in world.rooms():
            continue
        room = world.room(rid)
        seconds = None if expiry is None else max(0, int(expiry - time.time()))
        entries.append({"label": room["body"], "room": room["name"],
                        "permanent": expiry is None,
                        "seconds": seconds,
                        "current": rid == actor["room"]})
    return entries


def _jump(actor: dict, destination: str) -> None:
    room = world.room(actor["room"])
    if room.get("kind") != "jump_point":
        _toast(actor, "You have to be at a jump point to plot a jump.")
        return
    key = destination.strip().lower()
    if not key:
        _toast(actor, "Plot a jump to where? Your chart knows: "
                      + ", ".join(e["label"] for e in _chart_view(actor)
                                  if e.get("permanent")))
        return
    if f"remote:{key}" not in actor["chart"]:
        _toast(actor, "You have no jump coordinates for that system. "
                      "Find a chart broker.")
        return
    _toast(actor, "Jump drive spins up… the beacon chain beyond is dark — "
                  "the bugs took it. This jump is not yet restored (v2).")


def _follow(actor: dict, target_name: str) -> None:
    key = target_name.strip().lower()
    target = next((p for p in PLAYERS.values()
                   if p["room"] == actor["room"]
                   and p["name"].lower().startswith(key)
                   and p["id"] != actor["id"]), None)
    if target is None:
        _toast(actor, "No such player here.")
        return
    if target["id"] in actor["affiliates"]:
        _toast(actor, f"You already fly with {target['name']}.")
        return
    FOLLOW_REQ[target["id"]] = actor["id"]
    _send_self(target, {"t": "follow_request", "from": actor["name"],
                        "from_id": actor["id"]})
    _toast(actor, f"You ask {target['name']} to fly with you.")


def _accept(actor: dict, requester_name: str) -> None:
    requester_id = FOLLOW_REQ.pop(actor["id"], None)
    if requester_id is None:
        _toast(actor, "No pending affiliation request.")
        return
    requester = PLAYERS.get(requester_id)
    if not requester:
        return
    actor["affiliates"].add(requester_id)
    requester["affiliates"].add(actor["id"])
    _toast(actor, f"You are now affiliated with {requester['name']}.")
    _toast(requester, f"{actor['name']} accepted — you are affiliated.")


def _whisper(actor: dict, target_name: str, text: str) -> None:
    if not text:
        return
    key = target_name.strip().lower()
    target = next((p for p in PLAYERS.values()
                   if p["name"].lower().startswith(key)
                   and p["id"] in actor["affiliates"]), None)
    if target is None:
        _toast(actor, "You can only whisper to affiliates, anywhere in "
                      "the system.")
        return
    _send_self(target, {"t": "whisper", "from": actor["name"], "text": text})
    _send_self(actor, {"t": "whisper", "from": actor["name"], "text": text})


def _unfollow(actor: dict, target_name: str) -> None:
    key = target_name.strip().lower()
    target = next((p for p in PLAYERS.values()
                   if p["name"].lower().startswith(key)
                   and p["id"] in actor["affiliates"]), None)
    if target is None:
        _toast(actor, "You fly with nobody by that name.")
        return
    actor["affiliates"].discard(target["id"])
    target["affiliates"].discard(actor["id"])
    _toast(actor, f"You part ways with {target['name']}.")
    _toast(target, f"{actor['name']} parted ways with you.")
