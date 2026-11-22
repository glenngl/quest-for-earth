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

**Amended by D46 — Level 1 is one space.** The Level 1 cube has a spawn
end and a wormhole end; they are the two ends of one continuous space, so
everything that exists there (pilots, NPCs, salvage) shares the room id
`level_1` and the rule holds trivially. The two room records in
`world.json` describe the ends of the cube, not separate event rooms. The
rule bites the moment the world gains genuinely separate spaces (jump
points), which is when the broadcast helper must learn to filter.

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

## D20 — Wormholes at both ends; the view follows the ship *(amended)*

**Context.** The Level 1 portal was a cube at the cube centre (32,32,32)
and could be approached from any direction. The first fix parked the camera
on a fixed spawn→portal axis so the view never spun — but that made the ship
appear to fly *backwards* on the return leg, because the view kept facing the
wormhole while the hull travelled toward spawn. The user wants the view to
follow the ship: when it faces spawn, the camera faces spawn.

**Decision.** Spawn and portal positions live in `data/world.json`
(`spawn_position` `[32,12,6]`, `portal_position` `[32,32,58]`,
`portal_front` `-z`, `portal_aperture` `4`) and both are sent in `welcome`.
The far end is the cool cyan/violet **entrance**; spawn is a warm amber
**exit** (`spawn_portal`, front `+z`, counter-spinning) that the pilot comes
through on arrival. The camera is now a chase camera: it sits behind the
ship's nose and looks ahead of it, rotating with the rendered heading. The
heading itself is derived from authoritative `pos` deltas (and from local
move intents for immediate feedback), so the hull faces the way it actually
travels — a reversal reads as a turn, not reverse thrust. `enter_portal`
remains intent-only; `Engine.can_enter_portal()` still judges side, distance
and aperture, and success returns the pilot to the exit at spawn.

**Consequence.** The two ends of the trip are visually distinct, and the view
always shows where the ship is going. Positions remain data, so moving either
end needs no engine edits. The mini-map still flips `+z` to point up and the
projection mirrors `+x`, so map and movement keys agree. The plume, the
brighter opposite engine on turns and the nose jets for vertical moves are
local visual feedback, never authoritative state (D2). A fixed camera would
contradict this decision — do not reintroduce it for the chase view. The pilot
starts at spawn in third person, then the camera eases into a first-person
cockpit view after a short intro delay; `V` toggles between the two. In first
person the hull is skipped so it cannot clip the near plane — this is a
renderer-only choice and does not change what the server owns. The cockpit
has a canopy frame, dashboard, gunsight and an MFD: `M` cycles its sub-screens
between a live map of the cube (contacts projected from authoritative `pos`,
`debris` and `portal` data) and a full set of gauges for HP, shields, energy,
methane and metal. A strip of always-on mini gauges sits on the dashboard.
All of it renders from the same lit cube mesh as the rest of the ship — no new
shader or texture path. Because the camera looks along local +Z, MFD marks
must be drawn at *smaller* Z than the screen face or the screen occludes them.

## D28 — Playtest-driven quality pass

**Context.** The game worked end to end but felt stepwise, offered no way to
shoot another pilot, and gave a first-person pilot no fixed place to read
their own health or find teammates. A smoke test proves endpoints; it cannot
judge whether the game is any good.

**Decision.** Added `scripts/playtest.py`, a reusable harness that drives a
real two-pilot session through resource pacing, caps, co-op repair, firing,
thrust vectors, gravity, death rules and a dive into the wormhole, and prints
timing notes. It found a real bug — a downed pilot could still fly — now fixed
by rejecting `move` intents from non-living players. `fire` gained a real
authoritative prototype mechanic (range, cooldown, shields-before-HP,
`combat` broadcast) in `balance.json`; the full target-square overlay from
D14 is still deferred. thrust now uses 0.5-unit steps every 50 ms with
client-side interpolation, so flight reads as continuous while the server
stays authoritative (D2). The WebGL context requests antialiasing and renders
at device pixel ratio (capped at 2); fog, gamma and rim light come from
`lighting.json`. The 3-D cockpit was stripped to a thin canopy rim and a
gunsight: all information moved to a fixed-zone HTML HUD — own ship bottom
left, tactical map with teammates and bearing lines bottom right, squad
status top centre — populated from other pilots' `pos`/`vitals` frames. `M`
toggles HUD detail and `F2`/the button toggles fullscreen.

