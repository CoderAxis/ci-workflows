#!/usr/bin/env python3
"""Raw-SQL guard (CI). Run from a repo root.

Fails (exit 1) if a hand-written SQL string literal appears in non-generated,
non-test Go code that is NOT explicitly allowlisted. This keeps data access in
sqlc-generated code (the enterprise standard).

Allowlist mechanisms (an occurrence is permitted if any apply):
  * the opening line carries a trailing  // raw-sql-allow: <reason>
    (or a leading  -- raw-sql-allow: <reason>  SQL comment for multi-line
    literals; both land on the match line the guard inspects)
  * the file lives under a path-excluded segment: schema/, seed/, migrations/,
    testdata/ (DDL, fixtures and goose migrations are legitimately raw)
  * the statement is built dynamically (fmt.Sprintf / strings.Builder /
    string concatenation / arg append) -- sqlc cannot express it

Generated code and *_test.go are never scanned.
"""
from __future__ import annotations

import os
import re
import sys

SKIP_DIRS = {".git", "vendor", "node_modules"}
# Every dot-prefixed directory goes too, and it is load-bearing rather than tidiness. service-ci
# checks ci-workflows out INTO the repository under test (`.central` for this very script, and
# `.uuid-tools` / `.gateway-tools` / `.archcheck` for its siblings), so an os.walk of the caller's
# tree also walks the checker's own tree — including scripts/testdata/raw-sql/, which exists to
# contain exactly what this guard rejects. Nothing fires today only because that checkout is sparse
# to `scripts` AND the raw-SQL fixtures happen to sit under a `testdata` segment this guard already
# exempts; both are accidents, not decisions. Excluding dot-directories cannot weaken the control:
# the Go toolchain itself ignores them, so Go source under one is never part of the module being
# built and cannot be the data access this guard governs.


def is_tooling_dir(name: str) -> bool:
    return name.startswith(".")
GENERATED_DIR_SEGMENTS = {"sqlc", "rootsqlc"}
EXCEPTION_PATH_SEGMENTS = ("seed", "migrations", "schema", "testdata")

# The directory names above were the whole definition of "generated", and no repository in the
# fleet uses them: all 27 *-core-postgres repos point sqlc's `out:` at internal/db. So the guard
# scanned sqlc's own output and reported every query it had just generated, telling the repo to
# "move static queries into sql/queries and run sqlc generate" about files that are the result of
# doing exactly that.
#
# Go's own convention is the reliable signal, because the tool writes it rather than the layout
# implying it: gofmt-adjacent tooling recognises this exact line, and sqlc emits it. Matching it
# means a repo can put generated code wherever it likes, and a hand-written package that happens
# to be called db is still scanned.
GENERATED_MARKER = re.compile(r"^// Code generated .* DO NOT EDIT\.$", re.MULTILINE)

SQL_OPEN = re.compile(
    r"""(["`])\s*(?:--[^\n]*\n\s*)?(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH)\b""",
    re.IGNORECASE,
)

# A bare opening keyword is not evidence of SQL. `UPDATE` and `WITH` in particular are ordinary
# English, and case-insensitively they match the first word of any number of error strings -
# fmt.Errorf("update backup codes: %w", err) was reported as raw SQL against auth-core, which is
# the kind of finding that teaches a team to stop reading this check's output.
#
# So each opener must be corroborated by the clause that necessarily follows it in real SQL. The
# two-word openers (INSERT INTO, DELETE FROM) are already specific enough to stand alone. SELECT
# requires FROM, which does mean a bare `SELECT 1` liveness probe is not reported - deliberately,
# since it reads no data and is not what this control exists to catch.
CORROBORATION = {
    "SELECT": re.compile(r"\bFROM\b", re.IGNORECASE),
    "UPDATE": re.compile(r"\bSET\b", re.IGNORECASE),
    "WITH": re.compile(r"\bAS\s*\(", re.IGNORECASE),
}


def is_sql(text: str, match: "re.Match[str]") -> bool:
    """Whether a keyword match is corroborated by the rest of the statement it opens."""
    keyword = re.sub(r"\s+", " ", match.group(2)).upper()
    needed = CORROBORATION.get(keyword)
    if needed is None:
        return True
    # Scan to the end of the literal the keyword opened, so corroboration cannot be borrowed from
    # unrelated code further down the file.
    quote = match.group(1)
    end = text.find(quote, match.end())
    return bool(needed.search(text[match.end(): end if end != -1 else match.end() + 400]))

DYNAMIC_SIGNALS = (
    "fmt.Sprintf",
    "strings.Builder",
    "WriteString",
    "strings.Join",
    "append(args",
    "args = append",
    '" + ',
    '+ "',
    "placeholders",
    "queryBuilder",
)


def is_generated(rel_file: str) -> bool:
    return any(seg in GENERATED_DIR_SEGMENTS for seg in rel_file.split(os.sep))


def is_generated_source(text: str) -> bool:
    """Whether the file declares itself generated.

    The marker is only honoured ahead of the package clause, where the convention puts it, so a
    string literal quoting the line further down does not exempt the file that contains it.
    """
    header_end = text.find("\npackage ")
    return bool(GENERATED_MARKER.search(text if header_end == -1 else text[:header_end]))


def is_exception_path(rel_file: str) -> bool:
    parts = [p.lower() for p in rel_file.split(os.sep)]
    return any(seg in parts for seg in EXCEPTION_PATH_SEGMENTS)


def main() -> int:
    root = os.getcwd()
    violations: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not is_tooling_dir(d)]
        for fn in filenames:
            if not fn.endswith(".go") or fn.endswith("_test.go"):
                continue
            fpath = os.path.join(dirpath, fn)
            rel = os.path.relpath(fpath, root)
            if is_generated(rel) or is_exception_path(rel):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if is_generated_source(text):
                continue
            for m in SQL_OPEN.finditer(text):
                if not is_sql(text, m):
                    continue
                start = m.start()
                line_start = text.rfind("\n", 0, start) + 1
                line_end = text.find("\n", start)
                line = text[line_start: line_end if line_end != -1 else len(text)]
                if "raw-sql-allow" in line:
                    continue
                window = text[max(0, start - 240): start + 240]
                if any(sig in window for sig in DYNAMIC_SIGNALS):
                    continue
                lineno = text.count("\n", 0, start) + 1
                violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    if violations:
        print("Un-allowlisted raw SQL literal(s) found outside generated sqlc code:\n")
        for v in violations:
            print(f"  {v}")
        print(
            "\nMove static queries into sql/queries/*.sql and run 'sqlc generate',\n"
            "or annotate genuinely-dynamic SQL with a trailing '// raw-sql-allow: <reason>'."
        )
        return 1

    print("raw-sql guard: OK (no un-allowlisted SQL literals).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
