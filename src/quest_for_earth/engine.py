"""Engine: rooms, scanner, combat, shield/repair, NPC AI, affiliates, death.

Room-based server-authoritative tick loop for Level 1 (8 players, 64^3 cube).
All content in data/*.json; hot-reloads on change.
"""

import json
import math
import os
import glob
import sqlite3
import random
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_TICK_MS = 100          # 10 TPS
DEFAULT_SHIELD_REGEN_MS = 30000   # 1 point every 30 s
DEFAULT_DEATH_RESPAWN_TICKS = 50   # 5 s at 10 TPS
DEFAULT_NPC_RESPAWN_TICKS = 900    # 90 s at 10 TPS

CONFIG_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# Load world / balance from JSON (hot-reload capable)
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def get_world():
    return _load_json(CONFIG_DIR / "world.json") or {}

def get_balance():
    return _load_json(CONFIG_DIR / "balance.json") or {}

def get_ships():
    return _load_json(CONFIG_DIR / "ships.json") or {}

def get_mobs_template():
    return _load_json(CONFIG_DIR / "mobs.json") or {}

def get_items():
    return _load_json(CONFIG_DIR / "items.json") or {}


def _parse_pos(value, default):
    """Accept a JSON string like "[32,32,58]" or a list of numbers."""
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    if isinstance(value, str):
        try:
            return [float(x) for x in value.strip("[] \t").split(",")]
        except Exception:
            pass
    return [float(x) for x in default]