**Consequence.** Playtest joins smoke as a quality gate: run both after
gameplay or renderer changes. Fullscreen is a browser capability, so the
button is a convenience rather than a guarantee on locked-down browsers. The
HUD keeps unknown teammate vitals marked as such instead of showing a
misleading zero. Keeping the physical cockpit minimal means most of the
screen stays outside the ship, which was the point of first person.

## D21 — Smoke test drives the real Level 1 surface; data must actually load

**Context.** After the Level 1 rewrite, `scripts/smoke.py` still called the
retired auth API (`/api/register`) and used placeholder assertions that always
passed. It failed before ever reaching the game loop. While fixing it, two
silent failures surfaced: `CONFIG_DIR` pointed at `src/data` instead of
`src/quest_for_earth/data`, and two JSON files contained `//` comments. Both
made `_load_json` return `None`, so the engine quietly used built-in defaults
and changes to the data files had no effect.

**Decision.** The smoke harness now drives the real Level 1 surface: HTTP
`/`, `/health`, `/api/ships`; WS `welcome` (incl. portal payload), `move` and
its authoritative broadcast, `chat`, `repair`, `enter_portal` (all three
outcomes), `fire`, `take`, `die`/respawn, shield regen and NPC payload. It
asserts that `get_balance()` is real data, so a broken config path or malformed
JSON fails CI. `CONFIG_DIR` was corrected and the JSON comments removed.

**Consequence.** `python scripts/smoke.py` passes through real handler code and
guards D1 — world/balance edits now demonstrably take effect. FastAPI/Starlette
disconnects are caught (`WebSocketDisconnected` is distinct from
`WebSocketDisconnect` in Starlette >= 1.10), so the harness output is clean.

## D22 — Harvestable debris field as the building-material source *(superseded by D25)*

**Context.** The user wants Fortnite-style building later, and wants the
materials to come from objects in space rather than from thin air. The
Level 1 corridor between spawn and the wormhole was empty.

**Decision.** A seeded (deterministic) field of ~30 small chunks is generated
from `data/world.json` → `debris_field`, with item definitions in
`items.json`: asteroids → stone, ice chunks → ice, scrap metal → metal.
Placement is confined to the spawn→portal corridor, clearing the launch pad
and the portal approach. `welcome` sends the live debris list and the pilot's
materials; the client renders each type differently in 3D and on the mini-map
and shows a stockpile readout. `F` sends a `harvest` intent; the engine checks
distance, marks the chunk harvested and credits the material, then broadcasts
`debris_gone` so every pilot stops rendering it. Actual building placement is
deferred.

**Consequence.** Materials have a diegetic source shared by all pilots in the
room (D2 — the engine owns the harvested state). Because the field is seeded
and data-driven, moving the spawn/portal or changing the mix needs no engine
or client edits. Smoke covers field placement, distance rejection, successful
harvest, material credit and the removal broadcast.

## D23 — Verify inlined matrix math with a known case

**Context.** The fighter model composes a per-part transform with a placement
matrix via the inlined `mat4.multiply`. Its original column-major loop used
transposed indices and silently discarded matrix B's translation. The result
was that parts collapsed to a single point and one box rendered enormous;
the shader reported no errors, so the bug only showed up visually.

**Decision.** `mat4.multiply` was rewritten as a standard column-major A × B
(`out[c*4+r] = Σ a[k*4+r] * b[c*4+k]`), and the fighter composes
`placement × local`. Any new inlined math helper must be checked with a known
input before it is trusted by geometry code.

