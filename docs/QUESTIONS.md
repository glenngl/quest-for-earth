# Open questions (accumulated while iterating)

Written down so a later session can pick them up instead of rediscovering
them. Each one is a real decision, not a task list — they need a call.

## Gameplay

1. **How long should a fight be?** Pilot fire is 8 damage against 100
   shields + 100 hull, so a kill takes ~13 landed shots (≈10 s of
   perfect shooting). Is that the intended pace, or should a fight be
   decided in a handful of exchanges (higher damage, lower pilot
   shields)? Elite-style dogfights are decided by positioning, not by
   emptying a bar.
2. **Do shields regenerate during a fight?** They regenerate 1 point per
   30 s now — slow enough that it barely matters, but it means a fight
   never truly ends. Should fighting suppress the regen?
3. **Instant shots make the lead pip advisory only.** The server judges
   a shot the moment it arrives, so there is no projectile to lead.
   Should shots travel (a visible bolt with flight time)? That would make
   the lead pip a real mechanic instead of a hint.
4. **Standoff vs gun range.** The autopilot stops the pair at 14 u while
   the gun reaches 40 u. Close quarters by design, or should the
   standoff sit inside the gun range so the fight happens at 20–40 u
   where the lead pip matters?
5. **Rematch logistics.** A downed pilot respawns at spawn, so the pair
   has to fly back together (the autopilot does it). Should the loser
   instead respawn at the fight site for an immediate rematch?
6. **No consent, no score.** Any pilot can challenge any pilot, and a
   fight has no round score. Should there be a refuse/accept handshake,
   a rule against fighting your affiliate, or a fight scoreboard?
7. **Does PVP feed the level objective?** Killing the wormhole guard
   opens the level; killing another pilot does not. Should a pilot kill
   count toward anything, or is PVP purely a duel?
8. **Spawn vulnerability.** After a respawn there is no protection, so a
   rematch can end instantly at spawn. Brief spawn protection?

## Technical / process

9. **The dev server bit me.** `uvicorn` without `--reload` served stale
   code and I chased phantom bugs. Should the repo ship a
   `scripts/dev.sh` (reload + Playwright visual check) and say so in
   AGENTS.md?
10. **No renderer gate in CI.** CI runs smoke and playtest only; the
    bloom and HUD work was verified by hand in a browser. Should a
    headless-Chromium render check (frame diff, no console errors) join
    CI?
11. **Offline pilots are never pruned.** The engine keeps every pilot
    record for a reconnect; over a long session that list grows and the
    roster shows players who left hours ago. Should offline pilots be
    dropped after some time (and what happens to their salvage)?
12. **The squad roster only says "here" or "gone".** A pilot who has not
    been seen for a while disappears from the list. Should the roster
    show a "last seen" state instead of vanishing?
