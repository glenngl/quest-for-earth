"""HTTP + WebSocket API: serves the client & game socket.

Room-scoped events (D4).  One process, one event loop.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import pathlib
import random
import sys
import threading
import time
import urllib.request

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
try:  # Starlette >= 1.10 raises WebSocketDisconnected on a closed socket
    from starlette.websockets import WebSocketDisconnected
except ImportError:  # pragma: no cover - older Starlette
    WebSocketDisconnected = WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── project imports ──────────────────────────────────────────────────────
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from quest_for_earth.engine import engine  # noqa: E402  (Engine singleton instance)
from quest_for_earth import world as world_mod  # noqa: E402
# (db, auth etc. would go here in the full game; kept minimal for Level 1)

# ── tiny in‑memory player store ──────────────────────────────────────────
# In production this would be SQLite (D9).  Here we keep it simple.
PLAYERS_DB = {}  # char_id -> dict with name, token, etc.

# ── FastAPI app ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app):
    """Drive the authoritative engine tick while the server is up (D2, D25)."""
    task = asyncio.create_task(_tick_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Quest for Earth — Level 1", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Level 1 assets change often; never let a browser serve stale JS/CSS."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Mount static files (game.js, style.css) — served from web/ directory
web_dir = pathlib.Path("/var/home/glenngl/Projects/quest-for-earth/src/quest_for_earth/web")
app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


# ── static HTML (single-page game) ───────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def get_health():
    """Liveness probe."""
    return {"ok": True}


@app.get("/api/ships", include_in_schema=False)
async def get_ships():
    """Ship classes and hull types."""
    from quest_for_earth.data.ships import get_ships as _get_ships
    ships = _get_ships()
    return ships


@app.get("/api/lighting", include_in_schema=False)
async def get_lighting():
    """Per-vertex light model used by the WebGL renderer (data/lighting.json)."""
    return world_mod.lighting()


def _inject_lighting(html):
    """Make data/lighting.json available to game.js before it runs (D1).

    The client has no build step, so the JSON is injected as a small inline
    script in front of game.js rather than fetched asynchronously — this
    keeps lighting as data without a new runtime code path in the renderer.
    """
    config = json.dumps(world_mod.lighting(), separators=(",", ":"))
    script = f"<script>window.QFE_LIGHTING={config};</script>"
    return html.replace("<script src=\"/static/game.js", script + "<script src=\"/static/game.js", 1)


@app.get("/", include_in_schema=False)
async def get_index():
    """Serve the game client HTML."""
    html_path = pathlib.Path("/var/home/glenngl/Projects/quest-for-earth/src/quest_for_earth/web/index.html")
    if html_path.exists():
        with open(html_path, "r") as f:
            html = f.read()
        # Chromium can keep a script in its memory cache even with no-store;
        # tie the URL to game.js's mtime so every edit is picked up on reload.
        js_version = (web_dir / "game.js").stat().st_mtime_ns
        html = html.replace("/static/game.js", f"/static/game.js?v={js_version}")
        return HTMLResponse(content=_inject_lighting(html), media_type="text/html")
    return HTMLResponse(content=_inject_lighting(_default_html()), media_type="text/html")


def _default_html():
    return f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Quest for Earth — Level 1</title>
    <style>
      body{{margin:0;background:#000;color:#fff;font-family:sans-serif}}
      #canvas{{display:block;width:100vw;height:100vh}}
      #minimap{{position:absolute;bottom:10px;left:10px;width:150px;height:150px;background:rgba(0,0,0,0.5);padding:5px}}
      #chat{{position:absolute;top:10px;left:10px;width:300px;height:150px;background:rgba(0,0,0,0.5);padding:5px;overflow:auto}}
    </style></head><body>
    <div id="canvas"><canvas id="glcanvas" width="800" height="600"></canvas></div>
    <div id="minimap"><strong>Mini-Map</strong>: fogged cube</div>
    <div id="chat"><strong>Chat</strong>: type messages</div>
    <script src="/static/game.js"></script></body></html>
    """


# ── WebSocket game socket ────────────────────────────────────────────────