**Consequence.** The fighter's boxes and wings now land where authored.
Because the bug was silent, smoke cannot catch it — visual verification in the
browser remains required after renderer changes (see AGENTS.md). A later
pitch-capable placement matrix repeated the lesson: checking only its nose
column was not enough — a sign error in the *up* column made the matrix
non-orthogonal and sheared the fighter into a flat sliver at steep angles.
Verify **all three basis columns** (mutually perpendicular, unit length) for
any new rotation helper, not just the one axis that seems to matter.

## D24 — Per-vertex lighting reuses the mesh buffers; light data is JSON

**Context.** The Level 1 meshes were flat-coloured — every face of a cube or
fighter part was the same shade, so the 3-D geometry read as a silhouette.
The renderer must gain ambient + Lambertian diffuse + specular shading without
a second shader program, a new draw path, or hardcoded visual constants.

**Decision.** Each mesh keeps its existing position buffer; a parallel normal
buffer of identical length supplies one face normal per vertex, computed as
the normalized cross product of the triangle's two edges. The vertex shader
passes the model-transformed normal to a single fragment shader that does the
lighting (one `u_lightDir` uniform; `u_emissive` lets glow effects skip it).
Light direction, view direction, ambient/diffuse/specular intensities and
shininess live in `data/lighting.json`, loaded through `world.lighting()`,
served at `/api/lighting` and injected into the page as `window.QFE_LIGHTING`.
The cube's bottom face and the mirrored fighting wing's triangles were
rewound so the cross-product normals point outward.

**Consequence.** Adding or re-skinning a lit mesh means a new normal buffer
and two attribute binds in its existing draw function — never a new shader or
draw path. A lighting change is a JSON edit (D1), hot-reloaded like world data;
shader scalar literals are formatted with a decimal point or GLSL treats them
as ints and `pow()` fails to compile. Smoke asserts the config loads and is
injected, but a browser round-trip is still required after renderer changes
(D23). That round-trip caught a latent bug: fighter box colours are authored
as RGB triples while `u_color` is a `vec4`, so `uniform4fv` silently refused
them — every colour passed to `uniform4fv` must carry an alpha component.

## D25 — Debris becomes ship stores: methane, metal and rock gravity

**Context.** The user reframed the three debris colours: brown is inert rock
(no mineral, but massive), blue is frozen methane, grey is metal. Ships carry
a limited methane tank and a metal tank alongside HP, shields and energy.
Methane must be able to restore energy, excess energy can repair a nearby
wingman's shields, and metal repairs hull points 1:1. Brown rock should pull a
pilot off course by its mass. D22's generic "building materials" no longer
describes any of that.

**Decision.** `items.json` retypes the chunks: `asteroid` → inert `rock`
(yield 0), `ice_chunk` → `methane`, `scrap_metal` → `metal`. `add_player`
gives every ship `energy`, `methane` and `metal` stores, all capped at 100 from
`balance.json`; tanks start empty, energy full. `harvest_debris` refuses rock
(the chunk stays), fills a tank up to its cap, and leaves full-tank chunks in
place. A per-second step in `tick_once` converts 1 methane → 1 energy, then
1 energy → 1 shield (self first, then a living teammate within `nearby_radius`),
and 1 metal → 1 HP (same priority). `repair` is now the explicit targeted form
of energy → teammate shields. Inert rocks get a deterministic `size` 1–3 from
`world.json`; every tick `_apply_gravity()` drifts each pilot toward rocks
within `gravity_radius` by `gravity_strength × size / distance`, capped, and
the web layer broadcasts the resulting `pos` and `vitals` deltas. The renderer
draws one procedural rock + crystal-nodule mesh with the crystals tinted by
type, replacing the old two-cube chunks; rocks are drawn larger and all share
one size. The field is now an exact per-type `counts` map (4 rock, 4 methane,
3 metal) in `world.json` rather than weighted odds. Death empties both tanks.

