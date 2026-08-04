#!/usr/bin/env python3
"""Applied migrations are immutable — a migration already on the base branch must not change.

WHY THIS EXISTS

A migration is a record of something that has already happened to a database. Once
it has run, editing its file cannot change that database; it can only make the file
and the database disagree. dbmigrate detects that disagreement by checksum and fails
closed on the next deploy:

    provenance FAIL CLOSED: version 1 checksum mismatch
    (applied=46ffc618... repo=84110acb...) — applied migrations are immutable

Because that check runs in the PreSync hook, the failure is not confined to the
service. It fails the hook, which fails the whole sync, which leaves the Argo CD
Application unhealthy — and since `on-deployed` only fires for a healthy app, the
service also stops promoting out of dev, silently and indefinitely. One edited file
took configuration-service 19 hours behind staging with no alert anywhere.

The edit is also, on its own terms, ineffective. A baseline migration only runs
against a database that has never been migrated, so every environment that had
already applied it keeps the old behaviour regardless of what the file now says. The
change appears to have been made and has in fact been made nowhere. That is what a
platform-wide sweep did on 2026-07-24: it rewrote the DEFAULT on four columns in the
already-applied baseline of fourteen services, and every one of those databases is
still generating the old values.

So this guard is not bureaucracy about file history. Editing a released migration
cannot achieve the thing it looks like it achieves, and it breaks deploys and
promotion while failing to achieve it. The correct change is always additive: a new
migration that ALTERs what the old one created.

WHAT COUNTS AS A VIOLATION

Any modification or deletion of a `.up.sql` migration that already exists on the
base branch. New migrations are always fine — that is the intended way to change a
schema. `.down.sql` files are excluded because dbmigrate does not checksum them
(see platform/dbmigrate/checksum.go: isBareUpSQL).

LAWFUL REWRITES

Two changes rewrite a released migration legitimately:

  * a squash, collapsing a chain into a fresh baseline, when no database has applied
    the chain;
  * a restore, putting a file back to the bytes the databases actually applied,
    which is how an edit like the one above is undone — the repair is itself a
    modification, and it is the only one that ends the checksum mismatch.

Record either in `schema/migrations/.immutability-exceptions` — one filename per
line, `#` comments allowed — in the same commit that performs it. The file is read
from the PR head, so the exception and the rewrite are reviewed together, and it
should be emptied again once they land.

Usage:
  python3 check-migration-immutability.py --base-ref origin/main
  python3 check-migration-immutability.py --base-ref origin/main --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = "schema/migrations"
EXCEPTIONS_FILE = f"{MIGRATIONS_DIR}/.immutability-exceptions"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True
    ).returncode == 0


def exceptions() -> set[str]:
    """Filenames the PR head declares as a lawful squash or restore."""
    path = Path(EXCEPTIONS_FILE)
    if not path.is_file():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(Path(line).name)
    return out


def violations(base: str) -> list[dict]:
    """Migrations present on the base branch whose bytes differ at HEAD."""
    merge_base = git("merge-base", base, "HEAD")
    allowed = exceptions()
    found: list[dict] = []

    # Only files the base already had can be immutable; anything else is a new
    # migration, which is the correct way to change a schema.
    listing = git("ls-tree", "-r", "--name-only", merge_base, "--", MIGRATIONS_DIR)
    for name in sorted(filter(None, listing.splitlines())):
        if not name.endswith(".up.sql"):
            continue
        before = git("rev-parse", f"{merge_base}:{name}")
        if git_ok("cat-file", "-e", f"HEAD:{name}"):
            after = git("rev-parse", f"HEAD:{name}")
            if before == after:
                continue
            kind = "modified"
        else:
            kind = "deleted"
        if Path(name).name in allowed:
            continue
        found.append({"file": name, "change": kind})
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Migration immutability guard")
    ap.add_argument("--base-ref", required=True,
                    help="base branch to compare against, e.g. origin/main")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    args = ap.parse_args()

    if not Path(MIGRATIONS_DIR).is_dir():
        print(f"no {MIGRATIONS_DIR}/ in this repository — nothing to check")
        return 0

    # A shallow clone cannot see the base branch, and reporting "no violations"
    # from a clone that cannot look would be worse than not running at all.
    if not git_ok("rev-parse", "--verify", args.base_ref):
        print(f"::error::cannot resolve {args.base_ref} — the checkout needs "
              "fetch-depth: 0 for this guard to see the base branch")
        return 1

    found = violations(args.base_ref)

    if args.json:
        print(json.dumps({"base_ref": args.base_ref, "violations": found}, indent=2))
        return 1 if found else 0

    if not found:
        allowed = exceptions()
        note = f" ({len(allowed)} rewrite(s) declared lawful)" if allowed else ""
        print(f"no released migration was modified{note}")
        return 0

    print(f"\n{len(found)} released migration(s) changed:\n")
    for v in found:
        print(f"  {v['change']:>8}  {v['file']}")
    print(
        "\nA migration records something that has already happened to a database, so\n"
        "editing it cannot change that database — it can only make the file and the\n"
        "database disagree. dbmigrate then fails the PreSync hook by checksum, which\n"
        "fails the whole sync, leaves the Argo CD Application unhealthy, and (because\n"
        "on-deployed only fires for a healthy app) silently stops the service promoting\n"
        "out of dev.\n\n"
        "Make the change additively instead — a new migration that ALTERs what the old\n"
        "one created:\n\n"
        f"    {MIGRATIONS_DIR}/00000N_<what_it_changes>.up.sql\n"
        f"    {MIGRATIONS_DIR}/00000N_<what_it_changes>.down.sql\n\n"
        "and add it to sqlc.yaml's schema list, or sqlc keeps generating against a\n"
        "schema the database no longer has.\n\n"
        "Two rewrites are lawful: squashing a chain no database has applied, and\n"
        "restoring a file to the bytes the databases did apply (the repair for an edit\n"
        f"like this one). For either, list the filenames in {EXCEPTIONS_FILE}\n"
        "in this same PR.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
