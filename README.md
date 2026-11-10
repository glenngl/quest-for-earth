# Return to Mordor

A browser multiplayer RPG inspired by the classic Tolkien MUDs —
**Mordor** (mordormud.net) and its sister **Isengard** — rebuilt as a
graphical game that plays in any modern browser: phone, tablet or PC.
One URL, one account, and you pick up exactly where you left off.

![stack](https://img.shields.io/badge/server-fastapi%20%2B%20websockets-blue)
![client](https://img.shields.io/badge/client-three.js%20(vendored%2C%20no%20build%20step)-green)

## What it is

- **First-person-ish rooms**: every room is a torch-lit 3D box; drag to
  look around, animated transitions between rooms.
- **You see your party**: other players appear in the room with you,
  in class colours, with nameplates.
- **Animated combat**: lunges, hit flashes, crits, damage popups — and
  clerics casting **vigor** (heal) show green sparkles on the target,
  visible whenever that actor is on screen.
- **Classic character creation**: name, gender, race (30 races with
  the classic Strength / Intelligence / Wisdom / Dexterity /
  Constitution table), and a two-class class order. You start in the
  newbie school, just like the original.
- **Persistence**: accounts (bcrypt), characters, positions, HP and
  XP live in SQLite under the container's `/data` volume.
- **Server-authoritative**: the engine ticks combat rounds, mob AI,
  aggro, respawns and XP; the browser only renders what it is told.

## Quick start (local dev)

```sh
python -m venv venv
venv/bin/pip install -e .
venv/bin/uvicorn return_to_mordor.web:app --reload
# open http://127.0.0.1:8000 — open a second tab to meet yourself
```

World state lands in `./.data/`; the world content in
`src/return_to_mordor/data/*.json` hot-reloads on change.

## Deploy (Docker, behind NGINX Proxy Manager)

```sh
./run.sh        # builds and runs on port 8010, volume return-to-mordor-data
```

Equivalent manual command (what `run.sh` does after building):

```sh
docker run -d --name return-to-mordor \
  -p 8010:8000 \
  -v return-to-mordor-data:/data \
  --restart unless-stopped \
  return-to-mordor
```

With the published image instead:

```sh
docker run -d --name return-to-mordor \
  -p 8010:8000 \
  -v return-to-mordor-data:/data \
  --restart unless-stopped \
  ghcr.io/glenngl/return-to-mordor:latest
```

**NGINX Proxy Manager**: create a Proxy Host for the game domain,
Forward Hostname `achilles`, Forward Port `8010`, and **enable
WebSockets** in the SSL/features tab — without it the client shows an
empty room. No cache for this host.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | The game client (single page) |
| `/health` | GET | Liveness probe |
| `/api/races` | GET | The 30-race attribute table |
| `/api/classes` | GET | Primary + remort class catalogue |
| `/api/register` | POST | `{username, password}` → `{token}` |
| `/api/login` | POST | `{username, password}` → `{token, character}` |
| `/api/me` | GET | `X-Token` header → `{character}` |
| `/api/character` | POST | Create character (name, gender, race, class1, class2) |
| `/ws?token=…` | WS | The game socket (below) |

### WebSocket protocol

Client → server:

| Message | Payload | Description |
|---|---|---|
| `move` | `dir` | north/south/east/west |
| `look` | — | re-send the room view |
| `attack` | `target` (mob id or null) | engage / break off combat |
| `vigor` | `target` (player name) | cleric heal, cooldown-gated |
| `say` | `text` | room-scoped chat |

Server → client: `welcome`, `room`, `enter`, `leave`, `combat`
(`swing`/`miss`/`cast`/`die`/`spawn`/`aggro` events), `say`, `toast`,
`you`, `error`. Events are room-scoped; see
[ARCHITECTURE.md](docs/ARCHITECTURE.md).

## CI/CD

Same pipeline as company-sleuth: `.github/workflows/docker-publish.yml`
runs the offline smoke test (`scripts/smoke.py`), pip-audit, bandit,
then builds the image, gates it with Trivy (HIGH/CRITICAL) and pushes
`ghcr.io/glenngl/return-to-mordor:{latest,sha-*}`. Dependabot keeps
actions and dependencies current.

## Project layout

```
src/return_to_mordor/
  settings.py   env-first settings, read at call time
  db.py         sqlite: accounts, characters, sessions
  auth.py       bcrypt accounts, sessions, creation rules
  world.py      JSON world loader (hot-reloads on change)
  engine.py     rooms, combat ticks, mob AI, spawns, events
  web.py        HTTP + WebSocket API, serves the client
  data/*.json   world content: rooms, mobs, items, races, balance
  web/          the browser client (index.html, game.js, vendored three.js)
scripts/smoke.py offline smoke test — gates the image build
```

## Legal note

The engine is original code. Tolkien place names (Mordor, Isengard,
Lothlorien…) are used as homage, exactly as the old MUDs did; the
Tolkien Estate holds rights over the setting, and the *actual* Mordor
MUD content remains © MordorMUD / MysticFrost — it is **not** copied.
The shipped world is a small original starter map; replace the data
JSONs to make this world entirely your own.