**Consequence.** Resources and gravity are fully server-authoritative (D2):
the client only renders `pos`, `vitals` and `debris_gone` frames. Because the
caps, rate, radius and gravity constants are data, rebalancing needs no code
change. The web layer now owns a background tick driver; smoke stops it and
ticks explicitly, so the harness stays deterministic. `debris_gone` must carry
`from` as well as `by` — the client matches `from` against the local pilot
name, and a browser round-trip caught the mismatch when the tank HUD stayed at
zero. Building placement remains deferred; metal now has a repair use in the
meantime.

## D26 — Free-look view; thrusters follow where you look

**Context.** With the chase camera the ship always moved on world axes —
`Space`/`Shift` just changed altitude without pitching — so a pilot hovering
above the spawn could not point at it and dive. The user wants mouse look
(or arrow keys) for the view, and thrusters that push along the look vector.

**Decision.** `viewYaw`/`viewPitch` are client-side look state: pointer lock
(or left-drag) and the arrow keys adjust them, with pitch clamped to ~77°.
The ship's smoothed `shipYaw`/`shipPitch` come from that orientation, and both
the chase camera and `mat4.modelYawPitch` use the same vector. `W`/`S` now
hold-to-thrust along ±`viewForward()` at tick rate, sending a 3-D `vec`
intent; the server normalises it, scales by `thrust_step` from `balance.json`,
clamps to the cube and broadcasts the new `pos` (D2). `A`/`D` and
`Space`/`Shift` stay as absolute-axis moves for fine positioning. Local key
commands still steer the view; authoritative position frames only pulse the
engine glow so they cannot fight the mouse.

**Consequence.** You can climb, look down and thrust toward the spawn exit —
"up/down" is no longer a plain altitude change. Movement remains intent-only;
the client never decides where it ended up. The new rotation helper must stay
orthonormal (D23) or the fighter shears into a 2-D sliver at steep angles.
Smoke covers downward and diagonal `vec` thrust; pointer-lock/arrow look and
the resulting pitch are renderer behaviour, so they still need a browser check.

## D29 — Combat, death and salvage loop close the Level 1 loop

**Context.** The playtest harness kept passing while the game still felt like
rooms connected by dead ends: the client had no `fire` path at all, the engine
had no NPC combat, death dropped no loot, and respawn lived in a stray thread
timer rather than the authoritative tick. Three playtest batches later the
loop actually closes.

**Decision.** `engine.fire_at` now resolves a target from a mob instance or a
pilot id, judges range/cooldown (data-driven) and applies shields-before-HP.
Killing blows drop salvage into the victim's room and award the winner the
loser's carried XP (the death-penalty slice leaves the loser). `engine` owns
death via a data-driven respawn countdown driven from `tick_once`, replacing
the ad-hoc timer in the WS layer (D2/D15). `engine.take_loot` performs a real
pickup from the room loot list; the web layer broadcasts `loot` drops and
handles `take`/`loot`. `engine._npc_combat` engages living mobs within the
data-driven aggro distance on the mob cooldown, and kills schedule mob respawns.

**Consequence.** The fight loop is playable end to end: click in first person
to fire at the nearest in-range teammate or mob (client picks a target from
learned state, server judges — D2), the mob answers, a kill drops salvage and
respawns the victim. The playtest now covers the full loop and both harnesses
must stay green in the same commit as any engine change (smoke stubs the
engine boundary; playtest drives it).

## D30 — Evade action and XP/level progression land

**Context.** The Level 1 loop list promised evade and XP progression; both
were either absent or pointed at balance keys that did not exist.

