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
import os
import subprocess
import tempfile
import sys

import yaml
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
#   SCHEMA-0006  194 tables fleet-wide with no COMMENT ON TABLE (measured 2026-09-30)
#   SCHEMA-0007  ai-core-postgres 000023 (a function the build before called, dropped)
#                and voice-ai-agent-core-postgres 000019 (a CHECK over existing columns)
EXPECTED = {
    ("SCHEMA-0003", "schema/migrations/000001_init.up.sql", 18),
    # Both tables the violating fixture creates are undescribed. Pinned at the
    # CREATE TABLE line rather than at the end of the migration, because that is where
    # the author is standing when they read the finding.
    ("SCHEMA-0006", "schema/migrations/000001_init.up.sql", 7),
    ("SCHEMA-0006", "schema/migrations/000002_forward.up.sql", 28),
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 7),
    ("SCHEMA-0002", "schema/migrations/000002_forward.up.sql", 11),
    ("SCHEMA-0001", "schema/migrations/000002_forward.up.sql", 15),
    ("SCHEMA-0005", "schema/migrations/000002_forward.up.sql", 18),
    # These two pin fixes from the SUPPRESSION side. Recall on its own cannot: a rule
    # that quietly stops reporting reads exactly like a repository that got cleaner.
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 31),
    ("SCHEMA-0004", "schema/migrations/000002_forward.up.sql", 39),
    # SCHEMA-0007: every contract with no release named. The two in 000002 are the
    # same sites SCHEMA-0001 and SCHEMA-0002 report: a validated constraint over a
    # column the table had, and a dropped column.
    ("SCHEMA-0007", "schema/migrations/000002_forward.up.sql", 11),
    ("SCHEMA-0007", "schema/migrations/000002_forward.up.sql", 15),
    ("SCHEMA-0007", "schema/migrations/000003_contract_unnamed.up.sql", 8),
    ("SCHEMA-0007", "schema/migrations/000003_contract_unnamed.up.sql", 11),
    ("SCHEMA-0007", "schema/migrations/000003_contract_unnamed.up.sql", 16),
    ("SCHEMA-0002", "schema/migrations/000003_contract_unnamed.up.sql", 16),
    # A drop under nothing-refused: that reason never covers a drop.
    ("SCHEMA-0007", "schema/migrations/000003_contract_unnamed.up.sql", 20),
    # A replace that narrows. Pinned from the suppression side: the widening check
    # that exempts conformant 000003 must not exempt this one.
    ("SCHEMA-0007", "schema/migrations/000004_contract_narrowed.up.sql", 9),
    ("SCHEMA-0002", "schema/migrations/000004_contract_narrowed.up.sql", 9),
    # A dropped CHECK is SCHEMA-0001's and not SCHEMA-0007's: it only accepts more.
    ("SCHEMA-0001", "schema/migrations/000004_contract_narrowed.up.sql", 13),
    # What an older build's statements trip on, each shown on PostgreSQL 18: a unique
    # index rebuilt on other columns (ON CONFLICT finds none), a new unique index, a
    # NOT VALID CHECK (it still checks new rows), DROP DEFAULT, FORCE ROW LEVEL
    # SECURITY, REVOKE, and a function back with another signature.
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 8),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 11),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 14),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 17),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 20),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 23),
    ("SCHEMA-0007", "schema/migrations/000005_older_build_trips.up.sql", 26),
}


def run(root: Path, *extra: str, catalog: Path = CATALOG) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--mode", "baseline", "--repo-root", str(root),
         "--control", str(catalog), "--format", "json", *extra],
        capture_output=True, text=True, env=without_ci_env())
    if proc.returncode == 2:
        raise AssertionError(f"checker could not run on {root}: {proc.stdout}{proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


def without_ci_env() -> dict:
    """The environment without the CI variables the checker reads (the job summary,
    the repository's name), so a self-test run in CI judges the fixture, not the job."""
    env = dict(os.environ)
    for key in ("GITHUB_STEP_SUMMARY", "GITHUB_REPOSITORY", "GITHUB_WORKSPACE"):
        env.pop(key, None)
    return env


G = ["-c", "user.email=t@e.com", "-c", "user.name=t", "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false"]


def git(*args: str) -> None:
    subprocess.run(["git", *G, *args], capture_output=True, check=True)


def tagged_repo(tmp: Path, src: Path, module: str, before: list[str], tags: list[str]) -> Path:
    """A git copy of a fixture tree whose go.mod names `module`: the migrations in
    `before` committed and tagged with `tags`, the rest committed after them, so a
    tag is a release of this module older than its head."""
    root = tmp / module
    mig = root / "schema" / "migrations"
    mig.mkdir(parents=True)
    (root / "go.mod").write_text(f"module example.com/{module}\n\ngo 1.26\n", encoding="utf-8")
    files = sorted((src / "schema" / "migrations").glob("*.up.sql"))
    git("init", "-q", "-b", "main", str(root))
    for f in files:
        if f.name in before:
            (mig / f.name).write_bytes(f.read_bytes())
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "the objects")
    # The release comes after the commit that created what the contracts drop: a
    # release is a later commit, never the one that added the object.
    (root / "RELEASE").write_text("released\n", encoding="utf-8")
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "released")
    for t in tags:
        git("-C", str(root), "tag", t)
    for f in files:
        if f.name not in before:
            (mig / f.name).write_bytes(f.read_bytes())
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-qm", "head")
    return root


