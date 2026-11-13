# Working rules for agent sessions

This file is loaded automatically in every session working on this
repository. Read it before changing code. The design rationale lives in
[docs/DECISIONS.md](docs/DECISIONS.md); the module map is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Before pushing — non-negotiable

1. **Run `python scripts/smoke.py` and make it pass.** CI blocks the
   image build on it. It drives every endpoint AND the live game loop
   (move, fight, vigor, reconnect) through real handler code. If a
   stub or harness assumption breaks because an engine interface
   changed, update the smoke harness in the same commit.
2. **Play one real round-trip before touching the engine or client.**
   Start `uvicorn return_to_mordor.web:app`, open two browser tabs
   (multiplayer needs two), walk a room, land a hit, cast vigor. A
   single-tab spot-check hides the broadcast paths.
3. **Grep for consumers before changing any module's public surface.**
   Renaming an engine event field (`swing.dmg`, `cast.amount`) breaks
   the client renderer in `web/game.js` and the smoke assertions.

## Architecture rules (violating these has caused bugs)

- **The server is authoritative (D2).** Clients render events; they
  never move actors, roll dice, or decide damage. Never trust a
  client action that the engine has not validated server-side.
- **World content is data (D1).** Rooms, mobs, items, races and
  balance live in `src/return_to_mordor/data/*.json`. Adding a room or
  mob means editing JSON — if you find yourself editing `engine.py` to
  add content, stop. The loader hot-reloads on file change.
- **Never read settings/env at import time (D3).** Everything goes
  through `settings.get(...)` inside the function that needs it.
- **Events are room-scoped (D4).** A socket never receives combat or
  say events for a room it is not in. New event kinds must be
  broadcast to rooms, not globally.
- **Secrets never appear in logs, events, or payloads.** Password
  hashes and session tokens must never be printed or echoed (D9).
- **One source of truth per constant.** The tick interval lives in
  `settings.DEFAULTS`, the combat math in `data/balance.json`. Do not
  duplicate either.

## Environment facts

- Production runs on the Docker host (`achilles`), container
  `return-to-mordor`, image
  `ghcr.io/glenngl/return-to-mordor:latest`, volume
  `return-to-mordor-data` mounted at `/data` (SQLite world state).
- Fronted by NGINX Proxy Manager; the proxy host **must** have
  WebSockets enabled or the game hangs on a black screen.
- Local dev: `python -m venv venv && pip install -e . && uvicorn
  return_to_mordor.web:app --reload`; DB lands in `./.data/`.
- The old MUD itself (mordormud.net:4000, Isengard at
  isengard.nazgul.com:4040) is the reference implementation; its
  actual game database is not public and is NOT copied — see D8.

## SDLC inference config

- This project uses the **global opencode config** — no
  project-local `opencode.json` (D10): the instruction files
  (web-search fallback, project standards), the MCP servers (github,
  tavily), and the Ollama provider on bean:11434 are defined there,
  identical to company-sleuth.
- If a project ever needs a template — the hybrid local builder
  (`agents.builder`) or the full-cloud provider spend guard — apply it
  with `sdlc-template.sh new|switch full-cloud|hybrid-cloud <dir>`
  (TEMPLATES.md in the global config directory documents both), and
  record the decision in docs/DECISIONS.md.
- The game **runtime** has no LLM dependency (D2, D10) — do not add
  one as a side effect of working on the project.

## Documentation duty

When a change makes a decision in docs/DECISIONS.md outdated, update
that entry in the same commit. When the API surface, WS protocol or
UI changes, update README.md. When a new invariant emerges from a
bug, add it here and to DECISIONS.md.
