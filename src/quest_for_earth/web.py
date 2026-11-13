"""HTTP + WebSocket API; serves the static game client.

One origin, one container (D5): the client is static files from the
package, so the NGINX Proxy Manager front-end only needs one proxy
host with WebSockets enabled. The browser never decides game state —
it renders what the engine broadcasts (D2).

Token handling: REST calls send ``X-Token``; the WebSocket takes
``?token=``. Tokens and passwords are secrets and never logged.
"""

import asyncio
import json
import pathlib
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, engine, world
from .auth import AuthError

WEB_DIR = pathlib.Path(__file__).with_name("web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    engine.spawn_all()
    engine.LOOP = asyncio.get_running_loop()
    engine.start_tick()
    try:
        yield
    finally:
        engine.stop_tick()


app = FastAPI(title="quest-for-earth", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health():
    """Liveness probe."""
    return {"ok": True}


@app.get("/")
def index():
    """The game client."""
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---- ship catalogue ---------------------------------------------------------

@app.get("/api/ships")
def ships():
    return world.ships()


# ---- accounts ---------------------------------------------------------------

def _session_or_401(request: Request) -> dict:
    session_row = auth.session(request.headers.get("X-Token", ""))
    if session_row is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return session_row


def _character_view(session_row: dict):
    """Client-facing ship state — same shape as engine._self_view."""
    if not session_row["character_id"]:
        return None
    char = db.character_by_id(session_row["character_id"])
    if not char:
        return None
    ship_type = world.ship_type(char["type"])
    return {
        "id": char["id"], "name": char["name"], "cls": char["cls"],
        "type": char["type"], "level": char["level"],
        "stats": json.loads(char["stats"]),
        "hp": char["hp"], "max_hp": world.max_hull(char["level"]),
        "xp": char["xp"], "xp_next": world.xp_next(char["level"]),
        "guns": json.loads(char["guns"]), "plates": json.loads(char["plates"]),
        "artifacts": json.loads(char["artifacts"]),
        "energy": char["energy"], "energy_max": ship_type["energy_cap"],
        "slots": ship_type["slots"], "room": char["room_id"],
    }


@app.post("/api/register")
def register(payload: dict = Body(...)):
    try:
        token = auth.register(payload.get("username", ""),
                              payload.get("password", ""))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"token": token, "character": None}


@app.post("/api/login")
def login(payload: dict = Body(...)):
    try:
        token = auth.login(payload.get("username", ""),
                           payload.get("password", ""))
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    session_row = auth.session(token)
    return {"token": token, "character": _character_view(session_row)}

@app.get("/api/me")
def me(request: Request):
    session_row = _session_or_401(request)
    return {"character": _character_view(session_row)}


@app.post("/api/character")
def create_character(request: Request, payload: dict = Body(...)):
    session_row = _session_or_401(request)
    try:
        auth.create_character(session_row, payload)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    fresh = db.session(session_row["token"])
    return {"character": _character_view(fresh)}


# ---- game socket ------------------------------------------------------------

@app.websocket("/ws")
async def game_socket(sock: WebSocket):
    await sock.accept()
    session_row = auth.session(sock.query_params.get("token", ""))
    if session_row is None:
        await sock.send_json({"t": "error", "text": "sign in first"})
        await sock.close(code=4001)
        return
    if not session_row["character_id"]:
        await sock.send_json({"t": "error", "text": "create a character first"})
        await sock.close(code=4002)
        return
    try:
        await engine.handle_socket(sock, session_row)
    except Exception:
        pass  # disconnects are normal here
