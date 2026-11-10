"""Tiny manual check: connect one player and print the first messages."""
import json
import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="r2m-dbg-")
os.environ["R2M_DB"] = os.path.join(_TMP, "dbg.sqlite3")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from return_to_mordor import db, engine  # noqa: E402
from return_to_mordor import web  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402
import threading, time, urllib.request, urllib.parse  # noqa: E402

import uvicorn  # noqa: E402
config = uvicorn.Config(web.app, host="127.0.0.1", port=8398, log_level="info")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:8398/health", timeout=2)
        break
    except Exception:
        time.sleep(0.2)

def http(path, payload, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:8398{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

token = http("/api/register", {"username": "debugger", "password": "hunter22"})["token"]
http("/api/character", {"name": "Debug", "gender": "male", "race": "Gondorian",
                        "class1": "warrior", "class2": "ranger"}, {"X-Token": token})

sock = ws_connect(f"ws://127.0.0.1:8398/ws?token={token}", open_timeout=5)
for _ in range(3):
    print("recv:", sock.recv(timeout=3))
sock.send(json.dumps({"t": "move", "dir": "south"}))
for _ in range(3):
    try:
        print("recv:", sock.recv(timeout=2))
    except TimeoutError:
        break
sock.close()
