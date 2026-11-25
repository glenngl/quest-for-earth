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

Client → server: `set_name` (first message), `move` (`dir` or a 3-D `vec` thrust
intent), `look`, `fire` (`target`: prototype direct fire, range/cooldown
judged by the server), `harvest` (debris id), `repair` (`target`, `amount`:
energy → teammate shields), `evade`, `scan`, `take` (salvage item id),
`use` (spend a carried artifact), `affiliate` (`target` pilot name, or
omitted to clear), `fog_share` (`target`: send your chart to a wingman),
`chat` (`/w <pilot> <text>` is a whisper), `enter_portal`, `die`, `loot`.
`engage` (`target` pilot name: declare a mutual PVP fight — both pilots
get an `engaged` frame), `disengage` (break it).

Server → client: `welcome` (your ship, the world, the debris field, the mob
roster and the squad roster), `vitals` (store changes, rank and hold),
`pos` (moves and rock-gravity drift), `combat` (`shot` between pilots,
`mob_hit` NPC attacks, `collision` rock impacts), `mobs` (the enemy roster,
resent when a mob dies or respawns), `mob_pos` (a mob closed on you),
`loot` (salvage dropped on death, with the position it fell at),
`debris_gone` (harvested, or blown up), `scan_result`, `fog_shared`,
`repair_result`, `say`, `toast`, `you`,
`join`, `leave`, `affiliate`, `fog_shared`, `engaged`, `disengaged`.
Events are room-scoped; see [ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Level 1 WebGL client

The current client is the WebGL Level 1 prototype (no auth). Connect with
`/?name=<name>`. Controls: click the canvas for mouse look (or use the arrow
keys) to aim the view; hold `W`/`S` to thrust forward/back **along where you
are looking** — pitch down and thrust to dive toward the spawn exit. `A`/`D`
strafe west/east, `Space`/`Shift` rise/sink, `E` enter the wormhole, `F`
harvest nearby debris, click the canvas to fire at the nearest in-range
target (teammate or mob), `Q` evade, `C` scan (reveals nearby debris and
mobs; contacts decay, so re-scan), `G` grab salvage lying nearby, `T`
affiliate with a squadmate (or clear the affiliation), `R` burn an energy
cell for shields, `Y` share your chart with a wingman, `B` spend energy to
repair the nearest hurt wingman. `X` declares a mutual fight (PVP) with
the nearest other pilot — click or shoot to engage, `X` again to disengage.
While a fight is on, both ships glide toward each other on autopilot
(server-side, D56): stop flying and the dogfight closes on its own;
press `W` to fly your own line. Whatever the gun would hit shows corner
brackets plus an amber lead pip (where the target drifts next, D57), and
a kill lands as a gold "TARGET DESTROYED" banner with a screen wash. A
fight survives a death — the pair rematches when the downed pilot
respawns — and only leaving the world ends it.
In chat,
`/w <pilot> <message>` sends a private whisper. The camera opens in third
person at spawn, then zooms
into first person; press `V` to toggle between the two views, `M` to trim HUD
detail and `F2` (or the button) for fullscreen. The cockpit HUD uses fixed
zones: your ship's gauges bottom-left, a tactical map with teammates and
bearing lines bottom-right, squad status across the top. A deterministic field of lumpy
debris is scattered between spawn and the wormhole (exact counts in
`data/world.json` → `debris_field`: 4 rock, 4 methane, 3 metal); the colour
says what it is: earth-brown **rock** (inert, but massive), cyan **methane
ice**, and silver **metal ore**. Each item carries a density and each chunk a
size, so mass is `density × size³`: the metal ore is dense *and* large (the
heaviest thing in the field), the methane ice is light. Mass drives gravity,
so a passing fighter curves around the metal ore and barely notices the ice.
Rocks are solid: hitting one bounces you off with damage that scales with
mass and closing speed, and a head-on impact at full speed into a heavy
enough rock blows it apart. The `welcome` frame carries `portal` (`{pos, front, aperture}`),
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

The combat loop is server-authoritative end to end: the engine keeps one NPC
per connected pilot alive near the exit portal, judges every shot against
range and cooldown, and routes an NPC's killing blow through the same death
path as a pilot's. A mob that dies drops salvage *where it fell* — the
client marks it on the tactical map and you fly to the wreck and press `G`
to grab it. The server resends the enemy roster whenever a mob dies or
respawns, so a destroyed enemy stops being a target. Sound is synthesised in
the browser (WebAudio, no asset files); combat feedback shows on the HUD as a
hit marker at the gunsight rather than in the chat transcript.

The Level 1 arc: press `C` to scan the field (contacts decay, so re-scan),
thread the debris field — the rocks are solid, and a head-on impact at full
speed into a heavy one blows it apart — destroy the NPC guarding the
wormhole, grab the salvage it drops, and the wormhole opens so you can
press `E` and leave through it. An objective banner above the squad panel
and a waypoint at the edge of the screen keep the next step in view.

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
scripts/playtest.py  gameplay playtest — pacing, firing, thrust, gravity
scripts/loadtest.py  capacity probe (N concurrent players)
```

## History

This project began as **Return to Mordor**, a Tolkien-themed browser
MUD inspired by the classic BBS games Mordor and Isengard — same
engine concepts (room-based movement, tick combat, server
authoritative state), new setting. The Mordor decision log (D1–D9)
still applies; the conversion decisions start at D10.
