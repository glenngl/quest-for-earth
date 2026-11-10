# Architecture

## Module map

```
                ┌────────────────────────────────────────────────┐
                │                 web.py (FastAPI)               │
   browser ────►│  GET / + /static  ·  /api/*  ·  WS /ws         │
                └───────┬───────────────┬───────────────┬────────┘
                        │               │               │
                        ▼               ▼               ▼
                   auth.py          engine.py        world.py
                (accounts,       (the game:      (JSON loader,
                 sessions,        actors, rooms,  hot-reloads
                 creation         combat ticks,   data/*.json)
                 rules)           mob AI, spawns)
                        │               │
                        ▼               ▼
                      db.py ──── settings.py
                  (sqlite in /data)  (env-first, call time)
```

- **settings.py** — `R2M_*` env vars over defaults; read at call time
  (D3). Paths: `db_path()` (SQLite in `/data`), `data_dir()` (JSONs).
- **db.py** — accounts / characters / sessions tables, one short
  connection per call, WAL (D9).
- **auth.py** — bcrypt hashing, session tokens, creation validation
  against the classic rules (D6).
- **world.py** — the only module that touches `data/*.json`; rooms,
  mobs, items, races, classes, balance; derived stats (`max_hp`,
  `xp_next`, `xp_reward`) read `balance.json` (D7).
- **engine.py** — authoritative game state (D2): `PLAYERS`, `MOBS`,
  `SPAWNS`, room-socket tables; `tick_once()` (combat rounds, aggro,
  respawns); broadcasts room-scoped events (D4).
- **web.py** — REST for accounts/creation/catalogue, WebSocket at
  `/ws?token=` delegating to `engine.handle_socket`, static client.
- **web/** — single-page client: `index.html` + `style.css` +
  `game.js` (three.js r160 vendored, D5).

## Data flow (one move)

1. Browser: drag look, tap ▲ on the d-pad → `{"t":"move","dir":"south"}`.
2. `engine._dispatch` → `_move`: validates the exit in `world.room()`,
   breaks combat, swaps the socket between room tables, saves the
   character row.
3. Engine broadcasts `leave` to the old room, `enter` to the new room,
   and sends the `room` snapshot to the mover only.
4. Browser: fades to the room tint, rebuilds the 3D room box, places
   every actor from the snapshot, keeps actors alive via later events.

## Data flow (one combat round)

1. `engine.tick_loop` sleeps `tick_ms`, then calls `tick_once()` (the
   smoke test calls it directly with the loop stopped).
2. Engaged mobs and players swing: `_attack` rolls `engine.rng`,
   applies damage, broadcasts a room-scoped `combat` event
   (`swing`/`miss`, with the victim's HP so any HUD can update).
3. Idle mobs aggro the first player in their (unsafe) room; dead spawn
   points rebuild after their respawn delay.
4. Deaths: mobs award XP + loot auto-equip and schedule their respawn;
   players wake up in the newbie school at quarter health (D7).
5. Clients render: lunge tween, hit flash, damage popup; a cleric's
   `cast` event plays the vigor sparkle burst on its target — visible
   to anyone whose camera has that actor on screen.

## Event vocabulary (server → client)

| Kind | Fields | Client rendering |
|---|---|---|
| `swing` | attacker, victim, dmg, crit, victim_hp/max | lunge + red flash + damage popup |
| `miss` | attacker, victim | grey "miss" popup |
| `cast` | caster, spell, target, amount, target_hp/max | green particle burst + `+N` popup |
| `die` | who, by, xp | sink + fade, XP toast for the killer |
| `spawn` | who, level | toast (pop-in handled on room rebuild) |
| `aggro` | who, on | log line for the victim |

## Persistence model

`accounts` (bcrypt hash) 1—N `characters` (stats, hp/xp/level,
room_id, weapon/armor) 1—N sessions (opaque token, expiry). The
character row is written on every move, kill, level-up, death and
disconnect; the container restart re-spawns mobs from world data but
characters stand exactly where they logged out (D9).
