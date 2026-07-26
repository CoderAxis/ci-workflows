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

Generated packages (sqlc/, rootsqlc/) and *_test.go are never scanned.
"""
from __future__ import annotations

import os
import re
import sys

SKIP_DIRS = {".git", "vendor", "node_modules"}
GENERATED_DIR_SEGMENTS = {"sqlc", "rootsqlc"}
EXCEPTION_PATH_SEGMENTS = ("seed", "migrations", "schema", "testdata")

SQL_OPEN = re.compile(
    r"""(["`])\s*(?:--[^\n]*\n\s*)?(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH)\b""",
    re.IGNORECASE,
)

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


def is_exception_path(rel_file: str) -> bool:
    parts = [p.lower() for p in rel_file.split(os.sep)]
    return any(seg in parts for seg in EXCEPTION_PATH_SEGMENTS)


def main() -> int:
    root = os.getcwd()
    violations: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
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
            for m in SQL_OPEN.finditer(text):
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
