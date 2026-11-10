"""Game engine: connections, movement, combat ticks, mob spawns.

The server is authoritative (D2): clients render events, and never move
actors or roll dice. All randomness flows through the module-global
``rng`` (a ``random.Random``) so the smoke test can make combat
deterministic by replacing it. Events are broadcast room-scoped (D4):
a socket never receives combat or say events for a room it is not in.
"""

import asyncio
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
TICK = None          # the tick-loop asyncio task
LOOP = None          # the main event loop; set at startup so engine
                     # calls from other threads can still broadcast
_TASKS = set()       # in-flight broadcasts; keep-alive so GC never drops them
_next_instance = [0]


# ---- derived stats ----------------------------------------------------------

def max_hp(stats: dict, level: int) -> int:
    return world.max_hp(stats, level)


def xp_next(level: int) -> int:
    return world.xp_next(level)


# ---- sockets and events -----------------------------------------------------

async def _send(sock, event: dict) -> None:
    try:
        await sock.send_json(event)
    except Exception:
        pass  # socket closing; the disconnect path cleans everything up


def _queue(coro) -> None:
    """Schedule a coroutine from anywhere: loop-internal as a task,
    thread-external via run_coroutine_threadsafe (smoke harness)."""
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


# ---- actors -----------------------------------------------------------------

def _player_view(actor: dict) -> dict:
    return {"kind": "player", "id": actor["id"], "name": actor["name"],
            "level": actor["level"], "class1": actor["class1"],
            "hp": actor["hp"], "max_hp": actor["max_hp"]}


def _mob_view(mob: dict) -> dict:
    return {"kind": "mob", "id": mob["id"], "name": mob["name"],
            "level": mob["level"], "hp": mob["hp"], "max_hp": mob["max_hp"]}


def _room_view(room_id: str) -> dict:
    view = world.snapshot(room_id)
    view["players"] = [_player_view(a) for a in PLAYERS.values()
                       if a["room"] == room_id]
    view["mobs"] = [_mob_view(m) for m in MOBS.values() if m["room"] == room_id]
    return view


def _self_view(actor: dict) -> dict:
    view = {k: actor[k] for k in
            ("id", "name", "gender", "race", "class1", "class2", "level",
             "hp", "max_hp", "xp", "xp_next", "weapon", "armor", "room")}
    view["stats"] = dict(actor["stats"])
    return view


def _load_player(session_row: dict):
    char = db.character_by_id(session_row["character_id"])
    if not char:
        return None
    stats = {"str": char["strength"], "int": char["intelligence"],
             "wis": char["wisdom"], "dex": char["dexterity"],
             "con": char["constitution"]}
    max_hp = world.max_hp(stats, char["level"])
    return {
        "kind": "player", "id": char["id"], "name": char["name"],
        "gender": char["gender"], "race": char["race"],
        "class1": char["class1"], "class2": char["class2"],
        "level": char["level"], "stats": stats,
        "hp": min(char["hp"], max_hp), "max_hp": max_hp,
        "xp": char["xp"], "xp_next": world.xp_next(char["level"]),
        "weapon": char["weapon"], "armor": char["armor"],
        "room": char["room_id"], "target": None, "last_vigor": 0.0,
    }


def _save(actor: dict) -> None:
    db.save_character(actor["id"], hp=actor["hp"], xp=actor["xp"],
                      level=actor["level"], room_id=actor["room"],
                      weapon=actor["weapon"], armor=actor["armor"])


# ---- connect / disconnect ---------------------------------------------------

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

    broadcast(actor["room"], {"t": "enter", "who": _player_view(actor)})
    await _send(sock, {"t": "welcome", "you": _self_view(actor),
                       "room": _room_view(actor["room"])})

    try:
        while True:
            msg = await sock.receive_json()
            await _dispatch(actor, msg)
    except Exception:
        pass  # receive_json raises on disconnect / bad json
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
    _break_combat(actor)
    _save(actor)
    broadcast(actor["room"], {"t": "leave", "who": actor["name"]})


def _break_combat(actor: dict) -> None:
    actor["target"] = None
    for mob in MOBS.values():
        if mob["target"] == actor["id"]:
            mob["target"] = None


# ---- client actions ---------------------------------------------------------