def main() -> int:
    failures: list[str] = []

    # Precision. On a git copy, because a contract's release is checked against the
    # repository's own tags: the conformant markers name fixture-core v0.9.0 and v0.10.0.
    precision_tmp = tempfile.TemporaryDirectory()
    conformant = tagged_repo(Path(precision_tmp.name), CONFORMANT, "fixture-core",
                             ["000001_init.up.sql", "000002_forward.up.sql", "000003_replace_constraint.up.sql",
                              "000004_safe_shapes.up.sql"], ["v0.9.0", "v0.10.0"])
    _, clean = run(conformant)
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
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in doc["controls"]}
    if by_id["SCHEMA-0003"].get("waivable") is not False:
        failures.append("SCHEMA-0003 must stay unwaivable: hiding a type from the "
                        "code generator has no sanctioned case")
    if by_id["SCHEMA-0007"]["stage"] != "warn" or by_id["SCHEMA-0007"].get("enforce_in"):
        failures.append("SCHEMA-0007 is report-only (stage warn, enforce_in empty) until a "
                        "repository has shown zero false findings; switching one on is its own PR")
    failures += contract_release_checks()
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


def contract_release_checks() -> list[str]:
    """SCHEMA-0007's release is a fact, not text: a tag of this same module, older
    than the head, on a branch that drops a function the release before called."""
    failures: list[str] = []
    base = "-- +goose Up\nCREATE FUNCTION loop_lookup(id uuid) RETURNS uuid LANGUAGE sql AS 'SELECT id';\n"
    drop = "DROP FUNCTION IF EXISTS loop_lookup(uuid);\n"
    cases = {
        # marker text -> the finding's words, or "" for no SCHEMA-0007 finding
        "-- expand-contract: AI-1 unused-since: loop-core-postgres v0.1.0\n": "",
        "-- expand-contract: AI-1\n-- unused-since: loop-core-postgres@v0.1.0\n": "",
        "-- expand-contract: AI-1 unused-since: left-pad@v1.0.0\n": "this repository is loop-core-postgres",
        "-- expand-contract: AI-1 unused-since: anything v0.0.0\n": "this repository is loop-core-postgres",
        "-- expand-contract: AI-1 unused-since: loop-core-postgres v99.0.0\n": "not a tag of this repository",
        "-- expand-contract: AI-1 unused-since: loop-core-postgres v0.2.0\n": "not a release before this head",
        "-- unused-since: loop-core-postgres v0.1.0\n": "names no release",
        "-- expand-contract: AI-1\n-- schema:allow reason=nothing-refused adr=ADR-0062\n": "names no release",
        # A tag older than the object, or on the root commit: no release that ever had it.
        "-- expand-contract: AI-1 unused-since: loop-core-postgres v0.0.9\n": "does not come after the commit that created routine loop_lookup",
        "-- expand-contract: AI-1 unused-since: loop-core-postgres v0.0.1\n": "does not come after the commit that created routine loop_lookup",
    }
    for marker, want in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "loop-core-postgres"
            mig = root / "schema" / "migrations"
            mig.mkdir(parents=True)
            (root / "go.mod").write_text("module example.com/loop-core-postgres\n\ngo 1.26\n", encoding="utf-8")
            (root / "README").write_text("root\n", encoding="utf-8")
            git("init", "-q", "-b", "main", str(root))
            git("-C", str(root), "add", "-A")
            git("-C", str(root), "commit", "-qm", "root")
            git("-C", str(root), "tag", "v0.0.1")
            (mig / "000001_init.up.sql").write_text(base, encoding="utf-8")
            git("-C", str(root), "add", "-A")
            git("-C", str(root), "commit", "-qm", "the function")
            git("-C", str(root), "tag", "v0.0.9")
            (root / "README").write_text("released\n", encoding="utf-8")
            git("-C", str(root), "add", "-A")
            git("-C", str(root), "commit", "-qm", "release")
            git("-C", str(root), "tag", "v0.1.0")
            git("-C", str(root), "checkout", "-qb", "feature")
            (mig / "000002_drop.up.sql").write_text("-- +goose Up\n" + marker + drop, encoding="utf-8")
            git("-C", str(root), "add", "-A")
            git("-C", str(root), "commit", "-qm", "drop")
            # A tag on the head itself is no release before it.
            git("-C", str(root), "tag", "v0.2.0")
            proc = subprocess.run(
                [sys.executable, str(CHECKER), "--repo-root", str(root), "--control", str(CATALOG),
                 "--mode", "diff", "--base-ref", "main", "--format", "json"],
                capture_output=True, text=True, env=without_ci_env())
            if proc.returncode != 0:
                failures.append(f"SCHEMA-0007 is report-only, yet diff mode exited {proc.returncode} for {marker!r}")
                continue
            got = [f["message"] for f in json.loads(proc.stdout)["findings"] if f["control"] == "SCHEMA-0007"]
            if not want and got:
                failures.append(f"a verified release was refused, {marker!r}: {got}")
            if want and not any(want in m for m in got):
                failures.append(f"{marker!r} was accepted or misreported (want {want!r}): {got}")
            if want == "this repository is loop-core-postgres" and marker.startswith("-- expand-contract: AI-1 unused-since: left-pad"):
                # Enforcement is per repository: switched on for this module, the same
                # finding fails the branch.
                doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
                for c in doc["controls"]:
                    if c["id"] == "SCHEMA-0007":
                        c["enforce_in"] = ["loop-core-postgres"]
                switched = Path(tmp) / "switched.yaml"
                switched.write_text(yaml.safe_dump(doc), encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, str(CHECKER), "--repo-root", str(root), "--control", str(switched),
                     "--mode", "diff", "--base-ref", "main"],
                    capture_output=True, text=True, env=without_ci_env())
                if proc.returncode != 1:
                    failures.append(f"SCHEMA-0007 switched on for the module did not fail the branch (exit {proc.returncode})")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
