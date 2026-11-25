"""Automated PVP duel harness (D59 verification).

Runs N two-pilot matches through the real server: both bots connect over
WebSocket, engage, fly with move intents, and fire travelling bolts at
each other. Logs kills, misses, stuck states and exceptions so balance
and flight problems surface without a human playing.

Usage:  ./venv/bin/python scripts/duel.py [matches]
"""

import json
import math
import os
import pathlib
import sys
import tempfile
import threading
import time

# Isolate world state BEFORE any import (same pattern as smoke.py).
_TMP = tempfile.mkdtemp(prefix="qfe-duel-")
os.environ["QFE_DB"] = os.path.join(_TMP, "duel.sqlite3")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from quest_for_earth.engine import engine  # noqa: E402
from quest_for_earth.web import app  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

PORT = 8407
MATCHES = int(sys.argv[1]) if len(sys.argv) > 1 else 25
MATCH_TIMEOUT = 45.0      # seconds before a match is abandoned
STUCK_SECONDS = 6.0       # no movement from either side for this long = stuck
FIRE_RANGE = 22.0         # fire_range_pilot (balance.json)
FIRE_COOLDOWN = 0.5       # fire_cooldown_pilot_ms


class Bot:
    """One WS pilot with a background frame reader."""

    def __init__(self, name):
        self.name = name
        self.sock = ws_connect(f"ws://127.0.0.1:{PORT}/ws", open_timeout=5,
                               legacy=True)
        self.frames = []
        self.consumed = 0          # frames already tallied by the match loop
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"t": "set_name", "name": name})

    def send(self, obj):
        try:
            self.sock.send(json.dumps(obj))
        except Exception:
            self.alive = False

    def _read(self):
        while self.alive:
            try:
                msg = json.loads(self.sock.recv(timeout=0.05))
                with self.lock:
                    self.frames.append(msg)
            except TimeoutError:
                continue
            except Exception:
                self.alive = False

    def latest(self, t, **match):
        """Most recent frame of type t whose fields match."""
        with self.lock:
            for msg in reversed(self.frames):
                if msg.get("t") != t:
                    continue
                if all(msg.get(k) == v for k, v in match.items()):
                    return msg
        return None

    def take_new(self):
        """Frames that arrived since the last take_new (tallied once)."""
        with self.lock:
            fresh = self.frames[self.consumed:]
            self.consumed = len(self.frames)
        return fresh

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


def far_side(side):
    """Spawn side / wormhole side of the asteroid wall (world.json)."""
    if side == "spawn":
        return [12.0, 16.0, 22.0]
    return [38.0, 28.0, 42.0]


def setup_match(a_name, b_name):
    """Park both pilots on opposite sides of the wall, full vitals, engaged.

    The level's guards would otherwise join the fight: freeze their attack
    clock and park them in the far corner so the duel measures PVP only.
    """
    for mid in engine.MOBS:
        engine.MOB_ATTACK_TICKS[mid] = 10 ** 9
        if engine.MOBS[mid].get("pos"):
            engine.MOBS[mid]["pos"] = [1.0, 1.0, 1.0]
    for name, side in ((a_name, "spawn"), (b_name, "wormhole")):
        p = engine.PLAYERS[name]
        p["status"] = "alive"
        p["pos"] = list(far_side(side))
        p["shields"] = p["max_shields"]
        p["hp"] = p["max_hp"]
        engine.DEATH_TIMERS.pop(name, None)
    engine.LAST_FIRE.clear()


