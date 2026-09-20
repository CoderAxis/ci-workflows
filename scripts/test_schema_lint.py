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
import tempfile
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
    # These two pin fixes from the SUPPRESSION side. Recall on its own cannot: a rule
    # that quietly stops reporting reads exactly like a repository that got cleaner.
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 31),
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 39),
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
                        "fleet run in diff mode reports zero findings (2026-09-20: 207)")
    sanctioned = {r["token"] for r in doc["reasons"]}
    if "because-i-said-so" in sanctioned:
        failures.append("the violating fixture's bogus reason token leaked into the catalog")

    # Mode contract: baseline never fails, so the inventory pass cannot block a PR.
    rc_baseline, _ = run(VIOLATING)
    if rc_baseline != 0:
        failures.append(f"baseline mode must never fail; got exit {rc_baseline}")

    # A checker that cannot run must say so with exit 2, never exit 1. Exit 1 is
    # "a control found a violation", so a broken runner reporting 1 accuses the
    # repository of a defect that is the checker's own. Both paths below did exactly
    # that before this assertion existed: a shallow clone raised CalledProcessError out
    # of git() and Python exited 1 with a traceback, and a missing PyYAML used
    # `raise SystemExit("...")`, which is also 1.
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--repo-root", str(VIOLATING),
         "--control", str(CATALOG), "--mode", "diff", "--base-ref", "refs/nonexistent"],
        capture_output=True, text=True)
    if proc.returncode != 2:
        failures.append(
            f"an unresolvable base ref must exit 2 (cannot run), got {proc.returncode}: "
            f"{(proc.stdout + proc.stderr).strip()[:200]}")
    if "Traceback" in proc.stdout + proc.stderr:
        failures.append("a git failure surfaced as a Python traceback rather than ::error::")

    proc = subprocess.run(
        [sys.executable, "-S", "-s", str(CHECKER), "--repo-root", str(CONFORMANT),
         "--control", str(CATALOG), "--mode", "baseline"],
        capture_output=True, text=True)
    if proc.returncode not in (0, 2):
        failures.append(
            f"a missing PyYAML must exit 2, got {proc.returncode}")

    # A genuinely SHALLOW clone, which is the case that actually happens in CI when a
    # job forgets fetch-depth: 0. The base ref resolves, so the guard before the git
    # calls passes, and `merge-base` is what fails - which used to raise
    # CalledProcessError out of git() and exit 1 with a traceback, accusing the
    # repository of a violation that was the checker's own inability to run.
    with tempfile.TemporaryDirectory() as tmp:
        src, clone = Path(tmp) / "src", Path(tmp) / "clone"
        mig = src / "schema" / "migrations"
        mig.mkdir(parents=True)
        (mig / "000001_init.up.sql").write_text("-- base\n", encoding="utf-8")
        g = ["-c", "user.email=t@e.com", "-c", "user.name=t"]
        for args in (["init", "-q", str(src)], [*g, "-C", str(src), "add", "-A"],
                     [*g, "-C", str(src), "commit", "-qm", "base"],
                     ["-C", str(src), "branch", "-M", "main"],
                     [*g, "-C", str(src), "checkout", "-qb", "feature"]):
            subprocess.run(["git", *args], capture_output=True, check=True)
        (mig / "000002_f.up.sql").write_text(
            "ALTER TABLE t ADD COLUMN x text;\n", encoding="utf-8")
        for args in ([*g, "-C", str(src), "add", "-A"],
                     [*g, "-C", str(src), "commit", "-qm", "f"],
                     ["clone", "-q", "--depth", "1", "--branch", "feature",
                      f"file://{src}", str(clone)],
                     ["-C", str(clone), "fetch", "-q", "--depth", "1", "origin",
                      "main:refs/remotes/origin/main"]):
            subprocess.run(["git", *args], capture_output=True)
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--repo-root", str(clone),
             "--control", str(CATALOG), "--mode", "diff", "--base-ref", "origin/main"],
            capture_output=True, text=True)
        if proc.returncode != 2:
            failures.append(
                f"a shallow clone must exit 2 (cannot run), got {proc.returncode}")
        if "Traceback" in proc.stdout + proc.stderr:
            failures.append("a shallow clone produced a Python traceback")

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
