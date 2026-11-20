# Design decisions

The why behind the way Quest for Earth works. Each entry records the
context, the choice, and the consequence — what it forbids going
forward. When a change contradicts a decision here, update the entry
first; the decision log is the contract.

D1–D9 were written for the Return to Mordor build and carry over as
engine principles; the conversion decisions start at D11. D5, D6, D7
and D8 were amended or superseded by the conversion.

## D1 — World content is data, never code

**Context.** The goal is a world that grows while keeping the first
iteration small.

**Decision.** Rooms, exits, spawns, mobs, items, ship classes/types
and combat balance live in `src/quest_for_earth/data/*.json`, loaded
through `world.py` with an mtime-staleness check so files hot-reload.

**Consequence.** Adding a room, a moon or a bug is a JSON edit; no
restart, no code. Engine code may only contain *mechanics* that apply
to all content. Do not special-case specific rooms or mobs in Python.

## D2 — The server is authoritative; clients render

**Context.** Multiplayer combat with visible team actions (repairs
mid-fight) desynchronises instantly if clients simulate.

**Decision.** The engine owns all state and dice: movement, the
target squares, shot judgement, damage, XP, deaths, respawns. Clients
send intents (`move`, `attack`, `fire`, `evade`, `say`) and render the
events they receive. All randomness flows through `engine.rng` so
tests are deterministic.

**Consequence.** The client never judges its own shots — the server
validates every `fire` against the live target square it issued.
Never let a client message change state without server validation.

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
from another room. The documented exception: whispers between
affiliates work system-wide (that is their point).

**Consequence.** "You can only see the fight if you are in it" holds
by construction. Global features (announcements) must be explicit
future additions, not a relaxation of this rule.

## D5 — One origin, no build step *(amended by D18: three.js retired)*

**Context.** The game must run on phone, tablet and PC from one URL
behind NGINX Proxy Manager, and CI must never fetch from CDNs.

**Decision.** The client is static files served by the same FastAPI
app (`/` + `/static`) with no bundler and no framework.

**Consequence.** Deployment is a single container with no CORS and no
proxy rewrites. Keep the client dependency-free — no CDN fetches, no
npm, no build step.

## D6 — Creation: class + hull type *(superseded by D14)*

**Context.** The original Mordor-style race/class creation was
replaced by the space conversion.

**Decision.** Ship creation takes a call sign, a **class**
(Bug / Human / Hybrid) and a **hull type** (Fighter / Frigate /
Warship). The class sets the stat base (Strength, Agility, Repair,
Energy, Tracking); the hull type modifies Agility and the rack sizes.
No free stat spending in v1.

**Consequence.** Frigate and Warship hulls are gated by level
(`min_level` in ships.json) — a new pilot always starts in a fighter
at Titan.

## D7 — Combat is the target-shooter overlay *(superseded by D14)*

**Context.** The tick-based MUD roll system was replaced by an
interactive shooter.

**Decision.** See D14 — the server spawns target squares over the
enemy, the client renders them and fires, the server judges.

**Consequence.** Old d20/vigor math is retired. All combat constants
live in `data/balance.json`.

## D8 — No third-party game content *(historical; fully original now)*

**Context.** The Tolkien-era build shipped an homage starter world;
the setting conversion removed even that.

**Decision.** Everything in the shipped world — rooms, bugs, items,
dialogue — is original content. The "bugs have invaded the solar
system" premise is the game's own.

**Consequence.** No legacy-content caveats remain in the data files.

## D9 — Persistence: SQLite in /data, sessions are opaque tokens

**Context.** "You start back where you left off" is a hard
requirement; the deployment model is one named volume.

**Decision.** Stdlib sqlite3 (no ORM), WAL, one connection per call.
Accounts carry bcrypt hashes; sessions are 32-byte random tokens with
an expiry row. Ship state (stats, guns, plates, artifacts, chart) is
stored as JSON columns and saved on every meaningful mutation.

**Consequence.** A container restart keeps ships standing in their
last room with bugs freshly spawned. Combat progress within a tick
can be lost on crash — acceptable for v1. Old return-to-mordor
volumes are incompatible with the new schema; deployments start
fresh.

## D10 — The SDLC inference layer: no project config, models in the OpenCode GUI

