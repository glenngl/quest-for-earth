# Execution protocol — chunked dispatch to the builder

How the orchestrator decomposes work into small chunks and drives
`agents.builder` (the local builder subagent). This protocol is
hybrid-only: it exists because the builder is a small model that
starts every chunk with a fresh context. Full-cloud work follows
AGENTS.md and does not use this file.

Adopting this protocol in a project is a design decision — record it
in docs/DECISIONS.md.

## The loop

1. The orchestrator writes and owns the execution plan: the chunk
   list, each with a one-line goal, status, and pointer to its block.
   The builder never reads the plan — each dispatch carries only its
   own chunk block.
2. Dispatch ONE chunk at a time. Every dispatch is a fresh context;
   the builder has no memory of earlier chunks by design.
3. Between chunks, verify against the disk: run the chunk's check
   command, review the diff against the spec, keep
   `python scripts/smoke.py` green, then commit.
4. On failure, dispatch a targeted fixup chunk that names the exact
   defect. Iterate at most three times, then stop and re-plan.

## The reconcile rule

Open every chunk block with this text; every chunk inherits it
implicitly:

> Files from earlier runs may exist and may be wrong. Reconcile
> means: make the disk match this spec exactly. Never merge with or
> build upon existing content; where spec and disk disagree, the disk
> is wrong.

## Test-first checks

- Before dispatching chunk N, write the check FIRST at
  `CHECKS/chunk<N>.py`. The check IS the contract; the builder's job
  is "make it pass".
- The check file is pre-placed and read-only for the builder: no
  re-typing checks by hand, no wrong-check confusion, no
  self-invented verifiers.
- Give function contracts as tables, not prose paragraphs.
- `CHECKS/` lives at the repo root, outside the deployment tree, and
  accumulates into a regression suite.

## What a chunk block contains

- **Goal**: one sentence.
- **Files**: the exact list the builder may create or change.
  Nothing else.
- **Contracts**: tables for each function or endpoint (name, args,
  returns, errors).
- **Check**: the single command that verifies this chunk.
- **The reconcile rule** (see above).

## Division of labor

- The builder creates only the files the chunk lists and runs only
  the chunk's check. It does not run git.
- Binary assets, font vendoring, infrastructure setup, and commits
  are operator work — never builder chunks.
- No chunk touches the planning documents (this file, the plan, the
  notes) or AGENTS.md and docs/.

## Why these rules exist — the failure modes they cure

- **History dominance**: a continuing session lets its own prior
  claims outweigh the task and the disk. Cured by fresh context per
  chunk plus the reconcile rule.
- **Self-invented verifiers**: an unchecked builder writes its own
  check and passes itself. Cured by pre-placed, read-only check
  files.
- **Wrong-pass claims**: the builder ran a different test (e.g. the
  health smoke) and reported success. Cured by "the check IS the
  contract".
- **Overbuilding**: the builder implements beyond the spec. Cured by
  "create only the listed files" plus the reconcile rule.