**Decision.** `engine.evade` bursts a living pilot away from the nearest
mob/living pilot with data-driven `evade_burst`/`evade_cooldown_ms`, broadcast
authoritatively; client input `Q`; playtest covers burst + cooldown. Killer
XP now flows through `_award_xp`, which rolls the data-driven level ladder
(`balance.xp.per_level`) and applies level bonuses to the ship stores.

**Consequence.** The loop closes in playtest: fight → kill → loot → respawn →
evade → repeat, with XP/levels moving the ship forward. The previously missing
`balance.hull` / `balance.xp` blocks now exist, so `world.max_hull` and
`world.xp_next` resolve.

## D31 — Procedural WebAudio sound effects

**Context.** The cockpit had visual feedback for fire, damage, harvest and
evade (D28 HUD pass) but no audio at all; the game felt silent.

**Decision.** A dependency-free WebAudio synth layer (`ensureAudio`,
`sfxTone`, `sfxNoise` + named effects) creates a lazily-instantiated
`AudioContext` with a master gain, and plays shoot/hit/kill/harvest/evade/
death/level-up on the existing event hooks (fire intent, authoritative
`combat` frames, `debris_gone`, evade intent, vitals-derived death).

**Consequence.** No asset files, no new dependencies; browsers suspend the
context until a gesture, so all entry points route through `ensureAudio()`
and tolerate absence. Audio stays client-side only — server authority is
unchanged (D2).

## D32 — Combat feedback: muzzle flash and camera shake

**Context.** Shots, hits and damage had audio and a red vignette but no
visual combat feedback; the pilot could not read that the gun fired.

**Decision.** A transient muzzle flash (emissive box at the fighter nose)
arms on client fire intent and decays (~140ms); damage arms a ~180ms camera
shake (magnitude 1, decaying) alongside the existing vignette.

**Consequence.** Purely client-side feedback; server authority and the
authoritative `combat` frames are untouched (D2). The shake reads through the
existing `viewMatrix` path in `drawScene`.

## D33 — Pilot-to-pilot whisper chat

**Context.** Chat existed but had no private channel; the Level 1 loop lists
"t"alk as a squad verb and the HUD squad panel implies coordination.

**Decision.** `/w <pilot> <message>` (client) routes through the server to a
single target via a `to`-tagged `say` frame; general chat keeps the old shape.

**Consequence.** Server-authoritative delivery only (D2); the client renders
whispers addressed to the local pilot distinctly in the transcript.

## D34 — Local scanner with decaying contacts

**Context.** The client had a fogged minimap but no way to plot nearby
contacts; the design calls for passive scanning whose knowledge decays.

**Decision.** `engine.scan` reveals unharvested debris and living mobs within
a data-driven `scan_radius` (balance.json) and stamps a `scan_ttl_ms` expiry
on the pilot. The client sends `{t:"scan"}` on `C`, receives a `scan_result`
frame with the hits and TTL, and paints scan-discovered debris as bright
markers on the tactical map that fade with the TTL.

**Consequence.** The server owns what is in range (D2) — the client only
renders. The radius had to grow past the spawn corridor (nearest debris is
22.5u from spawn) so the very first scan is not an empty one.

## D35 — NPC attacks land on the pilot, and the roster actually spawns

**Context.** Playtest review of `_npc_combat` found two defects that had been
hiding behind weak assertions: the mob's shot was applied to **the mob
itself** (so players were never damaged by NPCs and mobs killed themselves,
dropping loot as a "kill"), and the engine never created a mob roster at all —
`MOBS` was empty until a test added one by hand, so the live game had no
enemies to fight.

**Decision.** `_npc_combat` now applies damage to the pilot (shields first,
then hull) and routes a killing blow through `cause_death` so the engine owns
the death, respawn timer and salvage drop. `_ensure_mob_population` keeps the
roster at one mob per player (`spawn_defaults.count_per_player`), with stable
ids so a killed mob respawns on its own timer instead of doubling the roster.
The `mob_hit` frame now carries `dead` for the pilot and the dropped loot.

