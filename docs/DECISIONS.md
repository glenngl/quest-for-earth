# Design decisions

The why behind the way Return to Mordor works. Each entry records the
context, the choice, and the consequence — what it forbids going
forward. When a change contradicts a decision here, update the entry
first; the decision log is the contract.

## D1 — World content is data, never code

**Context.** The goal is a world that grows (the old game had 75+
areas) while keeping the first iteration small.

**Decision.** Rooms, exits, spawns, mobs, items, races, classes and
combat balance live in `src/return_to_mordor/data/*.json`, loaded
through `world.py` with an mtime-staleness check so files hot-reload.

**Consequence.** Adding a room or a mob is a JSON edit; no restart, no
code. Engine code may only contain *mechanics* that apply to all
content. Do not special-case specific rooms or mobs in Python.

## D2 — The server is authoritative; clients render

**Context.** Multiplayer combat with visible team actions (clerics
healing mid-fight) desynchronises instantly if clients simulate.

**Decision.** The engine owns all state and dice: movement, attack
resolution, vigor, XP, deaths, respawns. Clients send intents (`move`,
`attack`, `vigor`, `say`) and render the events they receive. All
randomness flows through `engine.rng` so tests are deterministic.

**Consequence.** Client-side prediction is off the table for v1; room
transitions are fade-and-rebuild. Never let a client message change
state without server validation.

## D3 — Settings and env are read at call time

**Context.** The company-sleuth D2 lesson, inherited wholesale.

**Decision.** No module reads env or settings at import time. Consumers
call `settings.get(...)` inside the function that needs it.

**Consequence.** Ports, tick interval and world paths can change via
environment without code churn. Bootstrap facts (bind address, port)
are resolved in `__main__` only.

## D4 — Events are room-scoped

**Context.** The old MUD's charm is a shared room, not a shared
server-wide feed.

**Decision.** Every combat, enter/leave and say event broadcasts to
exactly one room's sockets. Server→client messages never leak actors
from another room.

**Consequence.** "You can only see the fight if you are in it" holds
by construction. Global features (announcements) must be explicit
future additions, not a relaxation of this rule.

## D5 — One origin, vendored client, no build step

**Context.** The game must run on phone, tablet and PC from one URL
behind NGINX Proxy Manager, and CI must never fetch from CDNs.

**Decision.** The client is static files served by the same FastAPI
app (`/` + `/static`), three.js r160 is vendored into the package,
and there is no bundler.

**Consequence.** Deployment is a single container with no CORS and no
proxy rewrites; upgrading three.js means updating the vendored file in
one commit. Keep the client dependency-free beyond three.js.

## D6 — Creation mirrors the classic rules

**Context.** The old Mordor help: race determines attributes; the
primary class grants +1 to its key stat; you begin in the newbie
school; class *order* (primary + secondary) defines the character.

**Decision.** Creation takes name, gender, race (30-entry stat table
taken from the public Mordor help page), primary and secondary class
of the five primary classes. Attributes derive from the race table
plus the primary bonus; no free stat spending in v1.

**Consequence.** Remort classes (knight, strider, assassin, wizard,
priest) exist in the catalogue but are not selectable until remorting
exists. Stat training in-game is the expected later extension, not
extra creation options.

## D7 — v1 combat is deliberately simple

**Context.** Minimum code first; the fight must read well visually
even with simple math.

**Decision.** One tick per combat round (900 ms, `settings.tick_ms`).
Attack: d20 + level + dex/2 vs 11 + level/2 + armor; damage = d(2–5) +
weapon + str/4; natural 20 doubles. Vigor: heal 6 + wis/2 + level×2 on
a 6 s cooldown, cleric-only. Kill XP: 20 + mob level×15; level up every
150×level XP to a full heal. All constants live in `data/balance.json`.

**Consequence.** Rescue, snares, bows, mana and mob wandering are
later work — extend the event vocabulary (`combat.events`) rather than
reusing kinds for new meanings.

## D8 — The real Mordor content is not copied

**Context.** The request said "copy the exact same map, monsters and
items from Mordor"; that database is not public and is © MordorMUD /
MysticFrost, and the setting itself belongs to the Tolkien Estate.

**Decision.** The engine is original. The shipped world is a small
original starter map whose areas nod to the classic ones; the race
table copies *facts* (attribute numbers) from the public help pages,
not creative text. Content is data (D1), so the shipped world can be
replaced wholesale — by importing converted Mordor data for private
use, or by original content.

**Consequence.** Do not paste MUD help text, descriptions or area
maps into the data files. Keep the homage notice in README.md and the
client footer.

## D9 — Persistence: SQLite in /data, sessions are opaque tokens

**Context.** "You start back where you left off" is a hard
requirement; the deployment model is one named volume.

**Decision.** Stdlib sqlite3 (no ORM), WAL, one connection per call.
Accounts carry bcrypt hashes; sessions are 32-byte random tokens with
an expiry row; character position/HP/XP save on every move, death,
level-up and disconnect.

**Consequence.** A container restart keeps characters standing in
their last room with mobs freshly spawned. Combat progress within a
tick can be lost on crash — acceptable for v1; do not move game state
into memory-only structures without a save hook.

## D10 — The SDLC inference layer is the global opencode config

**Context.** The development workflow needed the same agent/inference
setup company-sleuth uses. The hybrid-cloud template was applied
initially (scaffold commit `b35cd22`, adding a project
`opencode.json` and `docs/EXECUTION-PROTOCOL.md`), then reverted:
sleuth itself runs on the global config, and this project should
match. The game runtime has no inference dependency either way (D2
stands) — this decision is about how the project is *built*.

**Decision.** No project-local `opencode.json`. Every session on this
repo uses the global opencode config: the instruction files
(web-search fallback, project standards), the MCP servers (github,
tavily), and the registered Ollama provider on bean:11434. The
template system (`sdlc-template.sh new|switch`, TEMPLATES.md) remains
the supported mechanism if this project ever needs a template — e.g.
the hybrid-cloud local builder or the full-cloud provider spend
guard — applied deliberately per project.

**Consequence.** The project has no local builder agent or provider
policy guard: model selection is whatever the session chooses, and
the deny-list spend guard only exists inside the templates, not in
the global config. Behaviour now matches company-sleuth exactly.
`docs/EXECUTION-PROTOCOL.md` was removed with the revert (its
builder-dependent protocol is meaningless without the hybrid
project config); the protocol text still lives in the global
templates directory if a template is ever applied again.