# ---------------------------------------------------------------------------
# Simple in-memory state (SQLite could be swapped in later)
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self):
        self.tick_ms = int(get_balance().get("tick_ms", DEFAULT_TICK_MS))
        self.shield_regen_ms = int(get_balance().get("shield_regen_ms", DEFAULT_SHIELD_REGEN_MS))
        self.death_respawn_ticks = int(get_balance().get("death_respawn_ticks", DEFAULT_DEATH_RESPAWN_TICKS))
        self.npc_respawn_ticks = int(get_balance().get("npc_respawn_ticks", DEFAULT_NPC_RESPAWN_TICKS))

        # Players: char_id -> dict
        self.PLAYERS = {}

        # Mobs: instance_id -> dict
        self.MOBS = {}

        # Room loot: room_id -> list of item dicts
        self.ROOM_LOOT = {}

        # Portals: room_id -> dict
        self.PORTALS = {}

        # Mob spawn timers: mob_id -> ticks_until_respawn
        self.MOB_TIMERS = {}

        # Tick counter (global)
        self.tick_count = 0

        # Death countdowns (ticks) per dead pilot; the tick loop respawns.
        self.DEATH_TIMERS = {}

        # The web layer drives ticks in the background; the smoke harness
        # stops that driver and ticks explicitly for deterministic tests.
        self._tick_stopped = False
        self._conversion_accum = 0
        self.LAST_FIRE = {}
        self.MOB_ATTACK_TICKS = {}

        # Initialise from world/balance
        self._reinit()

    def _reinit(self):
        """Reload world/balance and reset spawn-timer state."""
        w = get_world()
        b = get_balance()

        # Reset players
        self.PLAYERS = {}

        # Reset mobs / respawn timers / weapon cooldowns / death timers
        self.MOBS = {}
        self.MOB_TIMERS = {}
        self.MOB_ATTACK_TICKS = {}
        self.DEATH_TIMERS = {}

        # Reset room loot
        self.ROOM_LOOT = {}
        self.LAST_FIRE = {}

        # Spawn and portal positions come from the world data (D1).
        spawn_room = w.get("rooms", {}).get("spawn_cube", {})
        portal_room = w.get("rooms", {}).get("end_cube", {})
        self.SPAWN_POS = _parse_pos(spawn_room.get("spawn_position"), [32, 12, 6])
        self.PORTAL_POS = _parse_pos(portal_room.get("portal_position"), [32, 32, 58])
        self.PORTAL_FRONT = portal_room.get("portal_front", "-z")
        self.PORTAL_APERTURE = float(portal_room.get("portal_aperture", 4))
        self.PORTALS = {
            "end_portal": {
                "room": "end_cube",
                "position": list(self.PORTAL_POS),
                "front": self.PORTAL_FRONT,
                "active": True,
                "enemies_remaining": 0,
            }
        }

        # Set enemy count = player count (enforced by caller)
        self.PORTALS["end_portal"]["enemies_remaining"] = 0

        # Debris field between spawn and portal (seeded => same every run).
        self.DEBRIS = self._build_debris_field(w.get("debris_field", {}))

        self.tick_count = 0

    def _build_debris_field(self, cfg):
        """Scatter a data-defined mix of debris along the spawn→portal corridor.

        `counts` gives exact per-type numbers; the older weighted `types` list
        remains supported for other worlds. Both are content, not code (D1).
        """
        items = {it["id"]: it for it in get_items().get("items", [])}
        seed = int(cfg.get("seed", 1337))
        counts = cfg.get("counts")
        if counts:
            planned = []
            for item_id, n in counts.items():
                item = items.get(item_id)
                if item:
                    planned.extend([item] * max(0, int(n)))
        else:
            weighted = []
            for entry in cfg.get("types", []):
                item = items.get(entry.get("id"))
                if item:
                    weighted.extend([item] * max(1, int(entry.get("weight", 1))))
            pick_rng = random.Random(seed)
            planned = ([pick_rng.choice(weighted)
                        for _ in range(int(cfg.get("count", 0)))] if weighted else [])
        if not planned:
            return {}

        rng = random.Random(seed)
        sp, pp = self.SPAWN_POS, self.PORTAL_POS
        z0 = int(min(sp[2], pp[2])) + int(cfg.get("z_padding", 8))
        z1 = int(max(sp[2], pp[2])) - int(cfg.get("z_padding", 8))
        if z1 <= z0:
            return {}
        spread_x = int(cfg.get("spread_x", 20))
        spread_y = int(cfg.get("spread_y", 14))
        y_mid = (sp[1] + pp[1]) / 2

        field = {}
        for i, item in enumerate(planned):
            # Retry so the launch pad and portal approach stay clear without
            # silently dropping chunks from an exact-count field.
            for _ in range(40):
                pos = [
                    max(2, min(61, sp[0] + rng.randint(-spread_x, spread_x))),
                    max(2, min(61, y_mid + rng.randint(-spread_y, spread_y))),
                    rng.randint(z0, z1),
                ]
                if (abs(pos[2] - sp[2]) < 4 and abs(pos[0] - sp[0]) < 4):
                    continue
                if (abs(pos[2] - pp[2]) < 5 and abs(pos[0] - pp[0]) < 5
                        and abs(pos[1] - pp[1]) < 5):
                    continue
                break
            else:
                continue
            did = f"debris_{i}"
            material = item.get("material", "stone")
            # Rock size is uniform and data-defined (D25); other chunks keep 1.
            size = (int(cfg.get("rock_size", 3)) if material == "rock" else 1)
            field[did] = {
                "id": did,
                "type": item["id"],
                "name": item["name"],
                "material": material,
                "yield": int(item.get("yield", 1)),
                "pos": [float(x) for x in pos],
                "size": size,
                "harvested": False,
            }
        return field

    def stop_tick(self):
        """Stop the web layer's background driver (explicit-tick mode)."""
        self._tick_stopped = True

    # ---- player management ----

    def add_player(self, char_id, name, pos=None):
        """Add a new player at spawn cube."""
        if pos is None:
            # Spawn at the near end, opposite the portal; offset so pilots don't overlap
            pos = [self.SPAWN_POS[0] + random.randint(-2, 2),
                   self.SPAWN_POS[1] + random.randint(-2, 2),
                   self.SPAWN_POS[2] + random.randint(-2, 2)]
        balance = get_balance()
        hp = float(balance.get("player_starting_hp", 100))
        shields = float(balance.get("player_starting_shields", 100))
        energy_cap = float(balance.get("energy_max", 100))
        self.PLAYERS[char_id] = {
            "name": name,
            "room": "spawn_cube",
            "pos": [float(x) for x in pos],      # x,y,z in 64^3
            "look": [0.0, 0.0],                    # yaw, pitch fractions
            "hp": hp,
            "shields": shields,
            "max_hp": hp,
            "max_shields": shields,
            # D25: three onboard stores. Tanks start empty, energy full.
            "energy": energy_cap,
            "max_energy": energy_cap,
            "methane": 0.0,
            "max_methane": float(balance.get("methane_tank_max", 100)),
            "metal": 0.0,
            "max_metal": float(balance.get("metal_tank_max", 100)),
            "xp": 0,
            "level": 1,
            "status": "alive",
            "guns": [],       # Level 1: no guns start (or starter)
            "plates": [],     # Level 1: no plates
            "artifacts": [],
        }

    def remove_player(self, char_id):
        if char_id in self.PLAYERS:
            del self.PLAYERS[char_id]

    def get_player(self, char_id):
        return self.PLAYERS.get(char_id)

    # ---- mob / NPC management ----

    def add_mob(self, mob_id, pos=None):
        """Add an NPC mob at or near the end cube portal."""
        if pos is None:
            # Spread around the portal (just in front of it, on the spawn side)
            pp = self.PORTAL_POS
            offset = [random.randint(-4, 4), random.randint(-4, 4), random.randint(-4, 4)]
            pos = [pp[0] + offset[0], pp[1] + offset[1], pp[2] - 2 + offset[2]]

        template = get_mobs_template().get("mobs", [{}])[0]
        self.MOBS[mob_id] = {
            "id": mob_id,
            "type": template.get("name", "npc"),
            "room": "end_cube",
            "pos": [float(x) for x in pos],
            "hp": float(template.get("max_hp", 20)),
            "max_hp": float(template.get("max_hp", 20)),
            "shields": float(template.get("max_shields", 10)),
            "max_shields": float(template.get("max_shields", 10)),
            "damage": float(template.get("damage", 5)),
            "tracking": float(template.get("tracking", 1)),
            "repair_per_round": float(template.get("repair_per_round", 0)),
            "alive": True,
            "bounty_xp": int(template.get("bounty_xp", 10)),
        }
        # Set timer for this mob (respawn when dead)
        self.MOB_TIMERS[mob_id] = int(get_balance().get("npc_respawn_ticks", DEFAULT_NPC_RESPAWN_TICKS))

    def remove_mob(self, mob_id):
        if mob_id in self.MOBS:
            del self.MOBS[mob_id]
        if mob_id in self.MOB_TIMERS:
            del self.MOB_TIMERS[mob_id]

    def get_mob(self, mob_id):
        return self.MOBS.get(mob_id)

    # ---- shield regeneration (per-player, tick-driven) ----

    # ---- repair: energy -> teammate shields (D25) ----

    def repair_shields(self, healer_id, target_id, amount):
        """Spend the healer's energy to restore a nearby teammate's shields.

        1 energy = 1 shield. Own shields are refilled automatically from
        energy in the per-second conversion loop; this action is the explicit,
        targeted form for a wingman in range.
        """
        healer = self.get_player(healer_id)
        target = self.get_player(target_id)
        if not healer or not target or healer["status"] != "alive" or target["status"] != "alive":
            return False, "invalid players"
        if healer_id == target_id:
            return False, "your own shields refill automatically"
        radius = float(get_balance().get("nearby_radius", 5))
        if math.dist(healer["pos"], target["pos"]) > radius:
            return False, "target is too far away"

        amount = max(0, int(amount))
        if amount <= 0:
            return False, "invalid amount"
        if healer["energy"] < amount:
            return False, "not enough energy"
        if target["shields"] >= target["max_shields"]:
            return False, "target shields are already full"

        spend = min(amount, healer["energy"], target["max_shields"] - target["shields"])
        healer["energy"] -= spend
        target["shields"] += spend
        return True, None

    # ---- prototype direct fire (D14 simplified for Level 1) ----

    def fire_at(self, shooter_id, target_id):
        """Resolve one authoritative shot at an NPC mob or another pilot.

        Shields absorb damage first; the remainder hits HP. Range and
        cooldown come from balance.json, never from the client. The server
        owns the shot (D2): the target may be a mob instance or a pilot id.
        """
        shooter = self.get_player(shooter_id)
        if not shooter or shooter["status"] != "alive":
            return False, "invalid shooter", None
        target = self.MOBS.get(target_id)
        is_mob = target is not None
        if not is_mob:
            target = self.get_player(target_id)
        if not target or target.get("status", "alive") != "alive":
            return False, "invalid target", None
        if shooter_id == target_id:
            return False, "you cannot shoot yourself", None

        balance = get_balance()
        rng = float(balance.get("fire_range", 24))
        if math.dist(shooter["pos"], target["pos"]) > rng:
            return False, "target out of range", None
        cooldown = max(1, int(balance.get("fire_cooldown_ms", 400))) / max(1, self.tick_ms)
        last = self.LAST_FIRE.get(shooter_id, -1e9)
        if self.tick_count - last < cooldown:
            return False, "gun still cycling", None
        self.LAST_FIRE[shooter_id] = self.tick_count

        damage = float(balance.get("fire_damage", 6))
        absorbed = min(float(target.get("shields") or 0), damage)
        target["shields"] = float(target.get("shields") or 0) - absorbed
        hull = damage - absorbed
        target["hp"] = max(0, float(target.get("hp") or 0) - hull)
        hit = {
            "from": shooter_id,
            "target": target_id,
            "kind": "mob" if is_mob else "pilot",
            "damage": damage,
            "shields": float(target["shields"]),
            "hp": float(target["hp"]),
        }
        if target["hp"] <= 0:
            if is_mob:
                self._kill_mob(target_id, shooter_id)
            else:
                self.cause_death(target_id, shooter_id)
            hit["dead"] = True
        return True, "hit", hit

    # ---- death & loot ----

    def _drop_loot(self, actor):
        """Salvage falls out of a dead agent into the room it died in (D15)."""
        rule = get_items().get("salvage_on_death") or {}
        loot = self.ROOM_LOOT.setdefault(actor.get("room") or "spawn_cube", [])
        dropped = []
        if int(rule.get("artifacts") or 0) <= 0:
            return dropped
        items_map = {it.get("id"): it for it in get_items().get("items", []) if it}
        art = items_map.get("energy_cell") or {"id": "salvage_1", "name": "Salvage",
                                              "type": "salvage", "rarity": "common"}
        entry = dict(art)
        entry["count"] = int(rule.get("artifacts") or 1)
        entry["dropped_by"] = actor.get("name") or "?"
        loot.append(entry)
        dropped.append(entry)
        return dropped

    def _kill_mob(self, mob_id, killer_id):
        """A dead mob drops salvage for the killer and schedules respawn."""
        mob = self.MOBS.get(mob_id)
        if not mob or not mob.get("alive"):
            return
        killer = self.get_player(killer_id)
        if killer and killer["status"] == "alive":
            self._award_xp(killer_id, int(mob.get("bounty_xp") or 10))
        mob["alive"] = False
        mob["hp"] = 0.0
        mob["dropped_loot"] = self._drop_loot(mob)
        self.MOB_TIMERS[mob_id] = int(get_balance().get("npc_respawn_ticks",
                                                          DEFAULT_NPC_RESPAWN_TICKS))

    def cause_death(self, victim_id, killer_id=None):
        """Kill a pilot. Transfer HP to killer, award XP, drop salvage."""
        victim = self.get_player(victim_id)
        if not victim or victim["status"] != "alive":
            return None

        victim["status"] = "dead"
        victim["hp"] = 0
        self.DEATH_TIMERS[victim_id] = int(get_balance().get("death_respawn_ticks",
                                                                 DEFAULT_DEATH_RESPAWN_TICKS))

        # XP penalty: the loser loses a slice of current XP; the winner takes
        # back what the loser actually carried.
        xp_lost = int(victim.get("xp") or 0) * int(get_balance().get("death_xp_penalty_percent", 15)) // 100
        victim["xp"] = max(0, int(victim.get("xp") or 0) - xp_lost)

        result = {"victim": victim_id, "xp_lost": xp_lost, "shield_gain_to_killer": 0}
        if killer_id and killer_id in self.PLAYERS:
            killer = self.PLAYERS[killer_id]
            if killer["status"] == "alive":
                # "The hit points they normally have when healthy will transfer,
                # in quantity, to the shields of whoever killed them."
                shield_gain = min(float(killer.get("max_shields") or 100) - float(killer.get("shields") or 0),
                                  float(victim.get("max_hp") or 100))
                killer["shields"] = min(float(killer.get("max_shields") or 100),
                                       float(killer.get("shields") or 0) + shield_gain)
                result["shield_gain_to_killer"] = shield_gain
                self._award_xp(killer_id, int(victim.get("xp") or 0))

        result["dropped"] = self._drop_loot(victim) if victim.get("status") == "dead" else []
        return result

    def _apply_level_bonuses(self, player):
        """Raise the ship's stores to the new level's maxima (D28/XP loop)."""
        level = int(player.get("level") or 1)
        hull = get_balance().get("hull") or {}
        hp = float(hull.get("base") or 100) + (level - 1) * float(get_balance().get("hull_per_level") or 0)
        shields = float(get_balance().get("player_starting_shields") or 100) + (level - 1) * float(get_balance().get("shield_per_level") or 0)
        player["max_hp"] = max(float(player.get("max_hp") or 100), hp)
        player["max_shields"] = max(float(player.get("max_shields") or 100), shields)
        player["energy"] = float(player.get("max_energy") or get_balance().get("energy_max") or 100)

    def _award_xp(self, char_id, amount):
        """Grant XP and roll the level-up ladder (data-driven thresholds)."""
        player = self.get_player(char_id)
        if not player or player["status"] != "alive":
            return
        player["xp"] = int(player.get("xp") or 0) + int(amount or 0)
        per_level = float(get_balance().get("xp", {}).get("per_level") or 100)
        while player["xp"] >= per_level * int(player.get("level") or 1):
            player["level"] = int(player.get("level") or 1) + 1
            self._apply_level_bonuses(player)

    def take_loot(self, char_id, item_id):
        """Pick up salvage from the room the pilot is in (D15 salvage loop)."""
        player = self.get_player(char_id)
        loot = self.ROOM_LOOT.get(player.get("room") if player else None, [])
        for i, entry in enumerate(list(loot or [])):
            if entry.get("id") != item_id:
                continue
            del (loot or [])[i]
            player["artifacts"] = list(player.get("artifacts") or []) + [item_id]
            return True, f"Picked up {entry.get('name') or item_id}."
        return False, "No such salvage here."

    def evade(self, char_id):
        """Burst away from the nearest threat (mob or living pilot).

        Level 1 simplification of the agility roll: a short, clamped burst
        away from the closest engaged actor, with a per-pilot cooldown.
        Returns (ok, reason).
        """
        player = self.get_player(char_id)
        if not player or player["status"] != "alive":
            return False, "You cannot evade while down."
        cooldown_ticks = max(1, int(get_balance().get("evade_cooldown_ms", 3000))) / max(1, self.tick_ms)
        if self.tick_count - float(player.get("evade_cooldown") or -1e9) < cooldown_ticks:
            return False, "Too soon to evade."
        player["evade_cooldown"] = self.tick_count

        threat_pos = None
        best = float(get_balance().get("nearby_radius", 5)) * 4
        for mid, mob in self.MOBS.items():
            if not mob.get("alive") or mob.get("status", "alive") != "alive":
                continue
            pos = mob.get("pos") or [0, 0, 0]
            dist = math.dist(player["pos"], pos)
            if dist < best:
                best, threat_pos = dist, pos
        for pid, other in self.PLAYERS.items():
            if pid == char_id or other["status"] != "alive":
                continue
            pos = other.get("pos") or [0, 0, 0]
            dist = math.dist(player["pos"], pos)
            if dist < best:
                best, threat_pos = dist, pos
        if threat_pos is None:
            return False, "Nothing to evade from."
        burst = float(get_balance().get("evade_burst", 14))
        dx = player["pos"][0] - threat_pos[0]
        dy = player["pos"][1] - threat_pos[1]
        dz = player["pos"][2] - threat_pos[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        player["pos"] = [
            max(0, min(63, player["pos"][0] - dx / dist * burst)),
            max(0, min(63, player["pos"][1] - dy / dist * burst)),
            max(0, min(63, player["pos"][2] - dz / dist * burst)),
        ]
        return True, None

    def respawn_player(self, char_id):
        """Respawn player at spawn cube after death penalty."""
        if char_id not in self.PLAYERS:
            return
        player = self.PLAYERS[char_id]
        player["status"] = "alive"
        player["hp"] = player["max_hp"]  # full HP
        player["shields"] = player["max_shields"]  # full shields
        # D15: cargo is lost on death; the ship itself comes back topped up.
        player["energy"] = player["max_energy"]
        player["methane"] = 0.0
        player["metal"] = 0.0
        player["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                         self.SPAWN_POS[1] + random.uniform(-2, 2),
                         self.SPAWN_POS[2] + random.uniform(-2, 2)]
        player["look"] = [0.0, 0.0]
        player["evade_cooldown"] = -1e9
        # XP is already reduced from the death penalty

    # ---- one-way portal ----

    def can_enter_portal(self, char_id):
        """Authoritative check: the wormhole only accepts pilots approaching
        from its spawn-facing side, through the aperture. Returns (ok, reason).
        """
        player = self.PLAYERS.get(char_id)
        if not player:
            return False, "Unknown pilot."
        portal = self.PORTALS.get("end_portal")
        if not portal:
            return False, "No wormhole is open."

        px, py, pz = self.PORTAL_POS
        x, y, z = player["pos"]
        front = self.PORTAL_FRONT

        # Signed distance along the portal normal. Negative means "in front".
        if front == "+z":
            depth = portal["position"][2] - z
            lateral = (x - px, y - py)
        elif front == "+x":
            depth = portal["position"][0] - x
            lateral = (z - pz, y - py)
        else:  # "-z" — the default: approach from the spawn side (low Z)
            depth = z - portal["position"][2]
            lateral = (x - px, y - py)

        if depth > 0:
            return False, "The wormhole can only be entered from the spawn-facing side."
        if depth < -5:
            return False, "Too far from the wormhole. Fly closer."
        ap = self.PORTAL_APERTURE
        if abs(lateral[0]) > ap or abs(lateral[1]) > ap:
            return False, "Line up with the centre of the wormhole."
        return True, ""

    def enter_portal(self, char_id):
        """Return the pilot to spawn. Level progression comes later."""
        ok, reason = self.can_enter_portal(char_id)
        if not ok:
            return False, reason
        player = self.PLAYERS[char_id]
        player["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                         self.SPAWN_POS[1] + random.uniform(-2, 2),
                         self.SPAWN_POS[2] + random.uniform(-2, 2)]
        return True, None

    def harvest_debris(self, char_id, debris_id):
        """Fill the ship's methane or metal tank from a nearby chunk (D25).

        Inert rock cannot be harvested — it stays in the world as a gravity
        well. Tanks cap at their data-driven maximum; a full tank leaves the
        chunk in place for later.
        """
        player = self.PLAYERS.get(char_id)
        debris = self.DEBRIS.get(debris_id)
        if not player or not debris or debris["harvested"]:
            return False, "That debris is no longer available."
        if player["status"] != "alive":
            return False, "You cannot harvest while down."
        radius = float(get_balance().get("nearby_radius", 5))
        if math.dist(player["pos"], debris["pos"]) > radius:
            return False, "Fly closer to the debris to harvest it."

        material = debris["material"]
        if material == "rock":
            return False, "Inert rock — nothing to extract (but it does have mass)."
        tank = {"methane": "methane", "metal": "metal"}.get(material)
        if tank is None:
            return False, "Nothing to extract from that debris."

        gained = max(0, min(player["max_" + tank] - player[tank], debris["yield"]))
        if gained <= 0:
            return False, f"Your {tank} tank is full."
        player[tank] += gained
        debris["harvested"] = True
        return True, f"Harvested {debris['name']} (+{gained} {tank})."

    # ---- per-second resource conversion (D25) ----

    def _nearest_deficit(self, player, key, radius):
        """Another living pilot in range with room in the named store."""
        cap_key = "max_" + key
        for pid, other in self.PLAYERS.items():
            if pid is player or other["status"] != "alive":
                continue
            if math.dist(player["pos"], other["pos"]) > radius:
                continue
            if other[key] < other[cap_key]:
                return other
        return None

    def _convert_resources(self):
        """One conversion step for every living pilot.

        methane -> energy -> shields (self first, then a nearby wingman),
        metal -> HP (self first, then a nearby wingman). All conversions are
        1:1 and capped by the receiving store.
        """
        balance = get_balance()
        radius = float(balance.get("nearby_radius", 5))
        rate = max(1, int(balance.get("conversion_per_second", 1)))
        for player in self.PLAYERS.values():
            if player["status"] != "alive":
                continue
            if player["methane"] > 0 and player["energy"] < player["max_energy"]:
                step = min(rate, player["methane"],
                           player["max_energy"] - player["energy"])
                player["methane"] -= step
                player["energy"] += step

            if player["energy"] > 0 and player["shields"] < player["max_shields"]:
                step = min(rate, player["energy"],
                           player["max_shields"] - player["shields"])
                player["energy"] -= step
                player["shields"] += step
            elif player["energy"] > 0:
                mate = self._nearest_deficit(player, "shields", radius)
                if mate:
                    step = min(rate, player["energy"], mate["max_shields"] - mate["shields"])
                    player["energy"] -= step
                    mate["shields"] += step

            if player["metal"] > 0 and player["hp"] < player["max_hp"]:
                step = min(rate, player["metal"], player["max_hp"] - player["hp"])
                player["metal"] -= step
                player["hp"] += step
            elif player["metal"] > 0:
                mate = self._nearest_deficit(player, "hp", radius)
                if mate:
                    step = min(rate, player["metal"], mate["max_hp"] - mate["hp"])
                    player["metal"] -= step
                    mate["hp"] += step

    # ---- rock gravity (D25) ----

    def _apply_gravity(self):
        """Drift pilots toward nearby inert rock; return ids that moved."""
        balance = get_balance()
        radius = float(balance.get("gravity_radius", 8))
        strength = float(balance.get("gravity_strength", 0.2))
        cap = float(balance.get("gravity_pull_cap", 0.2))
        rocks = [d for d in self.DEBRIS.values()
                 if d["material"] == "rock" and not d["harvested"]]
        if not rocks:
            return []

        moved = []
        for pid, player in self.PLAYERS.items():
            if player["status"] != "alive":
                continue
            px, py, pz = player["pos"]
            dx = dy = dz = 0.0
            for rock in rocks:
                rx, ry, rz = rock["pos"]
                vx, vy, vz = rx - px, ry - py, rz - pz
                dist = math.sqrt(vx * vx + vy * vy + vz * vz)
                if dist <= 0.001 or dist > radius:
                    continue
                pull = min(cap, strength * rock["size"] / max(dist, 1.0))
                dx += vx / dist * pull
                dy += vy / dist * pull
                dz += vz / dist * pull
            if dx or dy or dz:
                player["pos"] = [
                    max(0, min(63, px + dx)),
                    max(0, min(63, py + dy)),
                    max(0, min(63, pz + dz)),
                ]
                moved.append(pid)
        return moved

    def _vitals_snapshot(self):
        return {
            pid: (p["hp"], p["shields"], p["energy"], p["methane"], p["metal"])
            for pid, p in self.PLAYERS.items()
        }

    # ---- NPC respawning ----

    def _tick_npc_respawn(self):
        """Called per tick: decrement timers, spawn dead NPCs."""
        to_spawn = []
        for mob_id, ticks_left in list(self.MOB_TIMERS.items()):
            if not self.MOBS.get(mob_id, {}).get("alive", True):
                # Mob is dead; decrement timer
                self.MOB_TIMERS[mob_id] = max(0, ticks_left - 1)
                if ticks_left <= 1:
                    # Respawn this mob
                    to_spawn.append(mob_id)
        # Actually spawn
        for mob_id in to_spawn:
            self.add_mob(mob_id)
            # Reset timer
            self.MOB_TIMERS[mob_id] = int(get_balance().get("npc_respawn_ticks", DEFAULT_NPC_RESPAWN_TICKS))

    # ---- NPC engagement (Level 1) ----

    def _npc_combat(self):
        """Mobs within aggro distance of a living pilot fire on cooldown.

        Returns the list of landed attacks for the web layer to broadcast;
        a killing blow drops salvage and schedules the mob's respawn.
        """
        balance = get_balance()
        mob_cfg = get_mobs_template().get("spawn_defaults") or {}
        aggro = float(mob_cfg.get("aggro_distance") or balance.get("npc_aggro_distance", 15))
        cd = max(1, int(mob_cfg.get("attack_cooldown_ms")
                        or balance.get("attack_cooldown_ms", 1000))) / max(1, self.tick_ms)
        events = []
        for pid, player in self.PLAYERS.items():
            if player["status"] != "alive":
                continue
            target_mob = None
            best = aggro + 1.0
            for mid, mob in self.MOBS.items():
                if not mob.get("alive") or mob.get("status", "alive") != "alive":
                    continue
                dist = math.dist(player["pos"], mob["pos"])
                if dist > best:
                    continue
                best = dist
                target_mob = mid
            if target_mob is None:
                continue
            mob = self.MOBS.get(target_mob)
            last = self.MOB_ATTACK_TICKS.get(target_mob, -1e9)
            if self.tick_count - last < cd:
                continue
            self.MOB_ATTACK_TICKS[target_mob] = self.tick_count
            damage = float(balance.get("npc_damage", 5))
            absorbed = min(float(mob.get("shields") or 0), damage)
            mob["shields"] = float(mob.get("shields") or 0) - absorbed
            hull = damage - absorbed
            mob["hp"] = max(0, float(mob.get("hp") or 0) - hull)
            events.append({
                "mob": target_mob, "pilot": pid, "damage": damage,
                "shields": float(mob.get("shields") or 0), "hp": float(mob.get("hp") or 0),
                "hit": mob["hp"] <= 0,
            })
            if mob["hp"] <= 0:
                self._kill_mob(target_mob, pid)
        return events

    # ---- tick loop ----

    def tick_once(self):
        """One tick of the engine (≈100 ms at 10 TPS). Deterministic when RNG seeded.

        Returns the usual status plus `moved` and `vitals_changed` pilot ids so
        the web layer can broadcast only what actually changed (D2, D25).
        """
        self.tick_count += 1
        before = self._vitals_snapshot()

        # 1. NPC respawn timer
        self._tick_npc_respawn()

        # 2. Passive shield regeneration (1 point per configured interval)
        ticks_per_regen = max(1, int(self.shield_regen_ms / self.tick_ms))
        if self.tick_count % ticks_per_regen == 0:
            for player in self.PLAYERS.values():
                if player["status"] == "alive" and player["shields"] < player["max_shields"]:
                    player["shields"] = min(player["max_shields"], player["shields"] + 1)

        # 3. D25 resource conversion runs once per second, not every tick.
        self._conversion_accum += 1
        ticks_per_second = max(1, int(1000 / self.tick_ms))
        if self._conversion_accum >= ticks_per_second:
            self._conversion_accum = 0
            self._convert_resources()

        # 4. Inert rock gravity nudges every nearby pilot each tick.
        moved = self._apply_gravity()

        # 5. NPC engagement: living mobs within aggro range of a living pilot
        #    fire on their own cooldown.
        attacks = self._npc_combat()

        # 6. Death timers: respawn dead pilots after the configured delay.
        for pid in list(self.DEATH_TIMERS):
            ticks_left = self.DEATH_TIMERS.get(pid, 0) - 1
            if ticks_left <= 0:
                del self.DEATH_TIMERS[pid]
                self.respawn_player(pid)
            else:
                self.DEATH_TIMERS[pid] = ticks_left

        after = self._vitals_snapshot()
        return {
            "tick": self.tick_count,
            "players": len(self.PLAYERS),
            "mobs": len(self.MOBS),
            "active_portal": bool(self.PORTALS),
            "moved": moved,
            "vitals_changed": [pid for pid in before if before[pid] != after[pid]],
            "attacks": attacks,
        }

# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

engine = Engine()

def stop_tick():
    engine.stop_tick()

def tick_once():
    return engine.tick_once()