**Consequence.** The combat loop works end to end in the live game: fly to
the portal, fight the NPC, take the salvage, respawn. The old playtest
assertion ("the mob drains the pilot's shields") passed for the wrong reason
— it read a shield value left over from an earlier shot — so it was rewritten
to start from a known-full state and to assert the mob does not damage itself.

## D36 — Salvage is spatial: fly to the wreck and grab it

**Context.** Salvage dropped by a death went into the dead agent's *room*
(`end_cube` for mobs, `spawn_cube` for pilots), but a pilot's room never
changed and the client never sent a `take` at all — so the salvage loop was
unreachable: you could kill an enemy and never collect the drop.

**Decision.** Salvage now carries the position where it fell. `take_loot`
searches the world and accepts only a drop within `nearby_radius` of the
pilot, taking the nearest match (several wrecks can hold the same item).
The client tracks `loot` frames, marks salvage on the tactical map and the
mini-map, and grabs the nearest drop with `G`; the server still judges the
pickup.

**Consequence.** The loop reads as "kill, fly to the wreck, grab". A mob kill
now broadcasts its drop (it used to be stored on the mob record only), and
the smoke harness was updated in the same commit because its old assumption —
grabbing salvage from anywhere — no longer held.

## D37 — Affiliation, a live squad roster, and a render loop that cannot die

**Context.** Three gaps in the squad play: the loop's "affiliate" verb was
never implemented; a wingman who had not *moved* never appeared (positions
were only learned from movement frames), so the squad panel could stay empty;
and one exception inside `drawScene()` would kill the animation frame chain
permanently, freezing the game on a black screen.

**Decision.** `engine.affiliate` records a pilot we fly with; the leader's
gains pay a data-driven share (`affiliate_share_percent`) to the follower,
and `T` sets or clears it in the client. The welcome frame now carries the
squad roster and the server broadcasts `join`/`leave`. The render loop was
split into `drawOnce()` + scheduling: a throw inside the draw path is
reported but no longer breaks the frame chain, and a timer fallback keeps
the view alive in environments that never fire animation frames.

**Consequence.** Squad play finally reads: see your wingman, trust them, fly
together, share the takings. The screen can no longer be frozen by a single
bad frame.

## D38 — Rock collisions: bounce damage, and head-on impacts blow rocks up

**Context.** The debris field was inert scenery: pilots flew straight through
rocks with no consequence. The design wants mass to matter — a dense metal
rock curves your path and hurts when you hit it; a light methane chunk is
almost nothing.

**Decision.** Mass is now data: each item carries a `density`
(`items.json`) and each chunk a `size` (`world.json` → `debris_field.sizes`),
so `mass = density × size³` — rock 27, dense metal ore 59.4, methane ice 0.35.
Gravity follows mass (`strength × mass / distance²`, capped), the server
measures each pilot's speed from the tick displacement, and `_rock_collisions`
resolves hard contact: the damage scales with mass × closing speed, the ship
bounces back along the contact normal, and a head-on impact at full speed
into a chunk heavy enough (`explosion_min_mass`) blows it up and splashes
nearby pilots.

**Consequence.** The debris field is now terrain with consequences rather
than decoration. The movement maths moved into `engine.apply_move` so the
engine owns both the position and the velocity it implies (D2); the co-op
playtest cases moved to clear space because the pilot now collides with the
debris it used to drift through.

## D39 — Salvage pays off: energy cells, rank, and a hold you can see

**Context.** Scavenging had no payoff — an Energy Cell is described as
"for shield repair" but there was no way to use it, and the pilot had no
idea of their rank or progress. The loop ran from kill to grab to nothing.

**Decision.** `engine.use_artifact` spends a carried Energy Cell for its
data-defined value (10 shields, capped) and the client uses one with `R`.
The authoritative vitals frame now carries the pilot's artifacts, XP, level
and the level's XP thresholds, so the own-ship panel can show a RANK row
(level plus an XP bar) and a CELLS count.

