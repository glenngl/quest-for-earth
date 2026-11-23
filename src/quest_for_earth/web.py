"""HTTP + WebSocket API: serves the client & game socket.

Room-scoped events (D4).  One process, one event loop.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import pathlib
import sys
import threading

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


# Resolve the client assets relative to this module: the package works from
# a source checkout, an editable install and the installed wheel in the
# container. Absolute developer paths break both CI and production.
web_dir = pathlib.Path(__file__).resolve().parent / "web"
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
    html_path = web_dir / "index.html"
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
    return """
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
    level = int(player.get("level") or 1)
    per_level = float(world_mod.balance().get("xp", {}).get("per_level") or 100)
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
        # Rank and progress, so the pilot can see the ladder they climb.
        "xp": int(player.get("xp") or 0),
        "level": level,
        "xp_base": int(per_level * (level - 1)),
        "xp_next": int(per_level * level),
        # What the pilot is carrying, so the HUD can show the hold.
        "artifacts": list(player.get("artifacts") or []),
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
        for mid in status.get("mobs_moved") or []:
            mob = engine.MOBS.get(mid)
            if mob:
                await _broadcast({"t": "mob_pos", "id": mid, "pos": mob["pos"]})
        if status.get("mobs_changed"):
            # The enemy roster changed (a mob died or respawned): resend it so
            # clients do not keep a stale target list (D2/D4).
            await _broadcast({
                "t": "mobs",
                "mobs": [
                    {"id": mid, "pos": m.get("pos"), "hp": m.get("hp"),
                     "max_hp": m.get("max_hp"), "alive": bool(m.get("alive"))}
                    for mid, m in engine.MOBS.items()
                ],
            })
        for attack in status.get("attacks") or []:
            if attack.get("windup"):
                # The guard is about to fire: tell the pilot it is coming.
                await _broadcast({
                    "t": "combat", "kind": "mob_windup", "mob": attack.get("mob"),
                    "pilot": attack.get("pilot"),
                })
                continue
            await _broadcast({
                "t": "combat", "kind": "mob_hit", "mob": attack.get("mob"),
                "pilot": attack.get("pilot"), "damage": attack.get("damage"),
                "shields": attack.get("shields"), "hp": attack.get("hp"),
                "hit": attack.get("hit", False),
                "dead": attack.get("dead", False),
            })
            # A pilot killed by a mob drops salvage in the room they died in.
            for entry in attack.get("dropped") or []:
                await _broadcast({"t": "loot", "room": attack.get("room") or "spawn_cube",
                                  "item": entry})
        for hit in status.get("collisions") or []:
            # A rock impact: bounce damage, and a head-on blast when a heavy
            # rock is hit at full speed.
            await _broadcast({
                "t": "combat", "kind": "collision", "pilot": hit.get("pilot"),
                "rock": hit.get("rock"), "name": hit.get("name"),
                "damage": hit.get("damage"), "speed": hit.get("speed"),
                "explosion": hit.get("explosion", False),
            })
            if hit.get("explosion"):
                await _broadcast({"t": "debris_gone", "id": hit.get("rock"),
                                  "by": hit.get("pilot"), "explosion": True})


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
            else:
                # A pilot who reconnects comes back into the world.
                engine.set_pilot_online(char_id)
            WS_NAMES[ws] = char_id
            me = engine.PLAYERS.get(char_id, {})
            # Tell the squad somebody joined (D4), with their state so the
            # squad panel can read it straight away.
            join_frame = {"t": "join", "name": name,
                          "pos": list(me.get("pos") or [])}
            if me:
                join_frame.update(_vitals(me))
            await _broadcast(join_frame)
            # The roster of guards follows the pilots who are connected.
            engine.set_online_pilots(list(WS_NAMES.values()))
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
                # The far end is the entrance; spawn is the exit you arrive
                # from. Both are data (D1/D20) and both are drawn as wormholes.
                "spawn_portal": {
                    "pos": list(engine.SPAWN_POS),
                    "front": "+z",
                },
                "debris": [dict(d) for d in engine.DEBRIS.values()
                           if not d["harvested"]],
                "room": {"id": me.get("room", "spawn_cube"), "fog": True},
                "npcs": [
                    {"id": mid, "pos": m.get("pos"), "hp": m.get("hp"),
                     "max_hp": m.get("max_hp")}
                    for mid, m in engine.MOBS.items() if m.get("alive")
                ],
                # The squad roster: other pilots already in the world, so a
                # wingman who has not moved yet still shows up (D4). Their
                # vitals come with it, so the squad panel can read them
                # before anything happens to them.
                "pilots": [
                    {"name": p.get("name"), "pos": list(p.get("pos") or []),
                     **_vitals(p)}
                    for pid, p in engine.PLAYERS.items() if pid != char_id
                ],
                "chart": [],
                # Range for the prototype client-side target pick; the server
                # still judges every shot (D2).
                "fire_range": float(world_mod.balance().get("fire_range") or 24),
                # How long the server waits before respawning a downed pilot.
                "respawn_ms": int(world_mod.balance().get("death_respawn_ticks") or 50)
                * int(world_mod.balance().get("tick_ms") or 100),
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
        left = WS_NAMES.pop(ws, None)
        if left:
            try:
                await _broadcast({"t": "leave", "name": left})
            except Exception:
                pass
            # A pilot who leaves takes their guard with them.
            engine.set_online_pilots(list(WS_NAMES.values()))
            if left:
                # Their ship stays on record, but the world forgets them.
                engine.set_pilot_offline(left)


