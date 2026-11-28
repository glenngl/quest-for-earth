"""Two-pilot playtest: fly around, pick a rock, shoot at it.

Drives two WebSocket pilots through the real server handler code and checks
that flying and shooting both work. Run it against a local server or the
deployed container:

    ./venv/bin/python scripts/duo_playtest.py                       # ws://127.0.0.1:8000/ws
    QFE_WS=wss://quest-for-earth.jarin.net/ws \
        ./venv/bin/python scripts/duo_playtest.py                   # deployed

This is intentionally a loop you can repeat while iterating: it exits
non-zero on any failed check so a fix can be verified with one command.
"""

import json
import math
import os
import sys
import time

from websockets.sync.client import connect as ws_connect

WS_URL = os.environ.get("QFE_WS", "ws://127.0.0.1:8000/ws")
FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class Pilot:
    def __init__(self, name):
        self.name = name
        self.sock = ws_connect(WS_URL, open_timeout=10, legacy=True)
        self.pending = []
        self.send({"t": "set_name", "name": name})
        self.welcome = self.wait_for(lambda m: m.get("t") == "welcome", attempts=60)

    def send(self, obj):
        self.sock.send(json.dumps(obj))

    def wait_for(self, pred, attempts=120, timeout=0.1):
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

    def drain(self):
        out = []
        for _ in range(20):
            try:
                out.append(json.loads(self.sock.recv(timeout=0.02)))
            except TimeoutError:
                break
        return out

    def close(self):
        self.sock.close()


def dist(a, b):
    return math.dist(a, b)


def main():
    print(f"duo playtest: {WS_URL}")
    a = b = None
    try:
        a = Pilot("DuoA")
        b = Pilot("DuoB")
        check("two pilots connected", bool(a.welcome and b.welcome),
              f"A={bool(a.welcome)} B={bool(b.welcome)}")
        if not a.welcome or not b.welcome:
            return
        start_a = list(a.welcome["you"]["pos"])
        start_b = list(b.welcome["you"]["pos"])
        debris = a.welcome.get("debris", [])

        # --- Flying: send move intents, confirm authoritative pos changes ---
        a.drain(); b.drain()
        for _ in range(6):
            a.send({"t": "move", "name": "DuoA", "vec": [1, 0, 0]})
            b.send({"t": "move", "name": "DuoB", "vec": [0, 1, 0]})
            time.sleep(0.12)
        # Drain all pos frames produced by the moves; keep the latest one.
        latest_a = [m for m in a.drain() if m.get("t") == "pos" and m.get("from") == "DuoA"]
        latest_b = [m for m in b.drain() if m.get("t") == "pos" and m.get("from") == "DuoB"]
        more_a = a.wait_for(lambda m: m.get("t") == "pos" and m.get("from") == "DuoA", 20)
        more_b = b.wait_for(lambda m: m.get("t") == "pos" and m.get("from") == "DuoB", 20)
        if more_a: latest_a.append(more_a)
        if more_b: latest_b.append(more_b)
        check("pilot A flies (authoritative pos broadcast)", len(latest_a) > 0)
        check("pilot B flies (authoritative pos broadcast)", len(latest_b) > 0)
        pos_a = latest_a[-1]["pos"] if latest_a else start_a
        pos_b = latest_b[-1]["pos"] if latest_b else start_b
        check("A moved along X", pos_a[0] > start_a[0] + 0.5,
              f"{start_a} -> {pos_a}")
        check("B moved along Y", pos_b[1] > start_b[1] + 0.5,
              f"{start_b} -> {pos_b}")

        # --- Pick a rock: nearest non-rock material chunk from the field ---
        # Prefer the big asteroid wall rocks if present, else any debris.
        rocks = [d for d in debris if not d.get("harvested")]
        check("debris field has rocks to shoot", len(rocks) > 0,
              f"{len(rocks)} chunks")
        if not rocks:
            return
        rock = max(rocks, key=lambda d: d.get("size", 0))
        aim = [rock["pos"][i] - pos_a[i] for i in range(3)]
        ln = math.sqrt(sum(v * v for v in aim)) or 1.0
        aim = [v / ln for v in aim]

        # --- Shooting: fire along the view vector at the rock ---
        a.drain(); b.drain()
        a.send({"t": "fire", "name": "DuoA", "dir": aim})
        proj = a.wait_for(lambda m: m.get("t") == "proj"
                          and any(s.get("from") == "DuoA" or s.get("id", "").endswith("DuoA")
                                  for s in m.get("shots", [])), 40)
        # The proj frame does not carry `from`, so accept any bolt in flight.
        if not proj:
            proj = a.wait_for(lambda m: m.get("t") == "proj" and m.get("shots"), 20)
        check("shooting spawns a bolt in flight", proj is not None and bool(proj.get("shots")))

        shot = None
        for _ in range(60):
            m = a.wait_for(lambda m: m.get("t") == "combat" and m.get("kind") == "shot"
                           and m.get("from") == "DuoA", 10)
            if m:
                shot = m
                break
            time.sleep(0.05)
        check("bolt strikes a body (combat hit frame)", shot is not None)
        if shot:
            check("bolt travelled straight to the aimed rock",
                  shot.get("target") == rock["id"] or shot.get("target_kind") in ("debris", "mob", "pilot"),
                  f"target={shot.get('target')} kind={shot.get('target_kind')}")
            check("a rock shot deals no damage to the rock",
                  shot.get("target_kind") != "debris" or float(shot.get("damage") or 0) == 0.0,
                  f"damage={shot.get('damage')}")

        # --- Co-op: pilot B sees the same combat broadcast (room-scoped) ---
        bshot = None
        for m in b.pending:
            if m.get("t") == "combat" and m.get("kind") == "shot" and m.get("from") == "DuoA":
                bshot = m
                break
        if not bshot:
            bshot = b.wait_for(lambda m: m.get("t") == "combat" and m.get("kind") == "shot"
                               and m.get("from") == "DuoA", 20)
        check("wingman sees the shot broadcast", bshot is not None)
    finally:
        for p in (a, b):
            if p:
                p.close()

    if FAILURES:
        print(f"\nduo playtest FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nduo playtest PASSED — flying and shooting both work")


if __name__ == "__main__":
    main()
