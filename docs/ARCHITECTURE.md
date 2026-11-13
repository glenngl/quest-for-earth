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
- **Tick loop** (`tick_once`): bug aggro → scheduled combat →
  respawn timers → passive scans. The tick interval is
  `settings.DEFAULTS["tick_ms"]` (1500 ms).
- **Combat is the shooter overlay** (D14): the server owns the target
  square (position `x/y/w`, dwell time), the client renders it and
  sends `fire` with x/y fractions; the server judges the hit against
  the square. Kinetic damage is reduced by the target's armor; energy
  and artifact damage bypass armor. Bug mobs attack on sight.
- **Death** (D15): all guns/plates/energy/artifacts are dropped, some
  as salvage where you fell, and one level is lost. You respawn at
  Titan.
- **Affiliates** (D16): follow requests, 2-way affiliation, follow-into
  attacks, whisper anywhere. You cannot attack an affiliate.

## Persistence

SQLite, one file, WAL mode. `accounts` (bcrypt), `sessions`
(opaque tokens), `characters` (JSON columns: stats, guns, plates,
artifacts, chart). `save_character` is called on every meaningful
mutation — the server can restart at any time. In the container the
file lives on the `quest-for-earth-data` volume.

## The client

`web/game.js` — no framework, no build step. The room view is a 2-D
canvas; the shooter overlay is a second canvas state. Every state
change comes from the server stream; the client interpolates movement
and animation locally (D2). The client is pixel-identical on mobile,
desktop, and anything else — the D-pad is the primary input, the
shooter overlay targets are click/tap.

## Security

- Passwords are bcrypt-hashed; sessions are 128-bit opaque tokens.
- The server judges every shot; the client never moves actors.
- Room-scoped events: a socket only receives combat/say events for
  rooms it is in. Whisper to affiliates is the documented exception.
- Secrets never appear in logs or payloads (D9).
