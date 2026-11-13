"""Quick capacity probe: N concurrent players, live moves + combat.

Measures the server process's CPU% and RSS while ``N`` WebSocket
clients behave like real players: everyone walks a room loop, and a
third of them park in a combat room swinging at mobs. Not a test of
correctness — the smoke suite owns that — just a resource reading.
"""

import json
import os
import pathlib
import sys
import time

N = int(os.environ.get("LOAD_N", "100"))
SECONDS = float(os.environ.get("LOAD_SECONDS", "30"))
PORT = int(os.environ.get("LOAD_PORT", "8397"))
BASE = f"http://127.0.0.1:{PORT}"


def http(path, payload=None, headers=None):
    from urllib.request import Request, urlopen
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=data,
                  headers={"Content-Type": "application/json", **(headers or {})},
                  method="POST" if payload is not None else "GET")
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def server_proc():
    """Find the uvicorn PID listening on the load port."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            if "return_to_mordor" in cmd and "web:app" in cmd:
                return int(pid)
        except OSError:
            continue
    raise RuntimeError("server process not found")


def read_proc(pid):
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().split()
    utime, stime = int(parts[13]), int(parts[14])
    with open(f"/proc/{pid}/statm") as f:
        rss_pages = int(f.read().split()[1])
    hz = os.sysconf("SC_CLK_TCK")
    return (utime + stime) / hz, rss_pages * os.sysconf("SC_PAGE_SIZE") / 1e6


def main():
    from websockets.sync.client import connect

    print(f"load: {N} players for {SECONDS:.0f}s on port {PORT}")
    tokens, sockets = [], []

    t0 = time.time()
    for i in range(N):
        name = f"load{i:03d}"
        d = http("/api/register", {"username": name, "password": "hunter22"})
        http("/api/character", {
            "name": name.title(), "gender": "other", "race": "Gondorian",
            "class1": "warrior", "class2": "ranger"}, {"X-Token": d["token"]})
        tokens.append(d["token"])
    print(f"setup: {N} accounts + characters in {time.time() - t0:.1f}s (bcrypt dominates)")

    t0 = time.time()
    for token in tokens:
        sockets.append(connect(f"ws://127.0.0.1:{PORT}/ws?token={token}", open_timeout=10))
        sockets[-1].recv(timeout=5)  # welcome
    print(f"connect: {len(sockets)} websockets in {time.time() - t0:.1f}s")

    # everyone walks; a third goes to the forest edge and fights
    fighters = sockets[::3]
    walkers = [s for s in sockets if s not in fighters]

    def drain(sock, n=8):
        for _ in range(n):
            try:
                sock.recv(timeout=0.01)
            except Exception:
                break

    for sock in walkers:
        drain(sock, 20)

    pid = server_proc()
    cpu0, rss0 = read_proc(pid)
    t_start = time.time()
    next_move = 0.0
    while time.time() - t_start < SECONDS:
        now = time.time()
        if now >= next_move:
            for sock in walkers:      # walk in place: bounce south/north
                sock.send(json.dumps({"t": "move", "dir": "south"}))
            next_move = now + 2.0
        for sock in fighters:          # keep re-engaging combat
            sock.send(json.dumps({"t": "attack", "target": "goblin"}))
            sock.send(json.dumps({"t": "vigor", "target": "Load000"}))
        time.sleep(0.9)
        for sock in sockets:
            drain(sock, 30)

    cpu1, rss1 = read_proc(pid)
    elapsed = time.time() - t_start
    for sock in sockets:
        sock.close()
    print(f"\nserver pid {pid}:")
    print(f"  CPU:    {(cpu1 - cpu0) / elapsed * 100:6.1f}% of one core")
    print(f"  RSS:    {rss1:6.1f} MB (was {rss0:.1f} MB before the run)")
    print(f"  ticks:  {elapsed / 0.9:.0f} combat rounds while 100 clients played")


if __name__ == "__main__":
    main()
