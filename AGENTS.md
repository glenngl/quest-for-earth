# Working rules for agent sessions

This file is loaded automatically in every session working on this
repository. Read it before changing code. The design rationale lives in
[docs/DECISIONS.md](docs/DECISIONS.md); the module map is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Before pushing — non-negotiable

1. **Run `python scripts/smoke.py` and make it pass.** CI blocks the
   image build on it. It drives every endpoint AND the live game loop
   (create → scan → talk → fight → loot → affiliate → evade → death)
   through real handler code. If a harness assumption breaks because
   an engine interface changed, update the smoke harness in the same
   commit.
2. **Play one real round-trip before touching the engine or client.**
   Start `uvicorn quest_for_earth.web:app`, open two browser tabs,
   undock from Titan, pick a fight with the Space Tick, land shots in
   the shooter overlay, evade, take salvage. A single-tab spot-check
   hides the broadcast paths.
3. **Grep for consumers before changing any module's public surface.**
   Renaming an engine event field (`swing.dmg`, `target.w`) breaks the
   client renderer in `web/game.js` and the smoke assertions.

## Architecture rules (violating these has caused bugs)

- **The server is authoritative (D2).** Clients render events; they
  never move ships, roll dice, or judge their own shots — the server
  owns the target squares and judges every `fire` against them.
- **World content is data (D1).** Rooms, mobs, items, ships and
  balance live in `src/quest_for_earth/data/*.json`. Adding a room or
  a bug means editing JSON — if you find yourself editing
  `engine.py` to add content, stop. The loader hot-reloads on file
  change.
- **Never read settings/env at import time (D3).** Everything goes
  through `settings.get(...)` inside the function that needs it.
- **Events are room-scoped (D4).** A socket never receives combat or
  say events for a room it is not in. Whisper to affiliates is the
  documented exception.
- **Secrets never appear in logs, events, or payloads.** Password
  hashes and session tokens must never be printed or echoed (D9).
- **One source of truth per constant.** The tick interval lives in
  `settings.DEFAULTS`, the combat math in `data/balance.json`. Do not
  duplicate either.

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

- Project config is `opencode.json`, installed from the
  **full-cloud** template (D10): the provider policy denies every
  provider except `openrouter` — that deny-list is the spend guard.
  The default **model is deliberately NOT pinned in config**: Plan and
  Build models are chosen through the OpenCode interface, which takes
  precedence for your sessions.
- `docs/EXECUTION-PROTOCOL.md` is present but **dormant** — it is
  hybrid-only, and full-cloud work follows AGENTS.md and does not
  use it. It is kept for a possible switch back.
- Switch templates with `sdlc-template.sh switch ...` (previous
  config kept as `opencode.json.bak`); check the active one with
  `sdlc-template.sh status`. Hybrid-cloud is still under testing in
  company-sleuth — revisit this project's choice once it settles.
- The game **runtime** has no LLM dependency (D2, D10) — do not add
  one as a side effect of working on the project.

## Documentation duty

When a change makes a decision in docs/DECISIONS.md outdated, update
that entry in the same commit. When the API surface, WS protocol or
UI changes, update README.md. When a new invariant emerges from a
bug, add it here and to DECISIONS.md.
