#!/usr/bin/env python3
"""Self-test for check-schema-lint.py: precision on the conformant fixture, recall by control id and line on the violating one.

Recall is pinned to (control, file, line) rather than to a count. A count passes when
one rule stops matching and another starts over-matching, which is precisely how a
gate rots into a rubber stamp.

Precision is pinned at ZERO findings on the conformant fixture. A gate with false
positives gets switched off, which is worse than no gate - the shape every rule here
accepts is written out in that fixture so the accepted form is reviewable, not folklore.

Run: python3 scripts/test_schema_lint.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check-schema-lint.py"
CATALOG = REPO_ROOT / "controls" / "schema-lint.yaml"
CONFORMANT = REPO_ROOT / "fixtures" / "schema-lint-conformant"
VIOLATING = REPO_ROOT / "fixtures" / "schema-lint-violating"

# Every finding the violating fixture must produce, by control and exact site. These
# are the shapes that actually shipped in the fleet, reduced to their smallest form:
#   SCHEMA-0001  order-core-postgres 000003:37 (DELETE then SET NOT NULL)
#   SCHEMA-0002  chat-core-postgres 000003:47 (ADD CONSTRAINT, no NOT VALID)
#   SCHEMA-0003  chat-core-postgres 000001:490 (CREATE TYPE inside DO)
#   SCHEMA-0004  chat_threads.channel_type, read by an authorization predicate
#   SCHEMA-0005  an excuse citing a reason nobody sanctioned
EXPECTED = {
    ("SCHEMA-0003", "schema/migrations/000001_init.up.sql", 18),
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 7),
    ("SCHEMA-0002", "schema/migrations/000002_forward.up.sql", 11),
    ("SCHEMA-0001", "schema/migrations/000002_forward.up.sql", 15),
    ("SCHEMA-0005", "schema/migrations/000002_forward.up.sql", 18),
}


def run(root: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--mode", "baseline", "--repo-root", str(root),
         "--control", str(CATALOG), "--format", "json"],
        capture_output=True, text=True)
    if proc.returncode == 2:
        raise AssertionError(f"checker could not run on {root}: {proc.stdout}{proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


def main() -> int:
    failures: list[str] = []

    # Precision.
    _, clean = run(CONFORMANT)
    if clean["findings"]:
        for f in clean["findings"]:
            failures.append(
                f"false positive on the conformant fixture: [{f['control']}] "
                f"{f['file']}:{f['line']} {f['message']}")

    # Recall.
    _, dirty = run(VIOLATING)
    got = {(f["control"], f["file"], f["line"]) for f in dirty["findings"]}
    for missing in sorted(EXPECTED - got):
        failures.append(f"rule stopped matching: {missing[0]} at {missing[1]}:{missing[2]}")
    for extra in sorted(got - EXPECTED):
        failures.append(f"unexpected finding: {extra[0]} at {extra[1]}:{extra[2]}")

    # The catalog's own promises.
    import yaml
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in doc["controls"]}
    if by_id["SCHEMA-0003"].get("waivable") is not False:
        failures.append("SCHEMA-0003 must stay unwaivable: hiding a type from the "
                        "code generator has no sanctioned case")
    if by_id["SCHEMA-0004"]["stage"] != "warn":
        failures.append("SCHEMA-0004 may not be promoted to enforce until a measured "
                        "fleet run in diff mode reports zero findings (2026-09-20: 196)")
    sanctioned = {r["token"] for r in doc["reasons"]}
    if "because-i-said-so" in sanctioned:
        failures.append("the violating fixture's bogus reason token leaked into the catalog")

    # Mode contract: baseline never fails, so the inventory pass cannot block a PR.
    rc_baseline, _ = run(VIOLATING)
    if rc_baseline != 0:
        failures.append(f"baseline mode must never fail; got exit {rc_baseline}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"\ntest_schema_lint: FAILED - {len(failures)} problem(s).")
        return 1
    print(f"test_schema_lint: OK - conformant clean, {len(EXPECTED)} expected findings "
          f"matched by control and line, catalog promises hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