async def _dispatch(actor: dict, msg: dict) -> None:
    action = (msg or {}).get("t")
    if action == "move":
        _move(actor, str(msg.get("dir", "")))
    elif action == "look":
        _send_self(actor, {"t": "room", "room": _room_view(actor["room"])})
    elif action == "attack":
        _attack_action(actor, msg.get("target"))
    elif action == "vigor":
        _vigor(actor, msg.get("target"))
    elif action == "say":
        text = str(msg.get("text", "")).strip()[:200]
        if text:
            broadcast(actor["room"],
                      {"t": "say", "from": actor["name"], "text": text})


def _move(actor: dict, direction: str) -> None:
    room = world.room(actor["room"])
    exits = room.get("exits", {})
    destination = exits.get(direction)
    if not destination or destination not in world.rooms():
        _toast(actor, "You can't go that way.")
        return

    _break_combat(actor)  # leaving a room breaks off combat (classic MUD rule)
    sockets = ROOM_SOCKETS.get(actor["room"], [])
    sock = PLAYER_SOCKS.get(actor["id"])
    if sock in sockets:
        sockets.remove(sock)
    broadcast(actor["room"], {"t": "leave", "who": actor["name"]})

    actor["room"] = destination
    ROOM_SOCKETS.setdefault(destination, []).append(sock)
    broadcast(destination, {"t": "enter", "who": _player_view(actor)})
    _save(actor)
    _send_self(actor, {"t": "room", "room": _room_view(destination)})


def _attack_action(actor: dict, target) -> None:
    if target in (None, ""):
        actor["target"] = None
        _toast(actor, "You stop fighting.")
        return
    key = str(target).lower()
    mob = next((m for m in MOBS.values()
                if m["room"] == actor["room"]
                and (m["id"].lower() == key
                     or m["name"].lower().startswith(key))), None)
    if mob is None:
        _toast(actor, "There is nothing like that here to attack.")
        return
    actor["target"] = mob["id"]
    _toast(actor, f"You attack {mob['name']}.")
    tick_once()  # swing immediately; the tick loop keeps it up


def _vigor(actor: dict, target_name) -> None:
    from . import settings  # call-time read (D2)
    if "cleric" not in (actor["class1"], actor["class2"]):
        _toast(actor, "Only clerics know the words of vigor.")
        return
    cooldown = world.balance()["vigor_cooldown_s"]
    if time.time() - actor["last_vigor"] < cooldown:
        _toast(actor, "Vigor is not ready yet.")
        return
    key = str(target_name or "").lower()
    target = next((p for p in PLAYERS.values()
                   if p["room"] == actor["room"]
                   and (not key or p["name"].lower().startswith(key))), None)
    if target is None:
        _toast(actor, "No teammate by that name is here.")
        return
    amount = world.balance()["vigor_base"] + actor["stats"]["wis"] // 2 \
        + actor["level"] * 2
    target["hp"] = min(target["max_hp"], target["hp"] + amount)
    actor["last_vigor"] = time.time()
    broadcast(actor["room"], {
        "t": "combat", "events": [{
            "kind": "cast", "caster": actor["name"], "spell": "vigor",
            "target": target["name"], "amount": amount,
            "target_hp": target["hp"], "target_max": target["max_hp"],
        }]})
    _save(target)


# ---- combat -----------------------------------------------------------------

