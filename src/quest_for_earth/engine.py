"""Engine: rooms, scanner, combat, shield/repair, NPC AI, affiliates, death.

Room-based server-authoritative tick loop for Level 1 (8 players, 64^3 cube).
All content in data/*.json; hot-reloads on change.
"""

import json
import math
import random
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_TICK_MS = 100          # 10 TPS
DEFAULT_SHIELD_REGEN_MS = 30000   # 1 point every 30 s
DEFAULT_DEATH_RESPAWN_TICKS = 50   # 5 s at 10 TPS
DEFAULT_NPC_RESPAWN_TICKS = 900    # 90 s at 10 TPS
DEFAULT_OFFLINE_PRUNE_SECONDS = 300  # 5 min; the world forgets a pilot no one has seen for this long

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
        # Alive-state of each mob, so tick_once can report roster changes.
        self._MOB_STATE = {}
        # Last tick position per pilot, for the speed measurement.
        self._LAST_POS = {}
        # Who is connected right now; the mob roster follows this.
        self.ONLINE = set()
        # Mutual PVP fights: char_id -> the other pilot we declared
        # (set by engage(); symmetric so a single handshake records it on both).
        self.FIGHTERS = {}
        # The last tick each pilot sent a movement intent, so the fight
        # autopilot only flies pilots who are not already flying themselves.
        self.LAST_MOVE = {}
        # Bolts in flight (D59): shot_id -> {pos, dir, damage, ...}.
        self.PROJECTILES = {}
        # The last tick each pilot took contact damage, so grinding against
        # a wall costs a beat every interval, not every tick (D59).
        self.LAST_COLLISION = {}
        # PVP kills (D59): victim_id -> killer_id. The downed pilot comes
        # back at the wormhole and the winner is returned to the spawn end,
        # so the rematch must fly the asteroid wall to re-engage.
        self.PVP_RESPAWN = {}

        # Initialise from world/balance
        self._reinit()

    def _reinit(self):
        """Reload world/balance and reset spawn-timer state."""
        w = get_world()

        # Reset players
        self.PLAYERS = {}

        # Reset mobs / respawn timers / weapon cooldowns / death timers
        self.MOBS = {}
        self.MOB_TIMERS = {}
        self.MOB_ATTACK_TICKS = {}
        self.DEATH_TIMERS = {}
        self.FIGHTERS = {}
        self.LAST_MOVE = {}
        self.PROJECTILES = {}
        self.LAST_COLLISION = {}
        self.PVP_RESPAWN = {}
        self._MOB_STATE = {}
        self._LAST_POS = {}

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
        # Level 1 is one continuous space: the two room records in the world
        # data describe its two ends (spawn and the wormhole), not separate
        # event rooms. Everything that exists here shares this room id, so
        # the room-scoped rule (D4) holds trivially until the world grows
        # real spaces.
        self.ROOM_ID = str(w.get("level_room") or "level_1")
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
        self._refresh_portal_guards()

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
        if not planned and not (cfg.get("placed") or []):
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
        # Explicitly placed chunks (D59): an obstacle wall and the loot that
        # sits on either side of it. Content lives in world.json, not here.
        for entry in (cfg.get("placed") or []):
            item = items.get(entry.get("type"))
            if not item:
                continue
            did = str(entry.get("id") or f"debris_placed_{len(field)}")
            size = int(entry.get("size") or 1)
            density = float(item.get("density", 1.0))
            field[did] = {
                "id": did,
                "type": item["id"],
                "name": item["name"],
                "material": item.get("material", "rock"),
                "density": density,
                "mass": density * size ** 3,
                "yield": int(item.get("yield", 1)),
                "pos": [float(x) for x in (entry.get("pos") or [32, 12, 6])],
                "size": size,
                "harvested": False,
            }
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
            if did in field:
                did = f"debris_{i}_{len(field)}"
            material = item.get("material", "stone")
            # Size is data-defined per chunk; density comes from the item.
            # Mass (density x volume) drives gravity and collision damage.
            sizes = cfg.get("sizes") or {}
            if item["id"] in sizes:
                size = int(sizes[item["id"]])
            else:
                size = (int(cfg.get("rock_size", 3)) if material == "rock" else 1)
            density = float(item.get("density", 1.0))
            field[did] = {
                "id": did,
                "type": item["id"],
                "name": item["name"],
                "material": material,
                "density": density,
                "mass": density * size ** 3,
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
            "room": self.ROOM_ID,
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
            # The pilot we have chosen to fly with (D15 affiliate verb).
            "affiliate": None,
            # Velocity and speed, measured by the server from the tick
            # displacement; used for collision damage.
            "vel": [0.0, 0.0, 0.0],
            "speed": 0.0,
            "push": [0.0, 0.0, 0.0],
            "guns": [],       # Level 1: no guns start (or starter)
            "plates": [],     # Level 1: no plates
            "artifacts": [],
        }
        # The roster follows the player count (one mob per player by default).
        self._ensure_mob_population()

    def set_pilot_offline(self, char_id):
        """A pilot that disconnected is not in the world any more.

        Their ship stays on record (so a reconnect resumes it) but the
        simulation must ignore them — otherwise a guard flies to a ghost and
        camps there.
        """
        player = self.get_player(char_id)
        if not player:
            return
        if player.get("status") == "alive":
            player["status"] = "offline"
        player["vel"] = [0.0, 0.0, 0.0]
        player["speed"] = 0.0
        player["push"] = [0.0, 0.0, 0.0]
        # Q11: stamp when they went silent so a later tick can retire the record.
        # Seconds, kept server-visible only; not broadcast, not a secret.
        player["offline_since"] = self.tick_count * self.tick_ms / 1000.0
        # A pilot who left is not fighting anybody any more (D58).
        self._break_fight(char_id)

    def set_pilot_online(self, char_id):
        """Bring a returning pilot back into the world."""
        player = self.get_player(char_id)
        if not player:
            return False
        if player.get("status") == "offline":
            player["status"] = "alive"
            self._LAST_POS[char_id] = list(player["pos"])
            # A pilot back in the world is no longer a stale record.
            player.pop("offline_since", None)
            return True
        return False

    def purge_offline(self, until=None):
        """Retire pilots who disconnected long ago (Q11).

        Any pilot who is offline and has been gone for more than
        `offline_prune_after_seconds` no longer answers the world's calls
        (guards never engage them, shots never land on them), so the row for
        them only grows stale. Cut it: the ship and its salvage stay as public
        loot, but the row itself is gone from `PLAYERS` so the roster stops
        gaining one entry per disconnect per session.

        Never touches a fighting or alive pilot (D2 server-authoritative),
        never touches salvage or the revive timer. `until` lets a test inject
        an arbitrary clock.
        """
        # Seconds: when silence began (since) vs the wall clock (now, or a
        # test-injected limit). Both real seconds on the same axis.
        now = self.tick_count * self.tick_ms / 1000.0 if until is None else until
        # `offline_prune_after_ms` is milliseconds; the clock below is seconds.
        data_seconds = float(get_balance().get("offline_prune_after_ms", DEFAULT_OFFLINE_PRUNE_SECONDS * 1000)) / 1000.0
        purged = []
        for cid in list(self.PLAYERS.keys()):
            # Offline-only rows are the leak. A dead pilot is the revive path
            # (D2) and never carries this stamp, so only silence survives here.
            if self.PLAYERS[cid].get("status") != "offline":
                continue
            since = self.PLAYERS[cid].get("offline_since")
            if since is None:  # never disconnected, or already pruned
                continue
            # Stale only if gone past the limit *before* the cut. Cut it now.
            if now - since >= data_seconds:
                del self.PLAYERS[cid]
                purged.append(cid)
        return purged

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
            "room": self.ROOM_ID,
            "pos": [float(x) for x in pos],
            # The post this mob returns to when nobody is engaging it.
            "home": [float(x) for x in pos],
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

    def set_online_pilots(self, ids):
        """Tell the engine who is connected right now.

        The mob roster follows the pilots who are actually in the world —
        otherwise every pilot who ever joined would leave a guard behind.
        """
        self.ONLINE = set(ids or [])
        self._ensure_mob_population()

    def _ensure_mob_population(self):
        """Keep the roster at one mob per connected pilot (D1).

        The count comes from the mob spawn defaults and the end room's
        `spawn_count_per_player`. Stable ids mean a killed mob respawns on
        its own timer instead of doubling the roster, and pilots who leave
        take their guard with them.
        """
        spawn_defaults = get_mobs_template().get("spawn_defaults") or {}
        room = get_world().get("rooms", {}).get("end_cube", {})
        per_player = int(spawn_defaults.get("count_per_player")
                         or room.get("spawn_count_per_player") or 1)
        online = len(self.ONLINE) if self.ONLINE else max(1, len(self.PLAYERS))
        target = max(1, per_player * online)
        for _ in range(max(0, target - len(self.MOBS))):
            n = 0
            while f"mob_{n}" in self.MOBS:
                n += 1
            self.add_mob(f"mob_{n}")
        # The wormhole is sealed while a guardian stands at it.
        self._refresh_portal_guards()
        # Retire the surplus when fewer pilots are connected.
        while len(self.MOBS) > target:
            victim = max(self.MOBS,
                         key=lambda mid: math.dist(self.MOBS[mid]["pos"], self.PORTAL_POS))
            self.remove_mob(victim)

    def remove_mob(self, mob_id):
        if mob_id in self.MOBS:
            del self.MOBS[mob_id]
        if mob_id in self.MOB_TIMERS:
            del self.MOB_TIMERS[mob_id]
        self._refresh_portal_guards()

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
        """Launch one authoritative shot at an NPC mob or another pilot.

        The shot is a projectile: it flies straight at where the target was
        when the gun went off, so a moving target has to be led (D59). The
        server owns the flight and the impact (D2); the client only asks to
        fire. Range and cooldown come from balance.json, never from the
        client. Returns (ok, reason, shot).
        """
        shooter = self.get_player(shooter_id)
        if not shooter or shooter["status"] != "alive":
            return False, "invalid shooter", None
        target = self.MOBS.get(target_id)
        is_mob = target is not None
        is_debris = False
        if not is_mob:
            target = self.get_player(target_id)
        if not target and target_id in self.DEBRIS:
            target = self.DEBRIS.get(target_id)
            is_debris = target is not None
            if target and (target.get("harvested") or target.get("destroyed")):
                target = None
                is_debris = False
        if not target:
            return False, "invalid target", None
        if is_mob:
            # A mob that is already down is not a target (D58).
            if not target.get("alive", True):
                return False, "invalid target", None
        elif is_debris:
            pass  # debris is always a valid practice target
        elif target.get("status", "alive") != "alive":
            # Downed or offline pilots cannot be shot (a ghost is not a target).
            return False, "invalid target", None
        if shooter_id == target_id:
            return False, "you cannot shoot yourself", None

        balance = get_balance()
        if is_mob:
            rng = float(balance.get("fire_range", 24))
            damage = float(balance.get("fire_damage", 6))
            cooldown_ms = int(balance.get("fire_cooldown_ms", 400))
        elif is_debris:
            # Practice shots at rocks: no damage, but the gun still cycles.
            rng = float(balance.get("fire_range", 24))
            damage = 0.0
            cooldown_ms = int(balance.get("fire_cooldown_ms", 400))
        else:
            # Pilot-vs-pilot is the fighting slice (D55): its own range and
            # damage, tuned from balance.json.
            rng = float(balance.get("fire_range_pilot", 22))
            damage = float(balance.get("fire_damage_pilot", 8))
            cooldown_ms = int(balance.get("fire_cooldown_pilot_ms", 500))
        if math.dist(shooter["pos"], target["pos"]) > rng:
            return False, "target out of range", None
        cooldown = max(1, cooldown_ms) / max(1, self.tick_ms)
        last = self.LAST_FIRE.get(shooter_id, -1e9)
        if self.tick_count - last < cooldown:
            return False, "gun still cycling", None
        self.LAST_FIRE[shooter_id] = self.tick_count

        # Aim at where the target is now; the bolt flies straight and only
        # hits if the target is still there when it arrives (D59).
        sx, sy, sz = shooter["pos"]
        tx, ty, tz = target["pos"]
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        direction = [dx / dist, dy / dist, dz / dist]
        shot_id = f"shot_{self.tick_count}_{shooter_id}"
        shot = {
            "id": shot_id,
            "from": shooter_id,
            "target": target_id,
            "target_kind": "mob" if is_mob else ("debris" if is_debris else "pilot"),
            "damage": damage,
            "pos": [sx, sy, sz],
            "dir": direction,
            "travelled": 0.0,
        }
        self.PROJECTILES[shot_id] = shot
        return True, "fired", shot

    # ---- travelling shots (D59) ----

    def _tick_projectiles(self):
        """Advance every bolt and resolve its impact.

        A bolt hits a body that is inside the hit radius when it arrives and
        fizzes when it flies past its range or leaves the level. Damage is
        applied on arrival: shields absorb first, the remainder hits the hull
        (D2).
        """
        balance = get_balance()
        speed = float(balance.get("projectile_speed", 55))
        hit_radius = float(balance.get("projectile_hit_radius", 1.8))
        max_range = float(balance.get("projectile_max_range", 60))
        seconds = max(0.001, self.tick_ms / 1000.0)
        step = speed * seconds
        events = []
        for shot_id, shot in list(self.PROJECTILES.items()):
            old_pos = list(shot["pos"])
            pos = [old_pos[0] + shot["dir"][0] * step,
                   old_pos[1] + shot["dir"][1] * step,
                   old_pos[2] + shot["dir"][2] * step]
            shot["pos"] = pos
            shot["travelled"] += step

            # Swept hit test: a bolt that flies past a body between two ticks
            # still hits it (D59) — otherwise a close-range shot overshoots.
            def seg_dist(p0, p1, t):
                d = [p1[i] - p0[i] for i in range(3)]
                seg2 = sum(x * x for x in d)
                if seg2 <= 0:
                    return math.dist(t, p0)
                tt = max(0.0, min(1.0, sum((t[i] - p0[i]) * d[i] for i in range(3)) / seg2))
                proj = [p0[i] + tt * d[i] for i in range(3)]
                return math.dist(t, proj)

            # The bolt was aimed at a specific target: that body is the
            # primary candidate (D59). Other bodies only count if they
            # actually block the path (within the hit radius and closer).
            hit_id = None
            hit_kind = None
            hit_obj = None
            best = hit_radius
            intended = shot.get("target")
            # Intended target first (so a stray mob cannot steal the hit).
            if intended in self.MOBS and self.MOBS[intended].get("alive"):
                d = seg_dist(old_pos, pos, self.MOBS[intended]["pos"])
                if d <= best:
                    best, hit_id, hit_kind, hit_obj = d, intended, "mob", self.MOBS[intended]
            elif intended in self.PLAYERS and self.PLAYERS[intended].get("status") == "alive":
                d = seg_dist(old_pos, pos, self.PLAYERS[intended]["pos"])
                if d <= best:
                    best, hit_id, hit_kind, hit_obj = d, intended, "pilot", self.PLAYERS[intended]
            elif intended in self.DEBRIS:
                _deb = self.DEBRIS[intended]
                if not _deb.get("harvested") and not _deb.get("destroyed"):
                    d = seg_dist(old_pos, pos, _deb["pos"])
                    if d <= best:
                        best, hit_id, hit_kind, hit_obj = d, intended, "debris", _deb
            # Then any other body that the bolt actually passes through.
            for mid, mob in self.MOBS.items():
                if not mob.get("alive") or mid == intended:
                    continue
                d = seg_dist(old_pos, pos, mob["pos"])
                if d <= best:
                    best, hit_id, hit_kind, hit_obj = d, mid, "mob", mob
            for pid, player in self.PLAYERS.items():
                if pid == shot["from"] or player.get("status") != "alive" or pid == intended:
                    continue
                d = seg_dist(old_pos, pos, player["pos"])
                if d <= best:
                    best, hit_id, hit_kind, hit_obj = d, pid, "pilot", player

            if hit_obj is not None:
                self.PROJECTILES.pop(shot_id, None)
                # Practice shots at debris do no damage and kill nothing.
                if hit_kind == "debris":
                    events.append({"kind": "shot", "from": shot["from"],
                                   "target": hit_id, "target_kind": "debris",
                                   "damage": 0.0, "pos": list(pos)})
                    continue
                damage = float(shot["damage"])
                absorbed = min(float(hit_obj.get("shields") or 0), damage)
                hit_obj["shields"] = float(hit_obj.get("shields") or 0) - absorbed
                hull = damage - absorbed
                hit_obj["hp"] = max(0, float(hit_obj.get("hp") or 0) - hull)
                event = {
                    "kind": "shot", "from": shot["from"], "target": hit_id,
                    "target_kind": hit_kind, "damage": damage,
                    "shields": float(hit_obj["shields"]), "hp": float(hit_obj["hp"]),
                    "pos": list(pos),
                }
                if hit_obj["hp"] <= 0:
                    if hit_kind == "mob":
                        self._kill_mob(hit_id, shot["from"])
                    else:
                        self.cause_death(hit_id, shot["from"])
                        # D59: mark the pair for a split rematch, and let
                        # the kill advance the level objective.
                        self.PVP_RESPAWN[hit_id] = shot["from"]
                        credit = float(get_balance().get("pvp_guard_credit", 1))
                        portal = self.PORTALS.get("end_portal")
                        if portal is not None and not self.portal_is_open():
                            portal["pvp_credit"] = \
                                float(portal.get("pvp_credit") or 0) + credit
                    event["dead"] = True
                events.append(event)
                continue

            # No hit: the bolt keeps flying until it runs out of range.
            if (shot["travelled"] >= max_range
                    or any(c < 0 or c > 63 for c in pos)):
                self.PROJECTILES.pop(shot_id, None)
                events.append({"kind": "miss", "from": shot["from"],
                               "target": shot["target"], "pos": list(pos)})
        return events

    # ---- pilot-vs-pilot fighting (D55) ----

    def engage(self, char_id, target_id):
        """Declare a mutual fight between two pilots (the agreed pair).

        Both pilots end up fighting each other (stored symmetrically) as
        long as they are both alive and within the PVP proximity radius.
        Returns (ok, reason). Engaging is an explicit handshake so a fight
        is visible on both sides before the first shot.
        """
        me = self.get_player(char_id)
        other = self.get_player(target_id)
        if not me or not other or me["status"] != "alive" or other["status"] != "alive":
            return False, "invalid pilots"
        if char_id == target_id:
            return False, "you cannot fight yourself"
        radius = float(get_balance().get("pvp_proximity", 40))
        if math.dist(me["pos"], other["pos"]) > radius:
            return False, "outside fighting range"
        self.FIGHTERS[char_id] = target_id
        self.FIGHTERS[target_id] = char_id
        return True, None

    def disengage(self, char_id):
        """End the mutual fight (if any) from either side of the pair."""
        self.FIGHTERS.pop(char_id, None)
        # Break the symmetric reference if it pointed back at this pilot.
        for other_id, other in list(self.FIGHTERS.items()):
            if other_id != char_id and other == char_id:
                self.FIGHTERS.pop(other_id, None)
        return True, None

    def _break_fight(self, pilot_id):
        """Break every fight a pilot is in (they left the world, D58)."""
        for other_id, target in list(self.FIGHTERS.items()):
            if other_id == pilot_id or target == pilot_id:
                self.FIGHTERS.pop(other_id, None)

    # ---- death & loot ----

    def _drop_loot(self, actor):
        """Salvage falls out of a dead agent where it died (D15).

        The entry keeps the room it fell in and the position it fell at, so
        a pilot can fly to the wreck and grab it.
        """
        rule = get_items().get("salvage_on_death") or {}
        loot = self.ROOM_LOOT.setdefault(actor.get("room") or self.ROOM_ID, [])
        dropped = []
        if int(rule.get("artifacts") or 0) <= 0:
            return dropped
        items_map = {it.get("id"): it for it in get_items().get("items", []) if it}
        art = items_map.get("energy_cell") or {"id": "salvage_1", "name": "Salvage",
                                              "type": "salvage", "rarity": "common"}
        entry = dict(art)
        entry["count"] = int(rule.get("artifacts") or 1)
        entry["dropped_by"] = (actor.get("name") or actor.get("type") or "?")
        entry["pos"] = [float(x) for x in (actor.get("pos") or [0, 0, 0])]
        loot.append(entry)
        dropped.append(entry)
        return dropped

    def _refresh_portal_guards(self):
        """The wormhole is guarded by every living NPC.

        Counted in one place so a kill, a respawn or a roster change cannot
        leave the seal out of step with the field.
        """
        portal = self.PORTALS.get("end_portal")
        if portal is None:
            return
        portal["enemies_remaining"] = len([m for m in self.MOBS.values()
                                            if m.get("alive")])

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
        # The wormhole is sealed while a guardian stands at it.
        self._refresh_portal_guards()

    def portal_is_open(self):
        """True when nothing guards the wormhole any more."""
        portal = self.PORTALS.get("end_portal")
        if not portal:
            return False
        # PVP kills advance the level objective (D59): each one is worth
        # pvp_guard_credit guards, so the seal opens early if the pilots
        # settle their own scores while the field still stands.
        guards = int(portal.get("enemies_remaining") or 0)
        credit = float(portal.get("pvp_credit") or 0)
        return (guards - credit) <= 0

    def cause_death(self, victim_id, killer_id=None):
        """Kill a pilot. Transfer HP to killer, award XP, drop salvage."""
        victim = self.get_player(victim_id)
        if not victim or victim["status"] != "alive":
            return None

        victim["status"] = "dead"
        victim["hp"] = 0
        # A fight survives a death (D58): the pair rematches when the downed
        # pilot respawns. Only leaving the world breaks it.
        self.DEATH_TIMERS[victim_id] = int(get_balance().get("death_respawn_ticks",
                                                                 DEFAULT_DEATH_RESPAWN_TICKS))

        # XP penalty: the loser loses a slice of current XP; the winner takes
        # a bounty plus a share of what the loser actually carried.
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
                # The bounty comes from balance.json, not from a magic number
                # in the code (D1/D25).
                bounty = int(get_balance().get("xp_kill_player_base", 10))
                share = int(victim.get("xp") or 0) * int(get_balance().get("xp_kill_player_percent", 15)) // 100
                self._award_xp(killer_id, bounty + share)
                result["xp_to_killer"] = bounty + share

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
        """Grant XP and roll the level-up ladder (data-driven thresholds).

        A pilot who is affiliated with somebody shares in what that partner
        earns: the follower gets a cut of the leader's gains.
        """
        player = self.get_player(char_id)
        if not player or player["status"] != "alive":
            return
        gained = int(amount or 0)
        player["xp"] = int(player.get("xp") or 0) + gained
        share_pct = int(get_balance().get("affiliate_share_percent", 25))
        if share_pct > 0:
            for mate_id, mate in self.PLAYERS.items():
                if mate_id == char_id or mate.get("affiliate") != char_id:
                    continue
                if mate["status"] != "alive":
                    continue
                share = gained * share_pct // 100
                if share > 0:
                    mate["xp"] = int(mate.get("xp") or 0) + share
        per_level = float(get_balance().get("xp", {}).get("per_level") or 100)
        while player["xp"] >= per_level * int(player.get("level") or 1):
            player["level"] = int(player.get("level") or 1) + 1
            self._apply_level_bonuses(player)

    def affiliate(self, char_id, target_id):
        """Trust another pilot: follow them and share what they gain.

        The affiliate is the pilot we fly with; a share of anything they
        earn comes back to us (D15 squad verb). Returns (ok, reason).
        """
        player = self.get_player(char_id)
        target = self.get_player(target_id)
        if not player:
            return False, "Unknown pilot."
        if not target:
            return False, "No such pilot."
        if target_id == char_id:
            return False, "You cannot affiliate with yourself."
        if target["status"] != "alive":
            return False, "That pilot is down."
        player["affiliate"] = target_id
        return True, None

    def break_affiliate(self, char_id):
        """Stop following a pilot."""
        player = self.get_player(char_id)
        if not player:
            return False, "Unknown pilot."
        player["affiliate"] = None
        return True, None

    def share_chart(self, char_id, target_id):
        """Share this pilot's scan contacts with a wingman.

        Knowledge decays, but a wingman's chart is worth sharing: the
        target receives the contacts as they were last scanned.
        Returns (ok, reason, hits).
        """
        player = self.get_player(char_id)
        target = self.get_player(target_id)
        if not player:
            return False, "Unknown pilot.", []
        if not target:
            return False, "No such pilot.", []
        if target_id == char_id:
            return False, "You cannot share a chart with yourself.", []
        if target["status"] != "alive":
            return False, "That pilot is down.", []
        radius = float(get_balance().get("scan_radius") or 28.0)
        hits = []
        for did, d in self.DEBRIS.items():
            if d.get("harvested"):
                continue
            if math.dist(player["pos"], d.get("pos") or [0, 0, 0]) > radius:
                continue
            hits.append({"kind": "debris", "id": did, "name": d.get("name") or did,
                         "mass": float(d.get("mass") or 0.0),
                         "size": int(d.get("size") or 1),
                         "material": d.get("material") or "rock"})
        return True, None, hits

    def use_artifact(self, char_id, item_id):
        """Use a carried artifact — an energy cell repairs the shields.

        The artifact's value is its repair amount, so this stays data-driven
        (D1). Returns (ok, reason).
        """
        player = self.get_player(char_id)
        if not player:
            return False, "Unknown pilot."
        if player["status"] != "alive":
            return False, "You cannot use that while down."
        hold = list(player.get("artifacts") or [])
        if item_id not in hold:
            return False, "You are not carrying that."
        if player["shields"] >= player["max_shields"]:
            return False, "Your shields are already full."
        amount = 0
        for art in get_items().get("items", []):
            if art.get("id") == item_id:
                amount = int(art.get("value") or 0)
                break
        if amount <= 0:
            return False, "That artifact cannot be used."
        hold.remove(item_id)
        player["artifacts"] = hold
        player["shields"] = min(float(player["max_shields"]),
                                float(player["shields"]) + amount)
        return True, f"Energy cell used (+{amount} shields)."

    def take_loot(self, char_id, item_id):
        """Grab salvage lying nearby (D15 salvage loop).

        Salvage stays where it fell; a pilot has to fly to the wreck and be
        within the nearby radius to pick it up. The server decides, and
        several wrecks can hold the same item — the nearest one wins.
        """
        player = self.get_player(char_id)
        if not player:
            return False, "Unknown pilot."
        radius = float(get_balance().get("nearby_radius", 5))
        best = None
        best_list = None
        best_dist = None
        found = False
        for loot in list(self.ROOM_LOOT.values()):
            for entry in list(loot or []):
                if entry.get("id") != item_id:
                    continue
                found = True
                pos = entry.get("pos")
                dist = math.dist(player["pos"], pos) if pos else 0.0
                if dist > radius:
                    continue
                if best_dist is None or dist < best_dist:
                    best, best_list, best_dist = entry, loot, dist
        if best is not None:
            best_list.remove(best)
            player["artifacts"] = list(player.get("artifacts") or []) + [item_id]
            return True, f"Picked up {best.get('name') or item_id}."
        if found:
            return False, "Fly closer to the salvage to grab it."
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

    def scan(self, char_id):
        """Local scan: reveal nearby unharvested debris and living mobs.

        Chart knowledge decays with the scan TTL (the world moves, D1); the
        pilot re-scans to refresh. Returns (ok, reason, hits).
        """
        player = self.get_player(char_id)
        if not player or player["status"] != "alive":
            return False, "You cannot scan while down.", []
        balance = get_balance()
        radius = float(balance.get("scan_radius") or 18.0)
        ttl_ms = int(balance.get("scan_ttl_ms") or 4000)
        ttl_ticks = max(1, ttl_ms) / max(1, self.tick_ms)
        hits = []
        for did, d in self.DEBRIS.items():
            if d.get("harvested"):
                continue
            if math.dist(player["pos"], d.get("pos") or [0, 0, 0]) > radius:
                continue
            hits.append({"kind": "debris", "id": did, "name": d.get("name") or did,
                         "mass": float(d.get("mass") or 0.0),
                         "size": int(d.get("size") or 1),
                         "material": d.get("material") or "rock"})
        for mid, m in self.MOBS.items():
            if not m.get("alive") or m.get("status", "alive") != "alive":
                continue
            if math.dist(player["pos"], m.get("pos") or [0, 0, 0]) > radius:
                continue
            hits.append({"kind": "mob", "id": mid, "name": m.get("name") or mid})
        player["scan_until"] = int(self.tick_count) + int(ttl_ticks)
        return True, None, hits

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
        # Split respawn (D59): a pilot downed in a PVP fight comes back at
        # the wormhole end while the winner is returned to the spawn end,
        # so both must fly the asteroid wall before they can re-engage.
        killer_id = self.PVP_RESPAWN.pop(char_id, None)
        if killer_id is not None:
            offset = float(get_balance().get("pvp_respawn_offset", 3.0))
            player["pos"] = [self.PORTAL_POS[0] + random.uniform(-2, 2),
                             self.PORTAL_POS[1] + random.uniform(-2, 2),
                             self.PORTAL_POS[2] - offset]
            winner = self.PLAYERS.get(killer_id)
            if winner and winner.get("status") == "alive":
                winner["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                                 self.SPAWN_POS[1] + random.uniform(-2, 2),
                                 self.SPAWN_POS[2] + random.uniform(-2, 2)]
                winner["vel"] = [0.0, 0.0, 0.0]
                winner["speed"] = 0.0
                winner["push"] = [0.0, 0.0, 0.0]
                self._LAST_POS[killer_id] = list(winner["pos"])
        else:
            player["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                             self.SPAWN_POS[1] + random.uniform(-2, 2),
                             self.SPAWN_POS[2] + random.uniform(-2, 2)]
        player["look"] = [0.0, 0.0]
        player["evade_cooldown"] = -1e9
        # Respawning is a teleport: the next tick must not read the jump as
        # a colossal velocity (which would fake a collision damage hit).
        player["vel"] = [0.0, 0.0, 0.0]
        player["speed"] = 0.0
        player["push"] = [0.0, 0.0, 0.0]
        self._LAST_POS[char_id] = list(player["pos"])
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
        if not self.portal_is_open():
            return False, "The wormhole is sealed while a guardian stands at it."
        ok, reason = self.can_enter_portal(char_id)
        if not ok:
            return False, reason
        player = self.PLAYERS[char_id]
        player["pos"] = [self.SPAWN_POS[0] + random.uniform(-2, 2),
                         self.SPAWN_POS[1] + random.uniform(-2, 2),
                         self.SPAWN_POS[2] + random.uniform(-2, 2)]
        # Slipping through the wormhole is worth something.
        bonus = int(get_balance().get("level_complete_xp", 0))
        if bonus:
            self._award_xp(char_id, bonus)
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

    # ---- movement (intent in, authoritative position out) ----

    def apply_move(self, char_id, vec=None, dir_="none"):
        """Apply one movement intent and return the new position.

        The client only expresses intent (D2): the engine clamps the result
        to the cube and owns the position. A downed pilot cannot fly.
        """
        player = self.get_player(char_id)
        if not player or player.get("status") != "alive":
            return None
        # A manual intent silences the fight autopilot for this pilot.
        self.LAST_MOVE[char_id] = self.tick_count
        x, y, z = player["pos"]
        if isinstance(vec, (list, tuple)) and len(vec) == 3:
            try:
                vx, vy, vz = (float(v) for v in vec)
            except (TypeError, ValueError):
                vx = vy = vz = 0.0
            length = math.sqrt(vx * vx + vy * vy + vz * vz)
            if length > 0:
                step = float(get_balance().get("thrust_step", 1.0))
                vx, vy, vz = (vx / length * step,
                              vy / length * step,
                              vz / length * step)
            x = max(0, min(63, x + vx))
            y = max(0, min(63, y + vy))
            z = max(0, min(63, z + vz))
        else:
            if dir_ == "north": z = min(63, z + 1)
            elif dir_ == "south": z = max(0, z - 1)
            elif dir_ == "east": x = min(63, x + 1)
            elif dir_ == "west": x = max(0, x - 1)
            elif dir_ == "up": y = min(63, y + 1)
            elif dir_ == "down": y = max(0, y - 1)
        player["pos"] = [x, y, z]
        return list(player["pos"])

    # ---- fight autopilot (D56) ----

    def _pvp_autopilot(self):
        """Fly each engaged pilot toward their fight partner.

        The server owns the flight (D2): a pilot who has not sent a move
        intent for the configured idle window glides toward the other side
        of the fight and holds at the standoff distance, so a two-player
        dogfight converges without anybody flying it by hand. A pilot who
        is flying themselves is never touched (D26 — intent wins).
        """
        balance = get_balance()
        if not bool(balance.get("pvp_autopilot", False)):
            return []
        speed = float(balance.get("pvp_autopilot_speed", 0.12))
        standoff = float(balance.get("pvp_autopilot_standoff", 14.0))
        idle_ticks = max(1, int(balance.get("pvp_autopilot_idle_ms", 1200))
                         // max(1, self.tick_ms))
        moved = []
        for pid, target_id in self.FIGHTERS.items():
            me = self.PLAYERS.get(pid)
            other = self.PLAYERS.get(target_id)
            if not me or not other or me["status"] != "alive" or other["status"] != "alive":
                continue
            if self.tick_count - self.LAST_MOVE.get(pid, -1e9) < idle_ticks:
                continue
            px, py, pz = me["pos"]
            ox, oy, oz = other["pos"]
            dx, dy, dz = ox - px, oy - py, oz - pz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist <= max(0.001, standoff):
                continue  # already in the fight; the guns are the answer
            step = min(speed, dist - standoff)
            me["pos"] = [
                max(0, min(63, px + dx / dist * step)),
                max(0, min(63, py + dy / dist * step)),
                max(0, min(63, pz + dz / dist * step)),
            ]
            moved.append(pid)
        return moved

    # ---- rock collisions: bounce damage and head-on blasts ----

    def _rock_collisions(self):
        """Resolve hard contact with debris.

        A glancing hit bounces the ship and hurts in proportion to the
        chunk's mass and the closing speed. A head-on impact at full speed
        into a heavy enough rock blows it up.
        """
        balance = get_balance()
        ship_radius = float(balance.get("ship_radius", 0.9))
        density = float(balance.get("rock_density", 1.0))
        damage_k = float(balance.get("collision_damage", 0.08))
        blast_speed = float(balance.get("explosion_speed", 9.0))
        blast_mass = float(balance.get("explosion_min_mass", 12.0))
        blast_damage = float(balance.get("explosion_damage", 45.0))
        blast_radius = float(balance.get("explosion_radius", 12.0))

        chunks = [d for d in self.DEBRIS.values()
                  if not d.get("harvested") and not d.get("destroyed")]
        if not chunks:
            return []

        events = []
        for pid, player in self.PLAYERS.items():
            if player["status"] != "alive":
                continue
            vel = player.get("vel") or [0.0, 0.0, 0.0]
            speed = float(player.get("speed") or 0.0)
            for chunk in chunks:
                if chunk.get("destroyed") or chunk.get("harvested"):
                    continue
                radius = ship_radius + float(chunk.get("size", 1)) * 0.55
                dx = player["pos"][0] - chunk["pos"][0]
                dy = player["pos"][1] - chunk["pos"][1]
                dz = player["pos"][2] - chunk["pos"][2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                if dist >= radius:
                    continue
                if dist < 1e-4:
                    dx, dy, dz, dist = 1.0, 0.0, 0.0, 1.0
                nx, ny, nz = dx / dist, dy / dist, dz / dist
                # Closing speed: positive when the ship is driving into the rock.
                closing = -(vel[0] * nx + vel[1] * ny + vel[2] * nz)
                mass = float(chunk.get("mass") or
                             (density * float(chunk.get("size", 1)) ** 3))
                damage = 0.0
                exploded = False
                # A scrape against a huge wall rock cannot cost more than
                # this per contact (D59): the formula was tuned for small
                # debris, and mass 729 turned a slow graze into 70+ damage.
                damage_cap = float(balance.get("collision_damage_cap", 20.0))
                # ...and grinding against it while holding thrust costs a
                # beat every interval, not every tick (per pilot+chunk, so a
                # burst through several small chunks still costs each one).
                interval = max(0, int(balance.get(
                    "collision_damage_interval_ms", 500))) / max(1, self.tick_ms)
                contact_key = f"{pid}|{chunk.get('id')}"
                if self.tick_count - self.LAST_COLLISION.get(contact_key, -1e9) < interval:
                    if closing > 0 and not (speed >= blast_speed and mass >= blast_mass):
                        continue          # bounce out, but no repeated chip damage
                if closing > 0:
                    self.LAST_COLLISION[contact_key] = self.tick_count
                    damage = min(damage_k * mass * closing, damage_cap)
                    # Head-on: the velocity points into the rock, not across it.
                    lateral = math.sqrt(max(0.0, speed * speed - closing * closing))
                    head_on = lateral <= 0.35 * closing
                    if head_on and speed >= blast_speed and mass >= blast_mass:
                        exploded = True
                        damage = blast_damage
                        chunk["destroyed"] = True
                        chunk["harvested"] = True
                        # The blast catches everyone nearby.
                        for other_id, other in self.PLAYERS.items():
                            if other["status"] != "alive":
                                continue
                            odist = math.dist(other["pos"], chunk["pos"])
                            if odist > blast_radius:
                                continue
                            falloff = 1.0 - (odist / blast_radius)
                            self._apply_damage(other_id, other,
                                               blast_damage * 0.5 * falloff)
                if damage > 0:
                    self._apply_damage(pid, player, damage)
                # Bounce: push the ship back out along the contact normal.
                push = radius - dist + 0.05
                player["pos"] = [
                    max(0.0, min(63.0, player["pos"][0] + nx * push)),
                    max(0.0, min(63.0, player["pos"][1] + ny * push)),
                    max(0.0, min(63.0, player["pos"][2] + nz * push)),
                ]
                # The bounce is a velocity change, not thrust: keep it out of
                # the next tick's speed measurement.
                player["push"] = [nx * push, ny * push, nz * push]
                events.append({
                    "pilot": pid, "rock": chunk["id"],
                    "name": chunk.get("name") or chunk["id"],
                    "damage": round(damage, 1), "explosion": exploded,
                    "mass": mass, "speed": round(speed, 1),
                })
        return events

    def _apply_damage(self, char_id, player, damage):
        """Shields absorb first, the remainder hits the hull (D2 order)."""
        damage = max(0.0, float(damage))
        absorbed = min(float(player.get("shields") or 0), damage)
        player["shields"] = float(player.get("shields") or 0) - absorbed
        player["hp"] = max(0.0, float(player.get("hp") or 0) - (damage - absorbed))
        if player["hp"] <= 0 and char_id:
            self.cause_death(char_id, None)
        return damage

    # ---- per-second resource conversion (D25) ----

    def _nearest_deficit(self, pid, player, key, radius):
        """Another living pilot in range with room in the named store."""
        cap_key = "max_" + key
        for other_id, other in self.PLAYERS.items():
            if other_id == pid or other["status"] != "alive":
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
        for pid, player in self.PLAYERS.items():
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
                mate = self._nearest_deficit(pid, player, "shields", radius)
                if mate:
                    step = min(rate, player["energy"], mate["max_shields"] - mate["shields"])
                    player["energy"] -= step
                    mate["shields"] += step

            if player["metal"] > 0 and player["hp"] < player["max_hp"]:
                step = min(rate, player["metal"], player["max_hp"] - player["hp"])
                player["metal"] -= step
                player["hp"] += step
            elif player["metal"] > 0:
                mate = self._nearest_deficit(pid, player, "hp", radius)
                if mate:
                    step = min(rate, player["metal"], mate["max_hp"] - mate["hp"])
                    player["metal"] -= step
                    mate["hp"] += step

    # ---- rock gravity (D25) ----

    def _apply_gravity(self):
        """Drift pilots toward nearby debris; return ids that moved.

        The pull follows the chunk's mass (density x volume): a big dense
        metal ore bends a passing fighter's path, while a light methane
        chunk barely registers (D25).
        """
        balance = get_balance()
        radius = float(balance.get("gravity_radius", 8))
        strength = float(balance.get("gravity_strength", 0.02))
        cap = float(balance.get("gravity_pull_cap", 0.25))
        rocks = [d for d in self.DEBRIS.values()
                 if not d["harvested"] and not d.get("destroyed")]
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
                mass = float(rock.get("mass") or 0.0)
                if mass <= 0:
                    continue
                pull = min(cap, strength * mass / (dist * dist))
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

    def _mob_state_changes(self):
        """Report mobs whose alive state changed since the last tick.

        Clients keep their own enemy list; without this they would keep
        showing a mob that was just destroyed (or miss a respawn).
        """
        state = {}
        changed = []
        for mid, m in self.MOBS.items():
            alive = bool(m.get("alive"))
            state[mid] = alive
            if self._MOB_STATE.get(mid) != alive:
                changed.append(mid)
        self._MOB_STATE = state
        return changed

    def _track_velocity(self):
        """Measure each pilot's speed from the displacement over one tick.

        The server owns the position, so the tick displacement is the honest
        measure of how fast the ship is actually travelling (D2).
        """
        seconds = max(0.001, self.tick_ms / 1000.0)
        for pid, player in self.PLAYERS.items():
            prev = self._LAST_POS.get(pid)
            if prev is not None:
                push = player.get("push") or [0.0, 0.0, 0.0]
                player["push"] = [0.0, 0.0, 0.0]
                net = [player["pos"][i] - prev[i] - push[i] for i in range(3)]
                player["vel"] = [net[i] / seconds for i in range(3)]
                player["speed"] = math.sqrt(sum(c * c for c in net)) / seconds
            self._LAST_POS[pid] = list(player["pos"])

    def _vitals_snapshot(self):
        # Everything the HUD shows about a pilot. Rank, the hold and the
        # store caps belong here too: if they are not tracked, a change to
        # them is never broadcast and the HUD silently goes stale.
        return {
            pid: (p["hp"], p["shields"], p["energy"], p["methane"], p["metal"],
                  p.get("max_hp"), p.get("max_shields"), p.get("level"),
                  p.get("xp"), tuple(p.get("artifacts") or []))
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

    def _npc_movement(self):
        """Mobs close on the nearest engaged pilot; otherwise they hold station.

        Returns the ids of mobs that moved so the web layer can broadcast
        their new positions (D2 — the client never moves a mob itself).
        """
        balance = get_balance()
        mob_cfg = get_mobs_template().get("spawn_defaults") or {}
        aggro = float(mob_cfg.get("aggro_distance") or balance.get("npc_aggro_distance", 15))
        speed = float(balance.get("npc_speed", 0.35))
        if speed <= 0:
            return []
        moved = []
        for mid, mob in self.MOBS.items():
            if not mob.get("alive") or mob.get("status", "alive") != "alive":
                continue
            target = None
            best = aggro
            for pid, player in self.PLAYERS.items():
                if player["status"] != "alive":
                    continue
                dist = math.dist(mob["pos"], player["pos"])
                if dist < best:
                    best = dist
                    target = player
            # With nobody in range, drift back to this mob's own post.
            home = mob.get("home") or self.PORTAL_POS
            goal = target["pos"] if target else home
            dx = goal[0] - mob["pos"][0]
            dy = goal[1] - mob["pos"][1]
            dz = goal[2] - mob["pos"][2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < 0.05:
                continue
            # Hold at a standoff distance: a guard that flies into your hull
            # is both ugly and unfair.
            standoff = float(balance.get("npc_standoff", 4.0))
            if target and dist <= standoff:
                # Back off a little, keeping the pressure on.
                dx, dy, dz = -dx, -dy, -dz
                dist = max(dist, 0.001)
            step = min(speed, dist)
            mob["pos"] = [
                max(0.0, min(63.0, mob["pos"][0] + dx / dist * step)),
                max(0.0, min(63.0, mob["pos"][1] + dy / dist * step)),
                max(0.0, min(63.0, mob["pos"][2] + dz / dist * step)),
            ]
            moved.append(mid)
        return moved

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
            since = self.tick_count - last
            if since < cd:
                # Wind up: the shot is about to land, so warn the pilot.
                windup_ms = float(balance.get("npc_windup_ms", 400))
                if since >= max(0.0, (cd * self.tick_ms - windup_ms) / max(1, self.tick_ms)):
                    events.append({"windup": True, "mob": target_mob, "pilot": pid})
                continue
            self.MOB_ATTACK_TICKS[target_mob] = self.tick_count
            damage = float(balance.get("npc_damage", 5))
            # The mob's shot lands on the pilot: shields absorb first, the
            # remainder hits the hull (D2, same order as pilot fire).
            absorbed = min(float(player.get("shields") or 0), damage)
            player["shields"] = float(player.get("shields") or 0) - absorbed
            hull = damage - absorbed
            player["hp"] = max(0, float(player.get("hp") or 0) - hull)
            dead = player["hp"] <= 0
            events.append({
                "mob": target_mob, "pilot": pid, "damage": damage,
                "shields": float(player.get("shields") or 0),
                "hp": float(player.get("hp") or 0),
                # `hit` means the mob died (kill credit for the pilot);
                # `dead` means the pilot was killed by the mob.
                "hit": False,
                "dead": dead,
            })
            if dead:
                death = self.cause_death(pid, target_mob)
                events[-1]["dropped"] = (death or {}).get("dropped") or []
                events[-1]["room"] = player.get("room") or self.ROOM_ID
        return events

    # ---- tick loop ----

    def tick_once(self):
        """One tick of the engine (≈100 ms at 10 TPS). Deterministic when RNG seeded.

        Returns the usual status plus `moved` and `vitals_changed` pilot ids so
        the web layer can broadcast only what actually changed (D2, D25).
        """
        self.tick_count += 1
        # Snapshot first so the vitals diff below reports what changed this tick.
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

        # 4b. Fight autopilot: idle engaged pilots glide toward their partner.
        moved += self._pvp_autopilot()

        # 4c. Travelling shots (D59): advance every bolt and resolve hits.
        shots = self._tick_projectiles()

        # 5. NPC engagement: living mobs within aggro range of a living pilot
        #    fire on their own cooldown.
        attacks = self._npc_combat()

        # 5b. Mobs close on the pilots that engage them.
        mobs_moved = self._npc_movement()

        # 5c. Speed, then hard contact with debris: bounce damage, and a
        #     head-on impact at full speed into a heavy rock blows it up.
        self._track_velocity()
        collisions = self._rock_collisions()

        # 6a. Retire pilots who disconnected long ago (Q11): offline rows past
        #    `offline_prune_after_seconds` no longer answer the world's calls,
        #    so keep only the pilot and the pilots who left recently. Never
        #    touches salvage, the revive timer, or a live pilot.
        retired = self.purge_offline()

        # 6. Death timers: respawn dead pilots after the configured delay.
        respawned = []
        for pid in list(self.DEATH_TIMERS):
            ticks_left = self.DEATH_TIMERS.get(pid, 0) - 1
            if ticks_left <= 0:
                del self.DEATH_TIMERS[pid]
                self.respawn_player(pid)
                respawned.append(pid)
                # The respawn moved the ship: the client must be told.
                if pid not in moved:
                    moved.append(pid)
            else:
                self.DEATH_TIMERS[pid] = ticks_left

        after = self._vitals_snapshot()
        return {
            "tick": self.tick_count,
            "players": len(self.PLAYERS),
            "mobs": len(self.MOBS),
            "active_portal": bool(self.PORTALS),
            "moved": moved,
            "vitals_changed": [pid for pid in before if before[pid] != after.get(pid)],
            "attacks": attacks,
            "mobs_changed": self._mob_state_changes(),
            "mobs_moved": mobs_moved,
            "collisions": collisions,
            # Travelling shots that landed or fizzled this tick (D59).
            "shots": shots,
            # Bolts still in flight, for the client's tracers.
            "projectiles": [{"id": s["id"], "pos": list(s["pos"])}
                            for s in self.PROJECTILES.values()],
            # Pilots who came back this tick: a fight they were in survives
            # the death, so the pair rematches (D58).
            "respawned": respawned,
            # Pilots retired by disconnect hygiene this tick (Q11). Not broadcast;
            # the client only ever grows its roster from welcome + pos frames.
            "retired": retired,
        }

# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

engine = Engine()

def stop_tick():
    engine.stop_tick()

def tick_once():
    return engine.tick_once()