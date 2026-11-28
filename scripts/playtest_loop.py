"""Playtest loop: run the gameplay suites and surface any failing checks.

Usage:
    python scripts/playtest_loop.py            # one pass over every suite
    python scripts/playtest_loop.py --repeat 3 # repeat to catch flaky bugs

Each suite starts its own server on a distinct port, so they run one after
another. The loop exits non-zero if any suite fails, making it suitable for
a local pre-push gate or a CI job.
"""

import argparse
import re
import subprocess
import sys
import time

SUITES = (
    ("smoke", "scripts/smoke.py"),
    ("playtest", "scripts/playtest.py"),
    ("duel", "scripts/duel.py"),
)

# A failing check is any "[FAIL]" line or a traceback in a suite's output.
FAIL_RE = re.compile(r"\[FAIL\]|Traceback \(most recent call last\)|FAILED")


def run_suite(name, script, python):
    started = time.time()
    proc = subprocess.run(
        [python, script], capture_output=True, text=True, timeout=300,
    )
    elapsed = time.time() - started
    out = proc.stdout + proc.stderr
    fails = [ln.strip() for ln in out.splitlines() if FAIL_RE.search(ln)]
    ok = proc.returncode == 0 and not fails
    return ok, elapsed, fails, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1,
                    help="number of passes (default: 1)")
    args = ap.parse_args()
    python = sys.executable

    total_fail = 0
    for round_no in range(1, args.repeat + 1):
        print(f"\n=== playtest loop — pass {round_no}/{args.repeat} ===")
        for name, script in SUITES:
            ok, elapsed, fails, out = run_suite(name, script, python)
            tag = "PASS" if ok else "FAIL"
            print(f"  [{tag}] {name} ({elapsed:.1f}s)")
            for line in fails[:20]:
                print(f"        {line}")
            if not ok and not fails:
                # Surface the tail so a crash without a [FAIL] line is visible.
                for line in out.splitlines()[-12:]:
                    print(f"        {line}")
            if not ok:
                total_fail += 1

    print(f"\nplaytest loop: {len(SUITES) * args.repeat - total_fail}/"
          f"{len(SUITES) * args.repeat} suites passed")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
