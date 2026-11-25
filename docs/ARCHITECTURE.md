# Architecture

One FastAPI process serves the static client and the game socket. The
server is authoritative (D2): the client renders what it is told and never
moves actors, judges its own shots or decides what it hit.

```
Browser (any device)                      Server (one container)
┌─────────────────────────┐               ┌──────────────────────────────┐
│ WebGL Level 1 cube      │  WebSocket    │ web.py    HTTP + socket      │
│ cockpit HUD, tactical   │◀─────────────▶│  engine.py   the simulation  │
│ map, objective, waypoint │  JSON events  │  world.py    data loading    │
└─────────────────────────┘               │  data/*.json  the content    │
                                          └──────────────────────────────┘
```

## Modules

| Module | Job |
| --- | --- |
| `web.py` | HTTP routes (`/`, `/health`, `/api/ships`, `/api/lighting`) and the `/ws` game socket. Owns the background tick driver and the frame broadcast. |
| `engine.py` | The authoritative simulation: pilots, NPCs, debris, salvage, combat, collisions, death, respawn. Plain dicts, one process, one event loop. |
| `world.py` | Loads `data/*.json` and hot-reloads it when the file changes. Every content question ("how big is a rock", "what does a shot cost") is answered here, never in the engine code. |
| `settings.py` | Environment/config access. Nothing reads env or settings at import time (D3). |
| `web/game.js` | The client: WebGL renderer, camera, input, HUD, audio. No framework, no build step. |

`db.py` and `auth.py` exist for the account layer but are **not wired into
the Level 1 app** — there is no login yet.

## The Level 1 world

One continuous 64×64×64 cube with a warm exit wormhole at the spawn end
and a cool entrance wormhole at the far end. Everything in it — pilots,
the NPC roster, salvage — shares the room id `level_1`; the two room
records in `world.json` describe the two *ends* of the cube, not separate
event rooms (D46).

- **Debris field** (`world.json` → `debris_field`): exact counts (4 rock,
  4 methane, 3 metal), a seed so every client sees the same field, and a
  size per material. Each item carries a `density`, so mass is
  `density × size³` — the metal ore is dense *and* large (the heaviest
  thing in the field), the methane ice is light (D38).
- **NPC roster**: one mob per player near the wormhole
  (`mobs.json` → `spawn_defaults.count_per_player`), stable ids so a
  killed mob respawns on its own timer instead of doubling the roster.
- **Lighting** (`lighting.json`): light direction, ambient/diffuse/
  specular intensities and fog, served to the client and injected into
  the shader — editing the JSON re-skins every mesh (D24).

## The engine

- `PLAYERS` (char id → actor), `MOBS` (instance id → actor), `ROOM_LOOT`
  (room id → salvage), `LAST_FIRE`, `MOB_TIMERS`, `DEATH_TIMERS`.
- **Tick loop** (`tick_once`, every `tick_ms` = 100 ms → 10 TPS):
  1. NPC respawn timers
  2. passive shield regeneration
  3. resource conversion (once per second, not every tick)
  4. debris gravity (mass-driven drift)
  4b. fight autopilot — idle engaged pilots glide toward their partner (D56)
  5. NPC engagement — mobs fire on their own cooldown
  5b. mobs close on the pilots that engage them
  5c. speed measurement, then hard contact with debris
  6. death timers → respawn
- **Movement**: `apply_move` takes a thrust *intent* and owns the result —
  it clamps to the cube and tracks the velocity the move implies. A move
  intent also marks `LAST_MOVE`, which silences the fight autopilot for
  that pilot (D26/D56).
- **Combat**: the client names a target, the server judges range,
  cooldown and damage, and the hit applies shields-before-HP. NPC hits
  land on the pilot the same way; a killing blow goes through
  `cause_death` so death, salvage and respawn stay in one place.
- **Collisions**: a hard contact with debris bounces the ship and hurts in
  proportion to mass × closing speed. A head-on impact at full speed into
  a heavy enough chunk blows it up, with a splash.
- **Death**: salvage drops where the agent fell (with its position), the
  killer takes the victim's shields and XP, the victim loses a slice of
  their XP, and the respawn timer is armed. A respawn is a teleport, so it
  resets the velocity — otherwise it reads as a colossal impact.
- **Affiliates**: a pilot can trust another, share a slice of what that
  partner earns, whisper them anywhere and share their chart.

## The client

`web/game.js` draws the cube, both wormholes, the debris, the fighter and
a minimal cockpit (a thin canopy rim and a gunsight); the HTML HUD draws
the fixed readouts. Every state change comes from the server stream. The
renderer also runs an optional single-pass sky-bloom post-pass (D53):
opaque scene into a float FBO, x-step blur + Reinhard tonemap, then the
live starfield redrawn on top; degrades to the single-pass path on any
WebGL failure. Renderer visuals (lighting, sky_bloom) come from
`lighting.json`.

**Fixed HUD zones** — the eye finds the same information in the same place:

| Zone | Content |
| --- | --- |
| top centre | objective banner, then squad status (affiliate marked ⭐) |
| bottom left | own ship: hull, shields, energy, methane, metal, rank + XP bar, energy cells |
| bottom right | tactical map: debris (sized by mass), NPCs, salvage, portals, teammates, bearing lines, shared contacts |

Overlays: hit marker at the gunsight, damage-direction chevron, waypoint to
the objective, damage vignette, death overlay with a respawn countdown.

**Sound** is synthesised in the browser with WebAudio (no asset files):
shoot, hit, kill, harvest, evade, death, level-up, impact, explosion, scan,
wormhole, plus a looping engine bed whose gain follows the thrust level.

## Persistence

None in Level 1. The engine holds the live world in memory and the data
files hold the content; `db.py` (SQLite) and `auth.py` (bcrypt accounts,
opaque session tokens) are staged for the account layer.

## Security

- The server judges every shot, every collision and every pickup; the
  client only expresses intent (D2).
- Events are room-scoped (D4, amended by D46); whispers to affiliates are
  the documented exception.
- Secrets never appear in logs or payloads (D9).
