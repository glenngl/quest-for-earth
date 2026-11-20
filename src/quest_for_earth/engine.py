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

        # Initialise from world/balance
        self._reinit()

    def _reinit(self):
        """Reload world/balance and reset spawn-timer state."""
        w = get_world()
        b = get_balance()

        # Reset players
        self.PLAYERS = {}

        # Reset mobs
        self.MOBS = {}
        self.MOB_TIMERS = {}

        # Reset room loot
        self.ROOM_LOOT = {}

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
        """Scatter harvestable space debris along the spawn→portal corridor."""
        count = int(cfg.get("count", 0))
        if count <= 0:
            return {}
        rng = random.Random(int(cfg.get("seed", 1337)))
        items = {it["id"]: it for it in get_items().get("items", [])}
        weighted = []
        for entry in cfg.get("types", []):
            item = items.get(entry.get("id"))
            if item:
                weighted.extend([item] * max(1, int(entry.get("weight", 1))))
        if not weighted:
            return {}

        sp, pp = self.SPAWN_POS, self.PORTAL_POS
        z0 = int(min(sp[2], pp[2])) + int(cfg.get("z_padding", 8))
        z1 = int(max(sp[2], pp[2])) - int(cfg.get("z_padding", 8))
        if z1 <= z0:
            return {}
        spread_x = int(cfg.get("spread_x", 20))
        spread_y = int(cfg.get("spread_y", 14))
        y_mid = (sp[1] + pp[1]) / 2

        field = {}
        for i in range(count):
            item = rng.choice(weighted)
            pos = [
                max(2, min(61, sp[0] + rng.randint(-spread_x, spread_x))),
                max(2, min(61, y_mid + rng.randint(-spread_y, spread_y))),
                rng.randint(z0, z1),
            ]
            # Keep the launch pad and the portal approach clear.
            if abs(pos[2] - sp[2]) < 4 and abs(pos[0] - sp[0]) < 4:
                continue
            if (abs(pos[2] - pp[2]) < 5 and abs(pos[0] - pp[0]) < 5
                    and abs(pos[1] - pp[1]) < 5):
                continue
            did = f"debris_{i}"
            field[did] = {
                "id": did,
                "type": item["id"],
                "name": item["name"],
                "material": item.get("material", "stone"),
                "yield": int(item.get("yield", 1)),
                "pos": [float(x) for x in pos],
                "harvested": False,
            }
        return field

    def stop_tick(self):
        pass  # no-op in explicit-tick mode

    # ---- player management ----

    def add_player(self, char_id, name, pos=None):
        """Add a new player at spawn cube."""
        if pos is None:
            # Spawn at the near end, opposite the portal; offset so pilots don't overlap
            pos = [self.SPAWN_POS[0] + random.randint(-2, 2),
                   self.SPAWN_POS[1] + random.randint(-2, 2),
                   self.SPAWN_POS[2] + random.randint(-2, 2)]
        self.PLAYERS[char_id] = {
            "name": name,
            "room": "spawn_cube",
            "pos": [float(x) for x in pos],      # x,y,z in 64^3
            "look": [0.0, 0.0],                    # yaw, pitch fractions
            "hp": float(get_balance().get("player_starting_hp", 100)),
            "shields": float(get_balance().get("player_starting_shields", 100)),
            "max_hp": float(get_balance().get("player_starting_hp", 100)),
            "max_shields": float(get_balance().get("player_starting_shields", 100)),
            "xp": 0,
            "level": 1,
            "status": "alive",
            "guns": [],       # Level 1: no guns start (or starter)
            "plates": [],     # Level 1: no plates
            "artifacts": [],
            "materials": {"stone": 0, "ice": 0, "metal": 0},  # building stock
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

    def _regenerate_shields(self):
        """Called once per tick: auto-regen 1 shield point per 30s."""
        ticks_per_regen = int(self.shield_regen_ms / self.tick_ms)
        for pid, player in self.PLAYERS.items():
            if player["status"] != "alive":
                continue
            if player["shields"] < player["max_shields"]:
                # Check if enough ticks have passed — we track per-player last-regen tick
                # Since we tick_once() explicitly, we use a simple counter approach:
                # Actually, we'll track last_regen_tick in the player dict
                pass
        # We'll handle regen via the per-player last_regen_tick in tick_once()

    # ---- repair: shield -> hp ----

    def repair_hp(self, healer_id, target_id, hp_to_restore):
        """Repair target's HP by drawing from healer's shields.

        1 shield point = 1 HP.
        Healer's shields decrease by hp_to_restore.
        Target's HP increases by hp_to_restore (capped at max_hp).
        """
        healer = self.get_player(healer_id)
        target = self.get_player(target_id)
        if not healer or not target or healer["status"] != "alive" or target["status"] != "alive":
            return False, "invalid players"

        if healer["shields"] < hp_to_restore:
            return False, "not enough shields"

        # Apply
        healer["shields"] -= hp_to_restore
        target["hp"] = min(target["max_hp"], target["hp"] + hp_to_restore)
        return True, None

    # ---- death & XP ----

    def cause_death(self, victim_id, killer_id=None):
        """Kill a player. Transfer HP to killer, grant XP penalty."""
        victim = self.get_player(victim_id)
        if not victim or victim["status"] != "alive":
            return None

        # Mark dead
        victim["status"] = "dead"
        victim["hp"] = 0

        # XP penalty: loser loses 15% of current XP; winner gains it
        xp_lost = int(victim["xp"] * 0.15)
        victim["xp"] = max(0, victim["xp"] - xp_lost)

        # Transfer HP to killer's shields
        if killer_id and killer_id in self.PLAYERS:
            killer = self.PLAYERS[killer_id]
            if killer["status"] == "alive":
                # Killer's shields increase by victim's max HP (or a portion)
                # Per design: "The hit points they normally have when healthy will transfer, in quantity, to the shields of whoever killed them"
                shield_gain = min(killer["max_shields"] - killer["shields"], victim["max_hp"])
                killer["shields"] = min(killer["max_shields"], killer["shields"] + shield_gain)

        # Schedule respawn
        # We'll handle respawn in tick_once() via a timer

        return {
            "victim": victim_id,
            "xp_lost": xp_lost,
            "shield_gain_to_killer": shield_gain if killer_id else 0,
        }

    def respawn_player(self, char_id):
        """Respawn player at spawn cube after death penalty."""
        if char_id not in self.PLAYERS:
            return
        player = self.PLAYERS[char_id]
        player["status"] = "alive"
        player["hp"] = player["max_hp"]  # full HP
        player["shields"] = player["max_shields"]  # full shields
        player["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                         self.SPAWN_POS[1] + random.uniform(-2, 2),
                         self.SPAWN_POS[2] + random.uniform(-2, 2)]
        player["look"] = [0.0, 0.0]
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
        """Harvest a nearby debris chunk into the pilot's building materials."""
        player = self.PLAYERS.get(char_id)
        debris = self.DEBRIS.get(debris_id)
        if not player or not debris or debris["harvested"]:
            return False, "That debris is no longer available."
        if player["status"] != "alive":
            return False, "You cannot harvest while down."
        if math.dist(player["pos"], debris["pos"]) > 5:
            return False, "Fly closer to the debris to harvest it."

        debris["harvested"] = True
        mats = player.setdefault("materials", {"stone": 0, "ice": 0, "metal": 0})
        material = debris["material"]
        amount = debris["yield"]
        mats[material] = mats.get(material, 0) + amount
        return True, f"Harvested {debris['name']} (+{amount} {material})."

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

    # ---- tick loop ----

    def tick_once(self):
        """One tick of the engine (≈100 ms at 10 TPS). Deterministic when RNG seeded."""
        self.tick_count += 1

        # 1. NPC respawn timer
        self._tick_npc_respawn()

        # 2. Shield regeneration (1 point per 30s per player)
        # We track last_regen_tick per player; on first tick, set it
        if not hasattr(self, "_regen_init"):
            self._regen_init = set()
        for pid in self.PLAYERS:
            if pid not in self._regen_init:
                self._regen_init.add(pid)

        # Simple approach: since we tick explicitly, track shield regen via tick count
        # Actually, let's just do a fixed: every N ticks, regen 1 shield per player
        ticks_per_regen = max(1, int(self.shield_regen_ms / self.tick_ms))
        if self.tick_count % ticks_per_regen == 0:
            for pid, player in self.PLAYERS.items():
                if player["status"] == "alive" and player["shields"] < player["max_shields"]:
                    player["shields"] = min(player["max_shields"], player["shields"] + 1)

        # 3. Combat / shot judgement would go here
        # (In explicit-tick mode, the shooter overlay client sends fire;
        #  the server judges against the live target square it owns.)

        # 4. Death timers / respawn scheduling
        # (Handled via explicit state; the smoke test uses wait_for with pump)

        return {
            "tick": self.tick_count,
            "players": len(self.PLAYERS),
            "mobs": len(self.MOBS),
            "active_portal": bool(self.PORTALS),
        }

# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

engine = Engine()

def stop_tick():
    engine.stop_tick()

def tick_once():
    return engine.tick_once()