WS_CONNECTIONS = set()
WS_NAMES = {}  # websocket -> char_id, set on the client's hello message


def _vitals(player):
    """Client-facing view of the three onboard stores plus hit points."""
    return {
        "hp": player["hp"],
        "shields": player["shields"],
        "max_hp": player["max_hp"],
        "max_shields": player["max_shields"],
        "energy": player["energy"],
        "methane": player["methane"],
        "metal": player["metal"],
        "max_energy": player["max_energy"],
        "max_methane": player["max_methane"],
        "max_metal": player["max_metal"],
    }


async def _broadcast(obj):
    """Send one authoritative frame to every connected pilot (D4)."""
    frame = json.dumps(obj)
    for conn in list(WS_CONNECTIONS):
        try:
            await conn.send_text(frame)
        except Exception:
            pass


async def _tick_loop():
    """Run engine ticks at the data-driven rate; broadcast only deltas."""
    while True:
        await asyncio.sleep(max(0.01, engine.tick_ms / 1000.0))
        if getattr(engine, "_tick_stopped", False):
            continue
        status = engine.tick_once()
        for pid in status.get("moved", []):
            player = engine.PLAYERS.get(pid)
            if player:
                await _broadcast({"t": "pos", "from": pid, "pos": player["pos"]})
        for pid in status.get("vitals_changed", []):
            player = engine.PLAYERS.get(pid)
            if player:
                await _broadcast({"t": "vitals", "from": pid, **_vitals(player)})


@app.websocket("/ws")
async def ws_game(ws: WebSocket):
    await ws.accept()
    WS_CONNECTIONS.add(ws)
    try:
        # Wait for first message: typically a "welcome" init
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            # Register the player (server is authoritative, D2) and welcome them.
            name = msg.get("name") or "Player"
            char_id = name
            if char_id not in engine.PLAYERS:
                engine.add_player(char_id, name)  # engine uses the data-driven spawn
            WS_NAMES[ws] = char_id
            me = engine.PLAYERS.get(char_id, {})
            await ws.send_text(json.dumps({
                "t": "welcome",
                "you": {
                    "name": name,
                    "room": me.get("room", "spawn_cube"),
                    "pos": me.get("pos", list(engine.SPAWN_POS)),
                    **(_vitals(me) if me else {}),
                },
                "portal": {
                    "pos": list(engine.PORTAL_POS),
                    "front": engine.PORTAL_FRONT,
                    "aperture": engine.PORTAL_APERTURE,
                },
                "debris": [dict(d) for d in engine.DEBRIS.values()
                           if not d["harvested"]],
                "room": {"id": me.get("room", "spawn_cube"), "fog": True},
                "npcs": [
                    {"id": mid, "pos": m.get("pos"), "hp": m.get("hp")}
                    for mid, m in engine.MOBS.items() if m.get("alive")
                ],
                "chart": [],
            }))
            # Loop: wait for player commands
            while True:
                try:
                    data = await ws.receive_text()
                    msg = json.loads(data)
                    await _handle_ws_message(ws, msg)
                except Exception:
                    break
    except (WebSocketDisconnect, WebSocketDisconnected):
        pass
    finally:
        WS_CONNECTIONS.discard(ws)
        WS_NAMES.pop(ws, None)