async def _handle_ws_message(ws: WebSocket, msg: dict):
    """Dispatch incoming WS messages to engine logic."""
    t = msg.get("t")
    char_id = WS_NAMES.get(ws) or msg.get("from") or msg.get("name")  # simplified

    if t == "move":
        vec = msg.get("vec")
        dir_ = msg.get("dir", "none")
        # Intent only — the engine clamps and owns the position (D2), and
        # refuses outright while the pilot is downed.
        if char_id and char_id in engine.PLAYERS:
            new_pos = engine.apply_move(char_id, vec, dir_)
            if new_pos is None:
                return
            # Broadcast the authoritative position to every client (D2).
            await _broadcast({"t": "pos", "from": char_id, "pos": new_pos})

    elif t == "enter_portal":
        # The client only expresses intent; the engine judges the side and
        # aperture (server-authoritative, D2).
        if char_id:
            ok, reason = engine.enter_portal(char_id)
            if ok:
                broadcast = json.dumps({
                    "t": "toast",
                    "text": f"{char_id} slipped through the wormhole — the level is complete.",
                })
                for conn in list(WS_CONNECTIONS):
                    try:
                        await conn.send_text(broadcast)
                    except Exception:
                        pass
                # The advancing pilot arrives at the exit.
                await ws.send_text(json.dumps({
                    "t": "portal_exit",
                    "text": "You slipped through the wormhole to the exit.",
                    "pos": list(engine.PLAYERS[char_id]["pos"]),
                }))
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
        # Prototype direct fire: the client names a target, the engine judges
        # range, cooldown and damage. The full target-square overlay stays a
        # later iteration (D14).
        target = msg.get("target")
        if char_id and target:
            ok, reason, hit = engine.fire_at(char_id, target)
            if not ok:
                # Only failures are worth a toast; a landed shot is reported
                # by the combat frame (and the hit marker on the HUD).
                await ws.send_text(json.dumps({
                    "t": "toast", "text": reason, "success": False,
                }))
            if ok:
                combat = {"t": "combat", "kind": "shot", "from": hit.get("from"),
                          "target": hit.get("target"), "damage": hit.get("damage"),
                          "shields": hit.get("shields"), "hp": hit.get("hp")}
                if hit.get("dead"):
                    combat["dead"] = True
                await _broadcast(combat)
                if hit.get("dead"):
                    # A mob that died drops salvage where it fell; the
                    # client needs the position so the pilot can fly to it.
                    mob = engine.MOBS.get(hit.get("target")) or {}
                    for entry in mob.get("dropped_loot") or []:
                        await _broadcast({"t": "loot",
                                          "room": mob.get("room") or engine.ROOM_ID,
                                          "item": entry})
                    # With the last guardian down the wormhole opens.
                    if engine.portal_is_open():
                        await _broadcast({"t": "portal_open",
                                          "text": "The wormhole is open."})
                for pid in (char_id, target):
                    pilot = engine.PLAYERS.get(pid)
                    if pilot:
                        await _broadcast({"t": "vitals", "from": pid, **_vitals(pilot)})
        else:
            await ws.send_text(json.dumps({
                "t": "toast",
                "text": "Server judges the shot (select a target).",
            }))

    elif t == "affiliate":
        # Trust a wingman: follow them and share in what they earn.
        target = msg.get("target")
        if char_id:
            if target:
                ok, reason = engine.affiliate(char_id, target)
            else:
                ok, reason = engine.break_affiliate(char_id)
                target = None
            await ws.send_text(json.dumps({
                "t": "toast", "text": reason if not ok else (
                    f"Affiliated with {target}." if target else "Affiliation cleared."),
                "success": ok,
            }))
            if ok and target:
                await _broadcast({"t": "affiliate", "from": char_id, "to": target})
            elif ok:
                await _broadcast({"t": "affiliate", "from": char_id, "to": None})

    elif t == "use":
        # Use a carried artifact (an energy cell repairs the shields).
        item = msg.get("item")
        if char_id:
            ok, res = engine.use_artifact(char_id, item)
            await ws.send_text(json.dumps({
                "t": "toast", "text": res, "success": ok,
            }))
            if ok:
                pilot = engine.PLAYERS.get(char_id)
                if pilot:
                    await _broadcast({"t": "vitals", "from": char_id, **_vitals(pilot)})

    elif t == "take":
        # Take salvage lying nearby; the server checks the distance.
        item = msg.get("item")
        if char_id:
            ok, res = engine.take_loot(char_id, item)
            await ws.send_text(json.dumps({
                "t": "toast", "text": res, "success": ok,
                "item": item if ok else None,
            }))
            if ok:
                # The hold changed: tell everyone what this pilot carries.
                pilot = engine.PLAYERS.get(char_id)
                if pilot:
                    await _broadcast({"t": "vitals", "from": char_id, **_vitals(pilot)})

    elif t == "loot":
        # Loot dropped by a death lands in the victim's room.
        room = msg.get("room") or engine.ROOM_ID
        item = msg.get("item") or {}
        if char_id and item.get("id"):
            engine.ROOM_LOOT.setdefault(room, [])
            engine.ROOM_LOOT[room].append(item)

    elif t == "evade":
        # Burst away from the nearest threat (mob or living pilot).
        if char_id:
            ok, reason = engine.evade(char_id)
            if ok:
                await _broadcast({"t": "pos", "from": char_id,
                                 "pos": engine.PLAYERS[char_id]["pos"]})
            await ws.send_text(json.dumps({
                "t": "toast", "text": reason if not ok else "Evaded.", "success": ok,
            }))

    elif t == "die":
        # Player death (called when HP -> 0). The engine records the death,
        # drops salvage and arms the authoritative respawn timer (D15).
        if char_id:
            result = engine.cause_death(char_id)
            dropped = (result or {}).get("dropped") or []
            room = (engine.PLAYERS.get(char_id) or {}).get("room") or engine.ROOM_ID
            for entry in dropped:
                await _broadcast({"t": "loot", "room": room, "item": entry})
            await ws.send_text(json.dumps({
                "t": "you",
                "you": {"room": "spawn_cube", "hp": 0, "shields": 0, "message": "You died. Respawning..."},
            }))

    elif t == "scan":
        # Local scan: reveal nearby unharvested debris and living mobs.
        if char_id:
            ok, reason, hits = engine.scan(char_id)
            await ws.send_text(json.dumps({
                "t": "scan_result", "ok": ok, "reason": reason, "hits": hits,
                "ttl": int(world_mod.balance().get("scan_ttl_ms") or 4000),
            }))

    elif t == "chat":
        text = msg.get("text", "")
        whisper_to = ""
        body = text
        if isinstance(text, str) and text.startswith("/w "):
            parts = text.split(None, 2)
            if len(parts) >= 2 and parts[1].strip():
                whisper_to = parts[1].strip()
                body = (parts[2] if len(parts) > 2 else "").strip()
        if whisper_to and whisper_to != char_id:
            # /w <pilot> <message> is a whisper: only the target hears it.
            whisper = json.dumps({"t": "say", "from": char_id or "Player",
                                  "to": whisper_to, "text": body})
            for conn in WS_CONNECTIONS:
                try:
                    if WS_NAMES.get(conn) == whisper_to:
                        await conn.send_text(whisper)
                except Exception:
                    pass
            return
        # Broadcast chat to all players
        broadcast = json.dumps({"t": "say", "from": char_id or "Player", "text": body})
        for conn in WS_CONNECTIONS:
            try:
                await conn.send_text(broadcast)
            except Exception:
                pass

    elif t == "fog_share":
        # Share the scan chart with a wingman (D4: only the target hears it).
        target_id = msg.get("target")
        if char_id and target_id:
            ok, reason, hits = engine.share_chart(char_id, target_id)
            if not ok:
                await ws.send_text(json.dumps({
                    "t": "toast", "text": reason, "success": False,
                }))
                return
            for conn in WS_CONNECTIONS:
                try:
                    if WS_NAMES.get(conn) == target_id:
                        await conn.send_text(json.dumps({
                            "t": "fog_shared", "from": char_id, "hits": hits,
                        }))
                except Exception:
                    pass
            await ws.send_text(json.dumps({
                "t": "toast", "success": True,
                "text": f"Chart shared with {target_id} ({len(hits)} contacts).",
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