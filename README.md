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
| `/api/register` | POST | `{username, password}` → `{token}` |
| `/api/login` | POST | `{username, password}` → `{token, character}` |
| `/api/me` | GET | `X-Token` header → `{character}` |
| `/api/character` | POST | Create ship (name, cls, type) |
| `/ws?token=…` | WS | The game socket (below) |

### WebSocket protocol

Client → server: `move`, `look`, `attack`, `fire` (x/y fractions),
`evade`, `repair`, `take`, `talk`, `chart`, `jump`, `say`, `whisper`,
`follow`, `accept`, `decline`, `unfollow`.

Server → client: `welcome`, `room`, `enter`, `leave`, `combat`
(`swing`/`die`/`spawn`/`aggro`/`repair`), `target`, `scan`, `chart`,
`loot`, `say`, `whisper`, `toast`, `you`, `follow_request`, `error`.
Events are room-scoped; see [ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
  data/*.json   world: Saturn system, ships, items, mobs, balance
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
