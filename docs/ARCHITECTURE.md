# Architecture

One FastAPI process serves the static client and the game socket.
The server is authoritative (D2): the client renders what it is
told and never moves actors or judges its own shots.

```
Browser (any device)                      Server (one container)
┌─────────────────────────┐               ┌──────────────────────────────┐
│ 2-D canvas room view    │  WebSocket    │ engine.py                    │
│ shooter overlay         │◀─────────────▶│  PLAYERS, MOBS, ROOM_LOOT    │
│ HUD, chart, chat        │  JSON events  │  tick loop: aggro, combat,   │
└─────────────────────────┘               │  spawns, scans               │
                                          │ world.py ← data/*.json       │
                                          │ db.py (SQLite)  auth.py      │
                                          └──────────────────────────────┘
```

## The world graph

Rooms are solar-system bodies and lanes. The player-side room view is
a 2-D canvas: ships as capsules, bugs as blobs, salvage as diamonds;
exits are labelled doorways. The graph lives in `data/world.json`:

- every planet system has a **jump point** on its outer edge —
  reachable from the neighbouring system's gate by a few lane hops;
- outer moons are fewer hops than inner ones (Enceladus is closer to
  the gate than Mimas);
- **Titan** is the only spawn point (`starting_room`) with a safe
  station; the first bug (Space Tick) is three rooms east.

The scanner: docking anywhere plots nearby same-system bodies onto the
star chart one round at a time (`scan_ttl`), and the chart entries
expire — bodies move. Chart brokers sell permanent coordinates.

## The engine

- `PLAYERS` (char id → actor), `MOBS` (instance id → actor),
  `ROOM_LOOT` (room id → salvage), `FOLLOW` links, `TARGETS` (live
  target squares). All plain dicts; one process, one event loop.
- **Tick loop** (`tick_once`): mob engagement (aggro → attack cooldown)
  → death timers → passive shield regen → resource conversions → rock
  gravity. The tick interval is the data-driven `tick_ms` from
  `balance.json` (100 ms → 10 TPS).
- **Combat** (D14 simplified, now Level 1 prototype): the server owns the
  shot — a client-picked target id (pilot or mob instance) is judged
  against `fire_range`/`fire_cooldown_ms` and the hit applies
  shields-before-HP. Kinetic damage is reduced by the target's armor; energy
  and artifact damage bypass armor. Bug mobs attack on sight via
  `_npc_combat`.
- **Death** (D15): salvage drops into the victim's room (`loot`), the
  winner takes the victim's shields/XP, and the dead pilot respawns via the
  tick-driven death timer. You respawn at Titan.
- **Affiliates** (D16): follow requests, 2-way affiliation, follow-into
  attacks, whisper anywhere. You cannot attack an affiliate.

## Persistence

SQLite, one file, WAL mode. `accounts` (bcrypt), `sessions`
(opaque tokens), `characters` (JSON columns: stats, guns, plates,
artifacts, chart). `save_character` is called on every meaningful
mutation — the server can restart at any time. In the container the
file lives on the `quest-for-earth-data` volume.

## The client

`web/game.js` — no framework, no build step. The WebGL scene draws the
cube, wormholes, debris, fighter and cockpit frame; the HTML cockpit HUD
draws the fixed readouts (squad, own ship, tactical map). Every state
change comes from the server stream; the client interpolates movement
locally (D2). The client is pixel-identical on mobile, desktop, and
anything else — the D-pad is the primary input, the tactical map and
click-to-fire are the primary combat input.

## Security

- Passwords are bcrypt-hashed; sessions are 128-bit opaque tokens.
- The server judges every shot; the client never moves actors.
- Room-scoped events: a socket only receives combat/say events for
  rooms it is in. Whisper to affiliates is the documented exception.
- Secrets never appear in logs or payloads (D9).