def bot_loop(bot, foe_name, deadline, shot_counter, rocks):
    """Fly toward the foe, strafe, dodge rocks, and fire when in range."""
    strafe_sign = 1.0
    strafe_flip = time.time() + 1.5
    last_fire = 0.0
    while time.time() < deadline and bot.alive:
        now = time.time()
        me_frame = bot.latest("pos", **{"from": bot.name})
        foe_frame = bot.latest("pos", **{"from": foe_name})
        me = (me_frame or {}).get("pos")
        foe = (foe_frame or {}).get("pos")
        if me and foe:
            d = math.dist(me, foe)
            toward = [(foe[i] - me[i]) / max(d, 0.001) for i in range(3)]
            steer = list(toward)
            # Wall avoidance: any big rock whose centre sits within a few
            # radii of the path ahead pushes the steering sideways.
            for rock in rocks:
                rp = rock["pos"]
                rr = 0.9 + float(rock.get("size", 1)) * 0.55 + 3.0
                rel = [rp[i] - me[i] for i in range(3)]
                ahead = sum(rel[i] * toward[i] for i in range(3))
                if ahead <= 0 or ahead > 10.0:
                    continue
                perp = math.sqrt(max(0.0, sum(c * c for c in rel) - ahead * ahead))
                if perp < rr:
                    side_dir = [rel[2], 0.0, -rel[0]]
                    n = math.sqrt(side_dir[0] ** 2 + side_dir[2] ** 2) or 1.0
                    push = 1.5 * (rr - perp) / rr
                    steer = [steer[i] + side_dir[i] / n * push for i in range(3)]
            n = math.sqrt(sum(c * c for c in steer)) or 1.0
            steer = [c / n for c in steer]
            if now > strafe_flip:
                strafe_sign = -strafe_sign
                strafe_flip = now + 1.5
            side = [toward[2], 0.0, -toward[0]]      # perpendicular in XZ
            if d > 16.0:
                vec = steer
            else:
                vec = [steer[i] * 0.4 + side[i] * 0.9 * strafe_sign
                       for i in range(3)]
            bot.send({"t": "move", "vec": vec, "name": bot.name})
            if d <= FIRE_RANGE and now - last_fire >= FIRE_COOLDOWN:
                bot.send({"t": "fire", "target": foe_name, "name": bot.name})
                last_fire = now
                shot_counter[bot.name] += 1
        time.sleep(0.08)


def run_match(i):
    a_name, b_name = f"Ace_{i}", f"Rival_{i}"
    stats = {"match": i, "shots": {a_name: 0, b_name: 0},
             "hits": {a_name: 0, b_name: 0}, "misses": {a_name: 0, b_name: 0},
             "duration": 0.0, "winner": None, "anomaly": None}
    a, b = Bot(a_name), Bot(b_name)
    try:
        # Wait for both welcomes, then park and engage. The welcome frame
        # names its pilot ("you") — a squadmate's welcome is not ours.
        deadline = time.time() + 5
        def own_welcome(bot):
            w = bot.latest("welcome") or {}
            return ((w.get("you") or {}).get("name") == bot.name) and w
        while time.time() < deadline and not (own_welcome(a) and own_welcome(b)):
            time.sleep(0.05)
        rocks = [d for d in (own_welcome(a) or {}).get("debris", [])
                 if d.get("material") == "rock"]
        setup_match(a_name, b_name)
        a.send({"t": "engage", "target": b_name, "name": a_name})
        b.send({"t": "engage", "target": a_name, "name": b_name})

        start = time.time()
        deadline = start + MATCH_TIMEOUT
        threads = [threading.Thread(target=bot_loop,
                                    args=(a, b_name, deadline, stats["shots"], rocks)),
                   threading.Thread(target=bot_loop,
                                    args=(b, a_name, deadline, stats["shots"], rocks))]
        for t in threads:
            t.start()

        last_pos = {a_name: None, b_name: None}
        last_move = {a_name: start, b_name: start}
        while time.time() < deadline:
            time.sleep(0.1)
            now = time.time()
            # Count shots/hits/misses from one frame stream: every frame is
            # broadcast to both bots, so tallying both would double-count.
            for m in a.take_new():
                if m.get("t") == "combat" and m.get("kind") in ("shot", "miss") \
                        and m.get("from") in stats["hits"]:
                    key = "hits" if m.get("kind") == "shot" else "misses"
                    stats[key][m["from"]] += 1
            for name, bot in ((a_name, a), (b_name, b)):
                f = bot.latest("pos", **{"from": name})
                pos = (f or {}).get("pos")
                if pos and last_pos[name] and math.dist(pos, last_pos[name]) > 0.05:
                    last_move[name] = now
                last_pos[name] = pos
            sa, sb = engine.PLAYERS[a_name]["status"], engine.PLAYERS[b_name]["status"]
            if sa != "alive" or sb != "alive":
                break
            if now - last_move[a_name] > STUCK_SECONDS and \
               now - last_move[b_name] > STUCK_SECONDS:
                stats["anomaly"] = "stuck"
                break
        stats["duration"] = round(time.time() - start, 1)
        sa, sb = engine.PLAYERS[a_name]["status"], engine.PLAYERS[b_name]["status"]
        if sa != "alive" and sb != "alive":
            stats["winner"] = "draw"
        elif sa != "alive":
            stats["winner"] = b_name
        elif sb != "alive":
            stats["winner"] = a_name
        elif stats["anomaly"] != "stuck":
            stats["anomaly"] = "timeout"
            stats["winner"] = "draw"
        for t in threads:
            t.join(timeout=2)
        # Split-respawn observation (D59): once the loser comes back, the
        # pair should be split across the asteroid wall.
        wait_until = time.time() + 6
        while time.time() < wait_until:
            if (engine.PLAYERS[a_name]["status"] == "alive"
                    and engine.PLAYERS[b_name]["status"] == "alive"):
                break
            time.sleep(0.2)
        stats["split"] = ((math.dist(engine.PLAYERS[a_name]["pos"],
                                     engine.SPAWN_POS) < 12
                           and math.dist(engine.PLAYERS[b_name]["pos"],
                                         engine.PORTAL_POS) < 12)
                          or (math.dist(engine.PLAYERS[b_name]["pos"],
                                        engine.SPAWN_POS) < 12
                              and math.dist(engine.PLAYERS[a_name]["pos"],
                                            engine.PORTAL_POS) < 12))
    except Exception as exc:                       # noqa: BLE001
        stats["anomaly"] = f"exception: {exc!r}"    # harness errors count
    finally:
        a.close()
        b.close()
    return stats


