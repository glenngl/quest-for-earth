# Quest for Earth

A browser multiplayer space-scavenger RPG. **The bugs have invaded the
solar system and won.** Humanity lives off what it kills, steals or
scavenges. One URL, one account, and you pick up exactly where you
left off.

![stack](https://img.shields.io/badge/server-fastapi%20%2B%20websockets-blue)
![client](https://img.shields.io/badge/client-2D%20canvas%20%2B%20shooter%20overlay-green)

## What it is

- **Room-based solar system**: planets, moons, lanes and jump points
  are rooms. The main screen is a 2-D view of the room with every
  ship, bug and piece of salvage in it.
- **Passive scanning & fog of war**: docking anywhere plots nearby
  bodies onto your star chart over time — but the system moves, so the
  knowledge decays. Chart brokers sell permanent coordinates.
- **Jump points**: every planetary system's gate sits on its outside —
  outer moons are fewer hops than inner ones. Jumps need coordinates.
- **Bug NPCs attack on sight**, drop weapons, armor, energy and
  artifacts; a Level-1 bug drops Level-1 gear, and ten same-level guns
  give a **10x weapon combo**.
- **First-person attack mode**: engaging anything switches to a
  target-shooter view — one large enemy, a drifting target square, click
  to fire. Kinetic damage is reduced by the target's armor; energy and
  artifact damage go straight through.
- **Evade**: jump out of a losing fight to a nearby room — agility
  roll, bigger hulls are worse at it.
- **Affiliates**: follow each other (2-way affiliation), follow into
  attacks, whisper anywhere in the system. You cannot attack an
  affiliate. (Voice chat: next iteration.)
- **Death costs everything**: all guns, plates, energy and artifacts —
  some salvage left behind where you fell — plus one level.
- **Ships**: class (Bug / Human / Hybrid) sets the stats; hull type
  (Fighter / Frigate / Warship) sets agility and rack sizes. Everyone
  starts in a fighter at **Titan** — one L1 gun, one L1 armor, and a
  Space Tick three rooms out.

## Quick start (local dev)

```sh
python -m venv venv
venv/bin/pip install -e .
venv/bin/uvicorn quest_for_earth.web:app --reload
# open http://127.0.0.1:8000 — open a second tab to meet yourself
```

World state lands in `./.data/`; the world content in
`src/quest_for_earth/data/*.json` hot-reloads on change.

## Deploy (Docker, behind NGINX Proxy Manager)

```sh
./run.sh        # builds and runs on port 8010, volume quest-for-earth-data
```

Equivalent manual command:

```sh
docker run -d --name quest-for-earth \
  -p 8010:8000 \
  -v quest-for-earth-data:/data \
  --restart unless-stopped \
  quest-for-earth
```

With the published image instead:

```sh
docker run -d --name quest-for-earth \
  -p 8010:8000 \
  -v quest-for-earth-data:/data \
  --restart unless-stopped \
  ghcr.io/glenngl/quest-for-earth:latest
```

**NGINX Proxy Manager**: create a Proxy Host for the game domain,
Forward Hostname `achilles`, Forward Port `8010`, and **enable
WebSockets** — without it the client hangs on a black screen. No cache
for this host. Old return-to-mordor volumes are not compatible with the
Quest for Earth schema; start fresh.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | The game client (single page) |
| `/health` | GET | Liveness probe |
| `/api/ships` | GET | Ship classes and hull types |
| `/api/lighting` | GET | WebGL per-vertex light model (`data/lighting.json`) |
| `/api/register` | POST | `{username, password}` → `{token}` |
| `/api/login` | POST | `{username, password}` → `{token, character}` |
| `/api/me` | GET | `X-Token` header → `{character}` |
| `/api/character` | POST | Create ship (name, cls, type) |
| `/ws?token=…` | WS | The game socket (below) |

### WebSocket protocol

Client → server: `move`, `look`, `attack`, `fire` (x/y fractions),
`harvest` (debris id), `repair` (`target`, `amount`: energy → teammate shields),
`evade`, `take`, `talk`, `chart`, `jump`, `say`, `whisper`,
`follow`, `accept`, `decline`, `unfollow`.

Server → client: `welcome`, `room`, `enter`, `leave`, `combat`
(`swing`/`die`/`spawn`/`aggro`/`repair`), `target`, `scan`, `chart`,
`loot`, `say`, `whisper`, `toast`, `you`, `follow_request`, `error`,
`debris_gone` (with the harvester's `vitals`), `vitals` (per-second store
changes), `pos` (moves and rock-gravity drift).
Events are room-scoped; see [ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Level 1 WebGL client

The current client is the WebGL Level 1 prototype (no auth). Connect with
`/?name=<name>`. Controls: `WASD` move, `Space`/`Shift` up/down, `E` enter
the wormhole, `F` harvest nearby debris. A deterministic field of lumpy
larger rocks with crystal deposits are scattered between spawn and the
wormhole (exact counts in `data/world.json` → `debris_field`: 4 rock, 4 methane,
3 metal); the crystal colour says what it is: earth-brown **rock** (inert, but
massive), cyan **methane ice**, and silver **metal ore**. Rocks all share one
size. The `welcome` frame carries `portal` (`{pos, front, aperture}`),
`spawn_portal` (the warm wormhole you arrive from), `debris` (with each rock's
gravity `size`) and the ship's `vitals`; spawn and portal positions come from
`data/world.json`, not from the client. The chase camera rotates with the
ship, so when you turn back toward spawn the view turns with you.

Each ship carries 100-point HP, shields, energy, methane and metal stores
(caps in `data/balance.json`). Harvesting methane or metal fills its tank;
rock cannot be mined and stays in place. Every second the server converts
1 methane → 1 energy → 1 shield (your own shields first, then a teammate
within `nearby_radius`), and 1 metal → 1 HP with the same priority. The
`repair` action is the explicit targeted form: spend energy to top up a
nearby wingman's shields. Inert rocks have a size-driven gravity well that
nudges your trajectory as you fly past; the server applies the drift and
broadcasts the new position. All of it is authoritative (D2) — the HUD on the
mini-map just renders the `vitals` frames. Building placement is deferred.

Meshes are shaded with a per-vertex light model: each triangle carries flat
face normals in a parallel buffer alongside its existing position buffer, and
the shader combines ambient, Lambertian diffuse and Blinn–Phong specular from
a single light direction. The light direction, view direction and material
intensities come from `data/lighting.json` (served via `/api/lighting` and
injected into the page) — editing that JSON re-skins every mesh without a
renderer code change. Glowing effects (wormhole, engine nozzles) bypass the
light model and keep their authored colours.

Client → server additionally accepts `enter_portal` (intent only). The engine
rejects it unless the pilot is inside the aperture on the portal's
spawn-facing side; a successful entry currently just returns the pilot to
spawn (level progression is deferred). Rejections come back as a `toast`.

## CI/CD

Same pipeline as company-sleuth: `.github/workflows/docker-publish.yml`
runs the offline smoke test (`scripts/smoke.py`), pip-audit, bandit,
then builds the image, gates it with Trivy (HIGH/CRITICAL) and pushes
`ghcr.io/glenngl/quest-for-earth:{latest,sha-*}`. Dependabot keeps
actions and dependencies current.

## Project layout

```
src/quest_for_earth/
  settings.py   env-first settings, read at call time
  db.py         sqlite: accounts, characters (JSON ship state), sessions
  auth.py       bcrypt accounts, sessions, ship-creation rules
  world.py      JSON world loader (hot-reloads on change)
  engine.py     rooms, scanner, shooter combat, bug AI, affiliates, deaths
  web.py        HTTP + WebSocket API, serves the client
  data/*.json   world: Saturn system, ships, items, mobs, balance, lighting
  web/          the browser client (2-D canvas, no framework, no build)
scripts/smoke.py offline smoke test — gates the image build
scripts/loadtest.py  capacity probe (N concurrent players)
```

## History

This project began as **Return to Mordor**, a Tolkien-themed browser
MUD inspired by the classic BBS games Mordor and Isengard — same
engine concepts (room-based movement, tick combat, server
authoritative state), new setting. The Mordor decision log (D1–D9)
still applies; the conversion decisions start at D10.