**Consequence.** Kill → fly to the wreck → grab → burn the cell to get back
into the fight, with the ladder visible in the same fixed HUD zone as the
rest of the ship's state.

## D40 — Chart sharing, and a harness that cannot hide a fresh frame

**Context.** `fog_share` was a stub that echoed a fake "partial" fog state
back to the sender. With the scanner working there is something real to
share. The same pass uncovered a harness flaw: the playtest left unread
frames on the pilot sockets, and `wait_for` pulls only a limited number per
call — a stale backlog could hide a frame that had just arrived.

**Decision.** `engine.share_chart` returns the sharer's current contacts
(with their masses) and the server delivers them to the named wingman only
(D4). The client draws shared contacts as hollow markers on the tactical
map and shares with `Y`. The playtest gained a `drain()` helper that clears
the socket backlog before a check that expects a fresh frame.

**Consequence.** Squad work now has a way to pass on what a pilot has
charted, and the harness can no longer pass or fail on a stale frame.

## D41 — The pilot is told what to do

**Context.** Level 1 had a loop (fly out, kill the NPC, take its salvage)
and no way to see it. The wormhole even says there is nothing beyond it, so
a new pilot could wander the cube with nothing to aim at.

**Decision.** A one-line objective banner sits directly above the squad
panel — in the fixed zone the eye already goes. It reads the authoritative
game state: destroy the NPC guarding the wormhole, then take the salvage
(fly to the wreck, press G), then salvage secured. The arrival briefing
names the same verbs.

**Consequence.** The loop is legible without a quest log, and the banner
reuses the events the loop already produces (the kill frame, the pickup
toast) rather than tracking its own state machine.

## D42 — A waypoint, so the objective is findable in a cube

**Context.** A 64×64×64 cube gives no landmarks. The wormhole was at the
far end and the NPC with it, and the only way to find either was to read
the tactical map in the corner of the screen.

**Decision.** A waypoint marker tracks whatever the objective currently is
(the nearest NPC, then the salvage) and points at it. On screen it sits on
the target; off screen or behind, it clamps to the edge and rotates to show
which way to turn, with the range in units.

**Consequence.** Navigation no longer depends on reading a map mid-flight.
The projection reuses the existing view/projection matrices, so it agrees
with what the pilot actually sees.

## D43 — Read where the damage came from, and hear the engine

**Context.** Taking a hit produced a red vignette and a shake, but never
said where the shot came from — in a cube with enemies closing from any
side, that is the difference between a fair fight and a guessing game.

**Decision.** A red chevron (the same projection the waypoint uses) points
at the shooter for a moment after the hit. The client also gained a looping
engine noise whose gain and cutoff follow the thrust level, plus scan and
wormhole sounds.

**Consequence.** Combat is now readable in the same way the objective is:
the eye reads an arrow, not a flickering vignette.

## D44 — The sound layer was silent: ensureAudio returned a boolean

**Context.** The WebAudio layer added in D31 never made a sound. The cause
was a contract slip: `ensureAudio()` returned `!!actx` (a boolean) while
every caller used the result as the context — so `ac.currentTime` was
`undefined`, the first `setValueAtTime` threw, and the `catch` swallowed it
into silence. The engine bed had the same problem, so it never even built
its nodes.

**Decision.** `ensureAudio()` now returns the `AudioContext` (or null when
audio is unavailable), and the feature detection also requires
`createBuffer` — a partial AudioContext must fall back to silence, not to a
half-working synth.

**Consequence.** Every effect (shoot, hit, kill, harvest, evade, death,
level-up, impact, explosion, scan, wormhole) and the looping engine bed now
actually play. The lesson worth keeping: a sound effect that cannot fail
loudly is a sound effect that fails silently — the audio path needs its own
check, because nothing else in the game exercises it.

## D45 — Deliberate PvP: mobs first, never your own wingman