**Context.** The project's agent/inference setup went through several
deliberate states, each recorded here because it was a real choice:
the hybrid-cloud template applied (`b35cd22`), reverted to the bare
global config to match company-sleuth (`00bbfd7`), hybrid re-applied
when the provider spend-guard was wanted (`b1b09c6`), switched to
full-cloud (`ce95a36`), the default model unpinned in favour of UI
control (`a0d64e4`), and finally — Nov 2026 — **the project config
removed entirely**: no spend guard, no model pin, nothing. The game
runtime has no inference dependency in any of these states (D2
stands) — this decision is about how the project is *built*, not
what it runs.

**Decision.** This repo carries **no project-local `opencode.json`**.
Plan and Build models are chosen in the OpenCode interface per
session; provider access follows the global opencode config. The
template system (`sdlc-template.sh new|switch`, TEMPLATES.md in the
global config directory) remains the supported mechanism if a spend
guard or a pinned local builder is ever wanted again — applied
deliberately, recorded here.

**Consequence.** Cost control is the operator's responsibility at
the OpenRouter dashboard, not a config gate. A session whose default
model stops resolving (upstream retirement — see D19) is fixed by
picking a current model in the interface. The game **runtime** has
no LLM dependency (D2) — never add one as a side effect of working
on the project.

## D11 — The setting conversion: bugs won, humanity scavenges

**Context.** The user requested a full setting conversion from the
Tolkien MUD to a space game, reusing the engine constructs, with the
synopsis: "The bugs have invaded the solar system and won. Humanity
lives off of what they kill, steal or scavenge." The repo was fully
renamed (directory, package, image, volume) to quest-for-earth.

**Decision.** The setting is a bug-conquered solar system. Players are
human scavengers in ships; bug NPCs are the primary hostile fauna and
attack on sight. Everything drops something you can use. Tolkien
content is fully retired.

**Consequence.** Game copy, world names and item names are space-themed.
Do not reintroduce fantasy vocabulary. The room-based movement model
and server-authoritative tick carry over unchanged (that was the point
of the reuse).

## D12 — The world graph: jump points on the outside, lanes sized for raids

**Context.** Movement stays room-based, but the map is the solar
system. "To get to Io you would jump to just outside the Jupiter system
first and then take a few room-moves to Io." Lane hops must leave room
for raider encounters.

**Decision.** Every planetary system is a cluster of body rooms
connected by lane rooms, with one **jump point** room on the system's
outer edge. Lane hops between bodies are 2–3 rooms so encounters can
happen en route. Jumps require coordinates (chart entries); v1 ships
the Saturn system only — the jump mechanic works but the beacon chain
beyond is dark until more systems are added.

**Consequence.** Adding a system is: its body rooms + a jump point +
lane rooms in world.json, plus a chart-broker entry if it needs
coordinates. Titan is the only spawn point. The first bug (Space Tick)
is three rooms out from the spawn, matching the onboarding spec.

## D13 — Fog of war: the scanner, and decaying chart knowledge

**Context.** "Within some time, through passive scanning, they should
be able to see their immediate surroundings over time. However this
knowledge is also time limited, since planets and moons all move."

**Decision.** Each ship has a star chart: room → expiry timestamp.
Docking anywhere plots the current room instantly and then reveals one
more body of the same system per round (`scan_ttl_s`, 600 s default).
Chart entries expire; the current room refreshes. Chart brokers grant
permanent entries for their system and sell jump coordinates as
permanent remote entries.

**Consequence.** Orbital motion is *simulated by knowledge decay* —
rooms are static; the map does not physically move. That is the v1
simplification. "Passive scanning" is per-round, not per-second; do
not reveal whole systems at once except through a broker.

## D14 — Shooter combat: server-owned target squares

**Context.** "The attack will appear to the player as a first-person
target shooter. One large enemy on the screen. Your target on the enemy
will move slowly... The player must click on the square to fire. The
damage done will be based on the level of weapons minus the armor that
the person you attack has. Energy based weapons and artifact inflicted
damage are unaffected by armor."

**Decision.** While engaged, the server spawns one target square per
attacker: position `x/y/w` (fractions of the view) and a dwell time
scaled by **Tracking** (`dwell_base_s + trk × dwell_per_trk`). The
client renders the square over the enemy and sends `fire` with x/y
fractions; the server judges expiry and the square's bounds. A hit
deals `max(min_damage, kinetic − armor) + energy + artifacts`, where
kinetic is the sum of kinetic gun levels (a 10x same-level combo
multiplies it by 10) plus `str ÷ str_bonus_div`; energy guns and
artifacts bypass armor. Crits multiply by 2. The next square spawns
immediately after a shot, so a fast clicker lands several shots per
round — that is how a fighter beats a same-level bug in 1–2 rounds
while taking one hit per round.

