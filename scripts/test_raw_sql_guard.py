#!/usr/bin/env python3
"""Precision/recall self-check for check_no_raw_sql.py.

The guard's job is to keep hand-written SQL out of application code, and it only does that job for
as long as people trust its output. It previously matched its opening keywords (`UPDATE`, `WITH`,
`SELECT`) case-insensitively with no corroboration, so any error string beginning with one of those
ordinary English words was reported as raw SQL - fmt.Errorf("update backup codes: %w", err) in
auth-core, twice. Findings like that train a team to skim past the whole check.

The fixture annotates each line as `wantSQL` or `wantProse`, and this asserts both directions: no
false positive on prose, and no loss of detection on the five statement forms plus lowercase SQL.
Recall matters as much as precision here, because the obvious way to kill the false positives is to
demand uppercase keywords, which would silently stop detecting real lowercase SQL.

The internal/db fixture covers the same tension for generated code. The guard used to treat only
directories named sqlc/ or rootsqlc/ as generated, and no repository uses those names, so it
reported sqlc's own output back at the repo. Fixing that by exempting internal/db would have gone
too far the other way, so the exemption follows Go's generated-code marker and the fixture holds
both halves: the marked file is silent, its unmarked neighbour is still reported.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE / "testdata" / "raw-sql"
CHECKER = HERE / "check_no_raw_sql.py"


def main() -> int:
    source = (FIXTURE / "precision.go").read_text(encoding="utf-8")
    lines = source.splitlines()

    expected_sql: set[int] = set()
    expected_prose: set[int] = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*// want(SQL|Prose)\b", line)
        if not m:
            continue
        # The annotation sits directly above the line it describes.
        target = i + 2  # 1-based line number of the following line
        (expected_sql if m.group(1) == "SQL" else expected_prose).add(target)

    if not expected_sql or not expected_prose:
        print("::error::fixture lost its annotations; this test would pass vacuously")
        return 1

    proc = subprocess.run(
        [sys.executable, str(CHECKER)], cwd=FIXTURE, capture_output=True, text=True
    )
    reported = {int(m.group(1)) for m in re.finditer(r"precision\.go:(\d+):", proc.stdout)}

    missed = sorted(expected_sql - reported)
    false_positives = sorted(reported & expected_prose)
    unexpected = sorted(reported - expected_sql - expected_prose)

    for ln in missed:
        print(f"::error::line {ln} is real SQL and was NOT reported: {lines[ln - 1].strip()}")
    for ln in false_positives:
        print(f"::error::line {ln} is prose and WAS reported: {lines[ln - 1].strip()}")
    for ln in unexpected:
        print(f"::error::line {ln} reported but not annotated either way: {lines[ln - 1].strip()}")

    generated_failures = check_generated(proc.stdout)

    if missed or false_positives or unexpected or generated_failures:
        return 1
    print(
        f"raw-sql guard: {len(expected_sql)} statement form(s) detected, "
        f"{len(expected_prose)} prose case(s) correctly ignored, "
        "generated code exempt and its hand-written neighbour still caught"
    )
    return 0


def check_generated(stdout: str) -> bool:
    """Assert the exemption is driven by the marker rather than by the directory."""
    failed = False

    if "queries.sql.go" in stdout:
        print(
            "::error::internal/db/queries.sql.go carries the generated-code marker and was "
            "reported; the guard is telling a repo to fix sqlc's own output"
        )
        failed = True

    if "handwritten.go" not in stdout:
        print(
            "::error::internal/db/handwritten.go has no generated-code marker and was NOT "
            "reported; the exemption has widened to the whole directory"
        )
        failed = True

    return failed


if __name__ == "__main__":
    raise SystemExit(main())
