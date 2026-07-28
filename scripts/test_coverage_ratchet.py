#!/usr/bin/env python3
"""Self-test for the coverage ratchet. Run from the github-actions repo root:

    python3 scripts/test_coverage_ratchet.py

Exercises each transition against a temporary floor file, because the ratchet's
value is entirely in which cases it refuses and a rule that only fails in the
obvious direction is worse than none: it reads as enforcement while allowing a
repo to lose its coverage a fraction at a time.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_coverage_ratchet import evaluate  # noqa: E402

FAILURES: list[str] = []


def check(name: str, *, pct: float, threshold: float, floor: dict | None,
          write: bool, want_rc: int, want_floor: float | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "coverage.floor.json")
        if floor is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(floor, fh)

        rc = evaluate(pct, threshold, path, write)
        if rc != want_rc:
            FAILURES.append(f"{name}: exit {rc}, want {want_rc}")
            return

        if want_floor is None:
            if os.path.exists(path):
                got = json.load(open(path, encoding="utf-8"))
                FAILURES.append(f"{name}: expected no floor file, found {got}")
            return

        if not os.path.exists(path):
            FAILURES.append(f"{name}: expected a floor of {want_floor}, found no file")
            return
        got = json.load(open(path, encoding="utf-8"))["floor"]
        if abs(got - want_floor) > 0.001:
            FAILURES.append(f"{name}: floor is {got}, want {want_floor}")


# ── below the tier, nothing recorded ──────────────────────────────────────────
check("undeclared shortfall fails",
      pct=50.9, threshold=70, floor=None, write=False, want_rc=1, want_floor=None)
check("--write records the shortfall",
      pct=50.9, threshold=70, floor=None, write=True, want_rc=0, want_floor=50.9)

# ── holding the floor ─────────────────────────────────────────────────────────
check("exactly at the floor passes",
      pct=50.9, threshold=70, floor={"floor": 50.9}, write=False, want_rc=0, want_floor=50.9)
check("noise below the floor passes",
      pct=50.6, threshold=70, floor={"floor": 50.9}, write=False, want_rc=0, want_floor=50.9)
check("noise above the floor passes without churn",
      pct=52.0, threshold=70, floor={"floor": 50.9}, write=False, want_rc=0, want_floor=50.9)

# ── the two directions that matter ────────────────────────────────────────────
check("a real regression fails",
      pct=44.0, threshold=70, floor={"floor": 50.9}, write=False, want_rc=1, want_floor=50.9)
check("a real improvement must be locked in",
      pct=61.0, threshold=70, floor={"floor": 50.9}, write=False, want_rc=1, want_floor=50.9)
check("--write locks the improvement in",
      pct=61.0, threshold=70, floor={"floor": 50.9}, write=True, want_rc=0, want_floor=61.0)
check("the floor never moves down on --write",
      pct=44.0, threshold=70, floor={"floor": 50.9}, write=True, want_rc=1, want_floor=50.9)

# ── clearing the tier ─────────────────────────────────────────────────────────
check("clearing the tier with no floor passes",
      pct=77.9, threshold=70, floor=None, write=False, want_rc=0, want_floor=None)
check("a floor left behind after clearing the tier fails",
      pct=77.9, threshold=70, floor={"floor": 50.9}, write=False, want_rc=1, want_floor=50.9)
check("--write removes the floor once the tier is cleared",
      pct=77.9, threshold=70, floor={"floor": 50.9}, write=True, want_rc=0, want_floor=None)

# ── a zero floor has to say so ────────────────────────────────────────────────
check("a bare zero floor is rejected",
      pct=0.0, threshold=70, floor={"floor": 0.0}, write=False, want_rc=1, want_floor=0.0)
check("a zero floor with a reason is accepted",
      pct=0.0, threshold=70, floor={"floor": 0.0, "reason": "no tests yet"},
      write=False, want_rc=0, want_floor=0.0)

# ── the reason survives a raise ───────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    p = os.path.join(tmp, "coverage.floor.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"floor": 50.9, "reason": "three repositories are untested"}, fh)
    evaluate(61.0, 70, p, True)
    after = json.load(open(p, encoding="utf-8"))
    if after.get("reason") != "three repositories are untested":
        FAILURES.append(f"raising the floor lost the reason: {after}")

if FAILURES:
    print("coverage ratchet: FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)

print("coverage ratchet: OK (regression, improvement, undeclared shortfall and tier-clearing all handled)")