**Consequence.** The client never judges its own shots (D2). Bug mobs
attack on sight once per round: `max(min_damage, mob_gun − armor)`.
Passive self-repair is class-based: Bug repairs fast (rep/10 per
round), Hybrid medium, Human only lightly while wearing any bug plate.
The energy `repair` action costs energy and heals self or an
affiliate. Evade is an agility roll (`base + agi × per_agi`, capped);
a successful evade drops you in a neighbouring room — larger hulls
have lower agility and evade worse. Do not move hit judgement to the
client.

## D15 — Death: everything drops, one level down

**Context.** "If you die, you lose all your weapons, all your armor,
energy, and artifacts. And, you drop one level." Plus: "When a player
dies, some of their weapons, armor and energy will be left behind."

**Decision.** Death strips all guns, plates, artifacts and energy; a
rounded-down half of the guns and plates is left behind as room
salvage; artifacts are gone entirely; the pilot drops one level (XP
resets to the new level's start) and respawns at Titan with a fresh
starter-class hull integrity. Bug NPC deaths drop per-template loot
tables straight to the room floor; players pick salvage up with
`take` (rack limits enforced).

**Consequence.** Corpse-running is a designed activity; the room's
salvage is public. Never auto-equip drops — taking is always a
deliberate choice, and rack limits apply at pick-up time.

## D16 — Affiliates: follow, 2-way, whisper; audio chat deferred

**Context.** "Players can follow other players. When this happens, the
players can choose to 2-way affiliate. If the players affiliate, they
can now use the chat system to text each other within the game, no
matter their location. I would also like to build in an audio chat
feature here... You cannot attack an affiliate."

**Decision.** `follow` sends a request; `accept` creates the 2-way
affiliation. Affiliated pilots whisper anywhere in the system and
follow the leader room-to-room automatically (including into attacks).
Attacking an affiliate is refused server-side. Voice chat is deferred
to a later iteration — the client ships the text layer only.

**Consequence.** Following is mutual consent, never one-way. When the
leader moves, affiliates in the leader's old room are moved with them
(combat broken). WebRTC audio will be its own decision when it lands.

## D17 — v1 scope: the Saturn system, Titan spawn, the Space Tick

**Context.** The synopsis ends: "Let's first focus on the spawn point:
Titan. Within 3 steps in one direction, we should see our first bug
NPC, The Space Tick... embedded into a nearby asteroid... When you walk
into its room, the player can then choose to attack."

**Decision.** v1 ships the Saturn system only: Titan station (safe,
spawn, chart broker), four neighbouring bodies, the Saturn jump point,
and the riddled asteroid with the Level-1 Space Tick three rooms out —
one L1 gun, one L1 armor, hull equal to a fresh pilot's. Jump
coordinates for Jupiter come from the broker; the jump itself is
dark until more systems exist.

**Consequence.** Frigate/Warship hulls exist in the catalogue but are
level-gated. The Jupiter system, raider lanes between systems, and
more bug tiers are the next content additions — all data-file work
(D1), no engine changes required.

## D18 — The renderer: 2-D canvas room view, retired three.js

**Context.** "When moving room to room, the main screen should be a
2-d view of the map with the other players, objects, or NPCs in the
room." The Mordor client used a vendored three.js 3-D room.

**Decision.** The room view is a plain 2-D canvas (starfield, ship
shapes, bug sprites, salvage markers, exit tags) with no framework
and no vendored 3-D library — three.js is retired. The attack overlay
is a second canvas state drawing the enemy large (first-person target
shooter).

**Consequence.** The client is smaller and fully keyboard/touch
playable. Animation stays local (drift, twinkle, pulses). If a 3-D
view is ever wanted back, it is a new decision, not a revert.

## D19 — Planner model renamed to GLM Flash Latest

**Context.** Upstream retired `z-ai/glm-5.3-flash`; the OpenCode
catalog no longer resolves it, so sessions configured with it fail
at startup.

**Decision.** *(Amended Nov 2026.)* The project's `opencode.json` is
gone entirely (D10) — no model pin, no provider policy. Plan and
Build models are chosen through the OpenCode interface. The hosted
`~z-ai/glm-flash-latest` remains the sensible pick: it resolves where
`z-ai/glm-5.3-flash` no longer does, and it does not bill the
OpenRouter key.

**Consequence.** Model choice lives entirely in the interface. If the
hosted model is ever retired, this decision is revisited — not
silently migrated.
