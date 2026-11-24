# Working rules for agent sessions

This file is loaded automatically in every session working on this
repository. Read it before changing code. The design rationale lives in
[docs/DECISIONS.md](docs/DECISIONS.md); the module map is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Visual north star: **Elite Dangerous** (D54). The current low-poly
renderer is an early step, not the finish line — aim visual slices
toward that feel while keeping the no-assets/data-driven/server-
authoritative rules.

## Before pushing — non-negotiable

1. **Run `python scripts/smoke.py` and make it pass.** CI blocks the
   image build on it. It drives every endpoint AND the live game loop
   (create → scan → talk → fight → loot → affiliate → evade → death)
   through real handler code. If a harness assumption breaks because
   an engine interface changed, update the smoke harness in the same
   commit. Also run `python scripts/playtest.py` after gameplay changes —
   it checks pacing, firing, thrust, gravity and co-op mechanics (D28),
   and CI gates the image on it too.
2. **Play one real round-trip before touching the engine or client.**
   Start `uvicorn quest_for_earth.web:app`, open two browser tabs,
   undock from Titan, pick a fight with the Space Tick, click to fire at
   it, evade, take salvage. A single-tab spot-check hides the broadcast
   paths.
3. **Grep for consumers before changing any module's public surface.**
   Renaming an engine event field (`swing.dmg`, `target.w`) breaks the
   client renderer in `web/game.js` and the smoke assertions.

## Architecture rules (violating these has caused bugs)

- **The server is authoritative (D2).** Clients render events; they
  never move ships, roll dice, or judge their own shots — the server
  judges every `fire` (mob instance or pilot id) against range, cooldown
  and shields-before-HP from `balance.json`.
- **World content is data (D1).** Rooms, mobs, items, ships and
  balance live in `src/quest_for_earth/data/*.json`. Adding a room or
  a bug means editing JSON — if you find yourself editing
  `engine.py` to add content, stop. The loader hot-reloads on file
  change. Renderer visuals too: the light model lives in
  `data/lighting.json` (D24) — never hardcode light colours/intensities
  into the shaders. Resource caps, conversion rate, repair radius and
  rock-gravity tuning live in `balance.json`/`world.json` (D25), not in
  `engine.py`.
- **GLSL is not JS (D53).** Shader sources are built by string
  concatenation: a `//` comment swallows everything after it (there is
  no newline), so use `/* ... */` inside shader strings. `gl_FragColor`
  needs a `vec4`. A "fullscreen" triangle must actually cover the
  `[-1,1]` viewport (`-1,-1, 3,-1, -1,3`), or half the frame keeps the
  clear colour and shows a diagonal seam. A float FBO texture needs
  `OES_texture_float_linear` for `LINEAR` sampling — use `NEAREST`
  (full-res, sampled 1:1) or the composite reads black. Verify these
  in a browser; smoke/playtest cannot see the renderer.
- **Ship stores and gravity are server-side (D2, D25).** Harvest, the
  per-second methane/metal conversions and rock gravity run in `engine.py`;
  the client only renders `vitals`, `pos` and `debris_gone` frames. If a
  broadcast carries an actor, use `from` (the client matches on it) — `by`
  alone silently loses the update.
- **Never read settings/env at import time (D3).** Everything goes
  through `settings.get(...)` inside the function that needs it.
- **Events are room-scoped (D4, amended by D46).** A socket never
  receives combat or say events for a room it is not in. Level 1 is a
  single space (`level_1`), so the rule holds trivially there; whispers
  to affiliates are the documented exception.
- **Secrets never appear in logs, events, or payloads.** Password
  hashes and session tokens must never be printed or echoed (D9).
- **The camera follows the ship's heading (D20, amended).** It trails the
  nose and rotates with it — when the ship faces spawn, the view faces
  spawn. Derive heading from authoritative `pos` deltas so a reversal
  reads as a turn. Do not restore the old fixed spawn→portal camera. The
  far wormhole is the entrance; spawn has a warm exit wormhole. The engine,
  not the client, still judges portal side and aperture.
- **Verify inlined math helpers with a known input (D23).** A transposed
  `mat4.multiply` silently collapsed geometry without any shader error, and a
  sign error in one column of `modelYawPitch` sheared the fighter into a flat
  sliver. Check **every** basis column (orthonormal), not just the nose.
  Smoke cannot catch renderer-only bugs — check the result visually.
- **View and thrust (D26).** Mouse/pointer-lock or arrow keys set
  `viewYaw`/`viewPitch`; `W`/`S` thrust along that vector via a `vec` intent.
  Server clamps the result. Authoritative `pos` frames must never re-aim the
  view or they fight the mouse.
- **One source of truth per constant.** The tick interval lives in
  `settings.DEFAULTS`, the combat math in `data/balance.json`. Do not
  duplicate either. Data lives in `src/quest_for_earth/data/` — verify
  `get_world()` / `get_balance()` are non-empty; a wrong `CONFIG_DIR` or a
  stray `//` in JSON silently falls back to defaults and hides config bugs.

## Environment facts

- Production runs on the Docker host (`achilles`), container
  `quest-for-earth`, image
  `ghcr.io/glenngl/quest-for-earth:latest`, volume
  `quest-for-earth-data` mounted at `/data` (SQLite world state).
  Old return-to-mordor volumes are incompatible — start fresh.
- Fronted by NGINX Proxy Manager; the proxy host **must** have
  WebSockets enabled or the game hangs on a black screen.
- Local dev: `python -m venv venv && pip install -e . && uvicorn
  quest_for_earth.web:app --reload`; DB lands in `./.data/`.

## SDLC inference layer

- This repo carries **no project-local `opencode.json`** (D10):
  Plan and Build models are chosen in the OpenCode interface, and
  provider access follows the global opencode config. There is no
  project-level spend guard.
- The template system (`sdlc-template.sh new|switch`, TEMPLATES.md in
  the global opencode config) can re-install a project config — a
  spend guard or a pinned local builder — at any time; record it in
  docs/DECISIONS.md if so.
- The game **runtime** has no LLM dependency (D2, D10) — do not add
  one as a side effect of working on the project.

## Documentation duty

When a change makes a decision in docs/DECISIONS.md outdated, update
that entry in the same commit. When the API surface, WS protocol or
UI changes, update README.md. When a new invariant emerges from a
bug, add it here and to DECISIONS.md.