def _attack(attacker: dict, victim: dict) -> None:
    b = world.balance()["attack"]
    weapon = world.item(attacker["weapon"] or "") or {}
    armor = world.item(victim["armor"] or "") or {}
    roll = rng.randint(1, 20)
    defense = b["base_defense"] + victim["level"] // 2 + armor.get("armor", 0)
    hit = (roll == 20 or roll + attacker["level"]
           + attacker["stats"]["dex"] // 2 >= defense)
    if not hit:
        broadcast(attacker["room"], {
            "t": "combat", "events": [{
                "kind": "miss", "attacker": attacker["name"],
                "victim": victim["name"], "room": attacker["room"],
                "victim_hp": victim["hp"], "victim_max": victim["max_hp"],
            }]})
        return

    damage = (rng.randint(b["dmg_min"], b["dmg_max"])
              + weapon.get("damage", 0) + attacker["stats"]["str"] // b["str_div"])
    crit = roll == 20
    if crit:
        damage *= 2
    victim["hp"] = max(0, victim["hp"] - damage)
    broadcast(attacker["room"], {
        "t": "combat", "events": [{
            "kind": "swing", "attacker": attacker["name"],
            "victim": victim["name"], "dmg": damage, "crit": crit,
            "room": attacker["room"],
            "victim_hp": victim["hp"], "victim_max": victim["max_hp"],
        }]})

    if victim["hp"] <= 0:
        if victim["kind"] == "mob":
            _on_mob_death(victim, attacker)
        else:
            _on_player_death(victim, attacker)


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
        # auto-equip loot into empty slots; the rest is lost for now (v1)
        for drop in mob["template"].get("loot", []):
            if world.item(drop) and not killer["weapon"]:
                killer["weapon"] = drop
                _toast(killer, f"You pick up the {world.item(drop)['name']}.")

    broadcast(mob["room"], {"t": "combat", "events": [{
        "kind": "die", "who": mob["name"], "by": killer["name"] if killer else "",
        "xp": reward,
    }]})


def _on_player_death(player: dict, killer) -> None:
    _break_combat(player)
    for mob in MOBS.values():
        if mob["target"] == player["id"]:
            mob["target"] = None
    player["hp"] = player["max_hp"] // 4
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
    _toast(player, "You have fallen! You wake up back at the newbie school.")
    ROOM_SOCKETS.setdefault(player["room"], []).append(sock)
    broadcast(player["room"], {"t": "enter", "who": _player_view(player)})
    _send_self(player, {"t": "room", "room": _room_view(player["room"])})


def _award_xp(player: dict, amount: int) -> None:
    player["xp"] += amount
    leveled = False
    while player["xp"] >= world.xp_next(player["level"]):
        player["level"] += 1
        leveled = True
    player["max_hp"] = world.max_hp(player["stats"], player["level"])
    if leveled:
        player["hp"] = player["max_hp"]
        broadcast(player["room"],
                  {"t": "toast", "text": f"{player['name']} reached level "
                                         f"{player['level']}!"})
    _save(player)
    _send_self(player, {"t": "you", "you": _self_view(player)})


# ---- mob AI and spawns ------------------------------------------------------

def _spawn_instance(entry: dict) -> None:
    template = world.mob(entry["mob"])
    _next_instance[0] += 1
    inst = f"m{_next_instance[0]}"
    stats = dict(template["stats"])
    actor = {
        "kind": "mob", "id": inst, "name": template["name"],
        "template": template, "room": entry["room"],
        "level": template["level"], "stats": stats,
        "hp": template["hp"], "max_hp": template["hp"],
        "weapon": None, "armor": None, "target": None,
    }
    MOBS[inst] = actor
    entry["inst"] = inst
    broadcast(entry["room"], {"t": "combat", "events": [{
        "kind": "spawn", "who": template["name"], "level": template["level"],
    }]})


def tick_once() -> None:
    """One combat round: engaged actors swing, mobs aggro, spawns return."""
    for mob in list(MOBS.values()):
        if mob["target"] is not None:
            victim = PLAYERS.get(mob["target"])
            if victim is None or victim["room"] != mob["room"]:
                mob["target"] = None
            else:
                _attack(mob, victim)

    for player in list(PLAYERS.values()):
        if player["target"] is None:
            continue
        mob = MOBS.get(player["target"])
        if mob is None or mob["room"] != player["room"]:
            player["target"] = None
            continue
        _attack(player, mob)

    for mob in list(MOBS.values()):
        if mob["target"] is not None:
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
    """(Re)create every spawn point from world data and fill them."""
    SPAWNS.clear()
    MOBS.clear()
    for room_id, room in world.rooms().items():
        for spec in room.get("spawns", []):
            entry = {"room": room_id, "mob": spec["mob"],
                     "respawn": spec.get("respawn", 40),
                     "next": 0.0, "inst": None}
            SPAWNS.append(entry)
    for entry in SPAWNS:
        _spawn_instance(entry)


# ---- tick loop --------------------------------------------------------------

def start_tick() -> None:
    global TICK
    TICK = asyncio.create_task(_tick_loop())


def stop_tick() -> None:
    if TICK is not None:
        TICK.cancel()


async def _tick_loop() -> None:
    from . import settings  # call-time read (D2)
    while True:
        try:
            await asyncio.sleep(settings.get_int("tick_ms") / 1000)
            tick_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"engine: tick failed: {exc}", file=sys.stderr)