async def _handle_ws_message(ws: WebSocket, msg: dict):
    """Dispatch incoming WS messages to engine logic."""
    t = msg.get("t")
    char_id = WS_NAMES.get(ws) or msg.get("from") or msg.get("name")  # simplified

    if t == "move":
        dir_ = msg.get("dir", "none")
        # Simple movement logic - update player position
        if char_id and char_id in engine.PLAYERS:
            player = engine.PLAYERS[char_id]
            # Basic direction handling
            x, y, z = player["pos"]
            if dir_ == "north": z = min(63, z + 1)
            elif dir_ == "south": z = max(0, z - 1)
            elif dir_ == "east": x = min(63, x + 1)
            elif dir_ == "west": x = max(0, x - 1)
            elif dir_ == "up": y = min(63, y + 1)
            elif dir_ == "down": y = max(0, y - 1)
            player["pos"] = [x, y, z]
            # Broadcast the authoritative position to every client (D2).
            await _broadcast({"t": "pos", "from": char_id, "pos": player["pos"]})

    elif t == "enter_portal":
        # The client only expresses intent; the engine judges the side and
        # aperture (server-authoritative, D2).
        if char_id:
            ok, reason = engine.enter_portal(char_id)
            if ok:
                broadcast = json.dumps({
                    "t": "toast",
                    "text": f"{char_id} passed through the wormhole and returned to spawn.",
                })
                for conn in list(WS_CONNECTIONS):
                    try:
                        await conn.send_text(broadcast)
                    except Exception:
                        pass
                # Tell the advancing pilot their new authoritative position.
                await ws.send_text(json.dumps({
                    "t": "pos", "from": char_id,
                    "pos": engine.PLAYERS[char_id]["pos"],
                }))
            else:
                await ws.send_text(json.dumps({"t": "toast", "text": reason}))

    elif t == "look":
        # Update look/aim direction
        pass

    elif t == "repair":
        # D25: spend energy to restore a nearby teammate's shields.
        target = msg.get("target")
        amount = msg.get("amount", msg.get("hp_to_restore", 1))
        if char_id and target:
            success, err = engine.repair_shields(char_id, target, amount)
            await ws.send_text(json.dumps({
                "t": "repair_result",
                "success": success,
                "error": err,
            }))
            if success:
                me = engine.PLAYERS.get(char_id)
                mate = engine.PLAYERS.get(target)
                if me:
                    await _broadcast({"t": "vitals", "from": char_id, **_vitals(me)})
                if mate:
                    await _broadcast({"t": "vitals", "from": target, **_vitals(mate)})

    elif t == "harvest":
        # D25: fill the methane or metal tank from a nearby chunk.
        debris_id = msg.get("id")
        if char_id:
            ok, result = engine.harvest_debris(char_id, debris_id)
            await ws.send_text(json.dumps({
                "t": "toast", "text": result, "success": ok,
            }))
            if ok:
                me = engine.PLAYERS.get(char_id, {})
                await _broadcast({
                    "t": "debris_gone", "id": debris_id, "by": char_id,
                    "from": char_id, "vitals": _vitals(me) if me else {},
                })

    elif t == "fire":
        # Shooter overlay fire — server judges hit against target square
        # For Level 1, we'll keep it simple: always miss unless specifically aimed
        await ws.send_text(json.dumps({
            "t": "toast",
            "text": "Server judges the shot (not implemented in Level 1 proto).",
        }))

    elif t == "take":
        # Take salvage from room floor
        item = msg.get("item")
        if char_id:
            await ws.send_text(json.dumps({
                "t": "you",
                "you": {"message": f"Picked up {item or 'salvage'}"},
            }))

    elif t == "die":
        # Player death (called when HP -> 0)
        if char_id:
            engine.cause_death(char_id)
            # Respawn after 5 ticks
            import threading
            threading.Timer(0.5, lambda: engine.respawn_player(char_id)).start()
            await ws.send_text(json.dumps({
                "t": "you",
                "you": {"room": "spawn_cube", "hp": 0, "shields": 0, "message": "You died. Respawning..."},
            }))

    elif t == "chat":
        text = msg.get("text", "")
        # Broadcast chat to all players
        broadcast = json.dumps({"t": "say", "from": char_id or "Player", "text": text})
        for conn in WS_CONNECTIONS:
            try:
                await conn.send_text(broadcast)
            except Exception:
                pass

    elif t == "fog_share":
        # Share fog-of-war state with another player
        target_id = msg.get("target")
        # Simplified: share own fog state
        if char_id and char_id in engine.PLAYERS:
            player = engine.PLAYERS[char_id]
            await ws.send_text(json.dumps({
                "t": "fog_shared",
                "from": char_id,
                "room": player["room"],
                "fog_state": "partial",  # would be a bitmap in full impl
            }))


# ── server entry point ───────────────────────────────────────────────────

def start_server(host="127.0.0.1", port=8000):
    """Start the uvicorn server."""
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # Run in a thread so we can block
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    start_server()