**Context.** The prototype picked the *nearest* thing in range to shoot, so a
wingman standing next to you could be the nearest target — friendly fire by
accident, with no indication it had happened (per-shot chat lines had moved
to the HUD).

**Decision.** The client now picks a mob first and only falls back to a
pilot; the pilot you are affiliated with is never picked. Shooting another
pilot logs "Firing on <name> — a fellow pilot." once every few seconds. The
server still judges range, cooldown and damage (D2) and does not police
friendship — PvP stays legal, it just stops being an accident.

**Consequence.** Two throttle stamps were also corrected: they started at
zero, so the first message was swallowed by the throttle window (five
seconds of silence at the start of a session).

## D46 — Level 1 is one space, and the room model now says so

**Context.** D4 says events are room-scoped, but the code contradicted it in
two ways: every frame was broadcast to every socket, and pilots were tagged
`spawn_cube` while the NPCs they were shooting stood in `end_cube` — two
different rooms in one physical cube. The salvage loop had already moved
to being spatial, which made the room tags pure bookkeeping with two
inconsistent values.

**Decision.** Level 1 is one continuous space with a single room id,
`level_1`; pilots, NPCs and salvage all share it. The two room records in
`world.json` describe the two *ends* of the cube, not separate event rooms.
D4 is amended to say so and to note that the rule bites when the world
gains real spaces.

**Consequence.** The room model is now truthful, the salvage bookkeeping has
one value instead of two, and the harness asserts the room the welcome frame
carries instead of a hard-coded one.

## D47 — A starfield, and the renderer rule it exposed

**Context.** The void behind the cube was flat black. A starfield gives the
space depth for one draw call, but the first attempt rendered nothing.

**Decision.** A fixed, seeded sphere of 420 points far outside the cube,
drawn with `gl.POINTS` and an emissive colour. The vertex shader sets
`gl_PointSize` (harmless for every other primitive).

**Consequence.** The first version drew nothing because the lit shader
normalises the normal attribute, and a zero normal is NaN — which poisons
the colour even when the surface is emissive, because the mix still
multiplies the NaN by zero. Every mesh now needs a valid normal attribute,
even a purely emissive one. Worth remembering: the lighting model is not
optional even for things that ignore it.

## D48 — The guard roster follows the pilots who are connected

**Context.** The roster was sized by `len(PLAYERS)` — every pilot who had
ever joined, not the ones in the world. Each reconnect with a new name left
another guard standing at the wormhole forever, and the level slowly filled
with NPCs nobody had sent for.

**Decision.** The web layer tells the engine who is connected
(`set_online_pilots`) on every join and leave; the roster is sized by that
number and retires the surplus when pilots go away. The playtest now
covers the mob respawn timer (a 90-second cycle that no test had ever
exercised), and the smoke harness checks the roster rather than a
hand-added mob, which the roster now correctly retires.

**Consequence.** The number of guards means something again: one per pilot
in the world. The dead helpers left over from the old solar-system model
(`world.snapshot`, `world.mob`, `world.item`, `world.item_name`,
`world.xp_reward`) were removed while the area was open.

## D49 — The wormhole opens when the guardian falls

**Context.** The level had no ending. Killing the NPC was the objective, but
the wormhole then said there was nothing beyond it — and it said so whether
you had cleared the field or not.

**Decision.** The portal tracks its `enemies_remaining` (a field the engine
already carried): it is set to the roster size, decremented with every
guardian destroyed, and the wormhole refuses entry while anything still
guards it. When the last one falls the server broadcasts `portal_open`, the
objective banner changes to "the wormhole is open — press E to leave", and
the waypoint points at the wormhole itself.

**Consequence.** Level 1 now has an arc rather than a checklist: scan the
field, thread the debris, destroy the guardian, grab the salvage, and slip
through the wormhole to the exit. The smoke harness learned that the
wormhole is sealed while a guard stands at it.