def start_server():
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("duel server did not start")


def main():
    print(f"duel: world state isolated in {_TMP}")
    start_server()
    engine._tick_stopped = False                  # real-time match loop
    results = []
    for i in range(1, MATCHES + 1):
        stats = run_match(i)
        results.append(stats)
        w = stats["winner"] or "?"
        print(f"match {i:>2}: {w:<8} in {stats['duration']:>5}s "
              f"A shots={stats['shots'][f'Ace_{i}']:<3}"
              f"hits={stats['hits'][f'Ace_{i}']:<3}miss={stats['misses'][f'Ace_{i}']:<3} | "
              f"B hits={stats['hits'][f'Rival_{i}']:<3}miss={stats['misses'][f'Rival_{i}']:<3}"
              + (f"  split-respawn: ok" if stats.get("split") else "")
              + (f"  ANOMALY: {stats['anomaly']}" if stats["anomaly"] else ""))
    engine.stop_tick()

    anomalies = [r for r in results if r["anomaly"]]
    decided = [r for r in results if r["winner"] not in (None, "draw")]
    total_shots = sum(sum(r["shots"].values()) for r in results)
    total_hits = sum(sum(r["hits"].values()) for r in results)
    total_miss = sum(sum(r["misses"].values()) for r in results)
    print(f"\n{MATCHES} matches: {len(decided)} decided, "
          f"{len([r for r in results if r['winner'] == 'draw'])} draws")
    print(f"bolt accuracy: {total_hits}/{total_hits + total_miss} landed "
          f"({100 * total_hits / max(1, total_hits + total_miss):.0f}%), "
          f"{total_shots} fired (client-acked)")
    if anomalies:
        print(f"anomalies ({len(anomalies)}):")
        for r in anomalies:
            print(f"  - match {r['match']}: {r['anomaly']}")
    else:
        print("no anomalies")
    # A duel harness reports; it does not gate (balance tuning is human work).
    return 0


if __name__ == "__main__":
    sys.exit(main())
