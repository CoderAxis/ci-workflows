#!/usr/bin/env python3
"""Schema-lint gate: the destructive-change guard the Schema Migration Standard requires, plus the lock-safety and codegen invariants a fleet scan found unheld.

The policy is a sentence in ADR-0062 and schema-migration-standard.md section 10
that nothing verified. schema-migration-conformance.yaml lists
`destructive_change_guard: true` under `required:` and gates Level 3 - and therefore
staging promotion - on it, while the standard's own section 10.1 records "Not
implemented ... no workflow or script in coderaxis/ci-workflows or inboxxhq-infra
inspects DDL for destructive statements". This is that inspection. It is split in two
on purpose:

  controls/schema-lint.yaml  the policy: which DDL shapes are refused, which reasons
                             are sanctioned grounds for an exception, and which stage
                             each control is at.
  this file                  the judge: a syntax-only pass over the migrations plus
                             the decision, with no knowledge of any particular repo.

Parsing is kept honest rather than grep-shaped because the subject defeats a text
scan in both directions. product-core-postgres 000004 spends five comment lines
explaining why a constraint is NOT VALID and the constraint it describes is not; a
grep for "NOT VALID" reads that repository as compliant. In the other direction,
eight of the ten repositories that declare an enum wrap it in a `DO $$ ... $$` block,
so a line-oriented scan splits the statement in half. The parser here strips comments
while preserving line numbers, and tracks dollar-quoting so a DO block stays one
statement.

WHY --mode DEFAULTS TO diff. An applied migration is immutable; the sibling gate
check-migration-immutability.py refuses any PR that edits one. The 49 NOT VALID
violations and 270 unconstrained columns already on main therefore cannot be fixed in
place by anybody, and a gate failing on them would fail every repository forever for a
state no developer is permitted to correct. In diff mode the controls read only the
migrations a PR ADDS, where there is no legacy excuse; baseline mode scans everything,
never fails, and exists to produce the inventory behind the waiver registry.

Exit codes follow the house convention:
  0  clean, or the repository has nothing for this gate to scan
  1  at least one finding from a control at stage `enforce`
  2  the checker could not do its job (missing catalog, unparseable catalog, or a
     repository that clearly has the subject matter but produced an empty scan)

Exit 2 exists because a checker that silently cannot run is worse than no checker: it
reports a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTROL = REPO_ROOT / "controls" / "schema-lint.yaml"

REQUIRED_CONTROL_FIELDS = (
    "id", "title", "owner", "scope", "status", "severity", "stage",
    "policy", "rationale", "remediation", "detector", "refs",
)
VALID_SEVERITY = {"critical", "major", "minor"}
VALID_STAGE = {"enforce", "warn", "observe"}
VALID_STATUS = {"active", "deprecated", "superseded"}

# A marker on line M excuses a site on line L when 0 < L - M <= reach, or when M falls
# inside the statement's own line span.
#
# Three lines for a STATEMENT, because a guarded one opens with `-- +goose
# StatementBegin` and `DO $$` and the marker belongs above the guard where a reviewer
# reads it, not buried inside the block.
#
# One line for a COLUMN, because column definitions are one line apart and a reach of
# three let a single marker silently excuse its two neighbours. Caught by mutation-
# testing the self-test: disabling the content_type exclusion did not fail, because a
# provider-vocabulary marker two lines above was covering content_type as well as the
# column it was written for. An excuse that reaches further than the reviewer's eye is
# an excuse nobody audited.
MARKER_REACH_STATEMENT = 3
MARKER_REACH_COLUMN = 1


def cannot_run(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"::error::{message}")
    sys.exit(2)


@dataclass
class Finding:
    control: str
    severity: str
    stage: str
    file: str
    line: int
    message: str
    remediation: str = ""
    refs: list = field(default_factory=list)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.file else "(repository)"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(path: Path) -> dict:
    if not path.exists():
        cannot_run(f"schema-lint control catalog not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        cannot_run(f"cannot read the schema-lint catalog at {path}: {exc}")
    if not isinstance(doc, dict):
        cannot_run(f"{path}: expected a mapping at the top level")

    errors: list[str] = []
    seen: set[str] = set()
    controls = doc.get("controls")
    if not isinstance(controls, list) or not controls:
        cannot_run(f"{path}: invalid catalog (expected a non-empty 'controls:' list)")
    for i, c in enumerate(controls):
        c = c or {}
        cid = c.get("id", f"#{i}")
        missing = [f for f in REQUIRED_CONTROL_FIELDS if not c.get(f)]
        if missing:
            errors.append(f"{cid}: missing required field(s): {', '.join(missing)}")
        if c.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity must be one of {sorted(VALID_SEVERITY)}")
        if c.get("stage") not in VALID_STAGE:
            errors.append(f"{cid}: stage must be one of {sorted(VALID_STAGE)}")
        if c.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status must be one of {sorted(VALID_STATUS)}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id")
        seen.add(cid)

    for key in ("destructive_forms", "reasons", "enumerated_suffixes",
                "enumerated_names", "enumerated_exclusions"):
        if not isinstance(doc.get(key), list) or not doc[key]:
            errors.append(f"missing or empty '{key}:' list")

    # Every destructive form must name a control that exists, or a violation would be
    # reported under an id nobody can look up.
    for form in doc.get("destructive_forms") or []:
        if (form or {}).get("control") not in seen:
            errors.append(
                f"destructive form {form.get('name')!r} names unknown control "
                f"{form.get('control')!r}")
    # Every sanctioned reason must cite an ADR, or the marker excuses nothing checkable.
    for reason in doc.get("reasons") or []:
        if not (reason or {}).get("adr"):
            errors.append(f"reason {reason.get('token')!r} names no adr")

    if errors:
        for e in errors:
            print(f"::error::schema-lint catalog invalid: {e}")
        sys.exit(2)
    return doc


def controls_by_id(doc: dict) -> dict:
    return {c["id"]: c for c in doc["controls"]}


# ---------------------------------------------------------------------------
# SQL parsing
# ---------------------------------------------------------------------------

def strip_comments(sql: str) -> str:
    """Blank out -- and /* */ comments, preserving the line count.

    Line numbers are the whole value of this gate's output, so a comment is replaced
    by whitespace of the same shape rather than removed.
    """
    out: list[str] = []
    in_block = False
    for line in sql.split("\n"):
        if in_block:
            i = line.find("*/")
            if i < 0:
                out.append("")
                continue
            line = " " * (i + 2) + line[i + 2:]
            in_block = False
        while True:
            b = line.find("/*")
            if b < 0:
                break
            e = line.find("*/", b + 2)
            if e < 0:
                line = line[:b]
                in_block = True
                break
            line = line[:b] + " " * (e + 2 - b) + line[e + 2:]
        i = line.find("--")
        if i >= 0:
            line = line[:i]
        out.append(line)
    return "\n".join(out)


def statements(sql: str) -> list[tuple[str, int]]:
    """[(text, first_line)] split on `;`, respecting $tag$ dollar-quoting.

    Dollar-quoting is not a nicety here: a `DO $$ ... $$` block contains semicolons,
    and splitting inside one turns a single guarded ALTER TABLE into fragments that no
    rule can read.
    """
    res: list[tuple[str, int]] = []
    cur = ""
    start = 1
    dollar: str | None = None
    tag = re.compile(r"\$[A-Za-z_]*\$")
    for ln, line in enumerate(sql.split("\n"), 1):
        if not cur.strip() and not dollar:
            # Drop leading blank/comment-stripped lines. Keeping them would leave
            # newlines in the statement that line_of() counts a second time, so every
            # reported line drifted down by the size of the preceding comment block.
            start = ln
            cur = ""
        pos = 0
        while pos < len(line):
            if dollar:
                j = line.find(dollar, pos)
                if j < 0:
                    cur += line[pos:]
                    pos = len(line)
                else:
                    cur += line[pos:j + len(dollar)]
                    pos = j + len(dollar)
                    dollar = None
                continue
            m = tag.search(line, pos)
            semi = line.find(";", pos)
            if m and (semi < 0 or m.start() < semi):
                cur += line[pos:m.end()]
                dollar = m.group(0)
                pos = m.end()
                continue
            if semi >= 0:
                cur += line[pos:semi]
                if cur.strip():
                    res.append((cur, start))
                cur = ""
                pos = semi + 1
                start = ln
            else:
                cur += line[pos:]
                pos = len(line)
        cur += "\n"
    if cur.strip():
        res.append((cur, start))
    return res


def line_of(stmt: str, start_line: int, offset: int) -> int:
    return start_line + stmt[:offset].count("\n")


def type_head(t: str) -> str:
    """First token of a type expression: 'chat_attachment_type NOT NULL' -> the type."""
    return re.split(r"[\s(]", t.strip(), 1)[0].lower()


def split_top(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def balanced(stmt: str, open_idx: int) -> str:
    """The text inside the parentheses that open at `open_idx`.

    A fixed-width window was wrong and quietly so: taking 800 characters after each
    `CHECK (` swept forward past the end of the expression and into the column
    definitions that followed it in the same CREATE TABLE, so any column declared near
    a constraint read as constrained. That suppresses real findings rather than
    inventing false ones, which is the failure mode a gate cannot afford - it reports a
    pass. Caught by the conformant fixture only because the fixture happened to place
    content_type after a CHECK.
    """
    depth = 0
    for j in range(open_idx, len(stmt)):
        if stmt[j] == "(":
            depth += 1
        elif stmt[j] == ")":
            depth -= 1
            if depth == 0:
                return stmt[open_idx + 1:j]
    return stmt[open_idx + 1:]


def table_body_off(stmt: str) -> tuple[str, int]:
    """(body, offset) of the outermost CREATE TABLE parentheses."""
    i = stmt.find("(")
    if i < 0:
        return "", 0
    depth = 0
    for j in range(i, len(stmt)):
        if stmt[j] == "(":
            depth += 1
        elif stmt[j] == ")":
            depth -= 1
            if depth == 0:
                return stmt[i + 1:j], i + 1
    return stmt[i + 1:], i + 1


RE_ADD_CON = re.compile(r"\bADD\s+CONSTRAINT\s+(\w+)\s+(CHECK|FOREIGN\s+KEY)\b", re.I)
RE_CRE_TBL = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I)
RE_ALT_TBL = re.compile(r"\bALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?([\w.]+)", re.I)
RE_ENUM = re.compile(r"\bCREATE\s+TYPE\s+([\w.]+)\s+AS\s+ENUM\b", re.I)
RE_DOLLAR = re.compile(r"\$[A-Za-z_]*\$")
RE_ADD_COL = re.compile(
    r"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+([A-Za-z][\w ]*(?:\(\d+\))?)", re.I)
CONSTRAINT_KW = re.compile(
    r"^\s*(CONSTRAINT|CHECK|PRIMARY|FOREIGN|UNIQUE|EXCLUDE|LIKE)\b", re.I)

# Destructive forms. DROP INDEX is deliberately absent: it loses no data and no
# invariant a reader depends on. SET NOT NULL is matched only when the same statement
# carries no DEFAULT, which is the standard's own wording.
DESTRUCTIVE = (
    ("drop_column", re.compile(r"\bDROP\s+COLUMN\b", re.I)),
    ("drop_table", re.compile(r"\bDROP\s+TABLE\b", re.I)),
    ("drop_constraint", re.compile(r"\bDROP\s+CONSTRAINT\b", re.I)),
    ("alter_column_type", re.compile(r"\bALTER\s+(?:COLUMN\s+)?\w+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I)),
    ("rename", re.compile(r"\bRENAME\s+(?:COLUMN\s+|TO\b|CONSTRAINT\s+)", re.I)),
    # SET NOT NULL on an EXISTING column, with no DEFAULT exemption. A default does
    # not backfill existing NULLs, so `ALTER COLUMN x SET NOT NULL, ALTER COLUMN x SET
    # DEFAULT y` still scans the whole table and still fails on the first NULL row.
    # Exempting it because DEFAULT appeared somewhere in the statement made this gate
    # pass ledger-core-postgres 000003, which the promotion gate refuses.
    ("set_not_null", re.compile(r"\bSET\s+NOT\s+NULL\b", re.I)),
    # ADD COLUMN x TYPE NOT NULL with no DEFAULT. A distinct shape from SET NOT NULL
    # and the one this gate originally missed: the promotion gate
    # (inboxxhq-infra/scripts/check-destructive-ddl.py) flags it, and omitting it made
    # the two gates disagree on communication, ledger and org-core-postgres - a PR
    # would pass here and the promotion be refused later, which is worse than either
    # gate alone. The lookaheads mirror that script's, including GENERATED.
    ("add_column_not_null_without_default", re.compile(
        # (?!CONSTRAINT\b) because `ADD CONSTRAINT x CHECK (col IS NOT NULL)` carries
        # the words NOT NULL inside the expression and is not a column addition at all.
        r"\bADD\s+(?!CONSTRAINT\b)(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+[^;,]*?\bNOT\s+NULL\b"
        r"(?![^;]*\bDEFAULT\b)(?![^;]*\bGENERATED\b)", re.I)),
)

TEXTY = re.compile(r"^(TEXT|VARCHAR|CHARACTER\s+VARYING|CHAR)$", re.I)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def excluded_paths(root: Path) -> list[Path]:
    """Directories under `root` that must not be scanned.

    This repository, when CI has checked it out INSIDE the tree under test. The
    reusable workflow does exactly that - actions/checkout cannot place a repository
    outside the workspace - so a walk of the caller's whole repository also walks the
    checker's own tree, including fixtures/schema-lint-violating, which exists to
    contain what this gate rejects.

    A computed path rather than a directory name in the catalog's skip_dirs. The name
    is the workflow's choice and differs per job (.schema-tools here, .uuid-tools and
    .central in its siblings), so a hardcoded name is stale the moment a job is
    renamed - and matching a bare name would also skip a same-named directory that
    genuinely belongs to the repository under test.

    Empty when the scanned root is inside this repository: that is how the fixtures and
    the self-test run, and there the fixtures ARE the subject.
    """
    if _within(root, REPO_ROOT):
        return []
    return [REPO_ROOT] if _within(REPO_ROOT, root) else []


def discover(root: Path, doc: dict, exclude: list[Path]) -> list[Path]:
    scan = doc.get("scan") or {}
    skip_dirs = set(scan.get("skip_dirs") or [])
    out: list[Path] = []
    for pattern in scan.get("include_globs") or []:
        for p in root.rglob(pattern):
            if not p.is_file():
                continue
            if skip_dirs & set(p.parts):
                continue
            if any(_within(p, e) for e in exclude):
                continue
            out.append(p)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Diff mode
# ---------------------------------------------------------------------------

def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def git_ok(root: Path, *args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    ).returncode == 0


def added_migrations(root: Path, base_ref: str, candidates: list[Path]) -> list[Path]:
    """The subset of `candidates` this branch ADDS relative to the merge base.

    Added, not modified: a modified migration is already refused by
    check-migration-immutability.py, and reporting it here too would give one defect
    two gates and two error messages.
    """
    if not git_ok(root, "rev-parse", "--verify", base_ref):
        cannot_run(
            f"cannot resolve {base_ref} - the checkout needs fetch-depth: 0 and the "
            f"base branch fetched")
    merge_base = git(root, "merge-base", base_ref, "HEAD")
    listing = git(root, "diff", "--name-only", "--diff-filter=A", merge_base, "HEAD")
    added = {(root / n).resolve() for n in listing.split("\n") if n.strip()}
    return [p for p in candidates if p.resolve() in added]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(root: Path, targets: list[Path], doc: dict, all_files: list[Path]) -> dict:
    """Parse the migrations into facts. Decides nothing.

    `all_files` is every migration in the repository and `targets` is the subset under
    judgement. The two differ in diff mode, and the difference matters: an enum type
    declared in the baseline must be known when a column in a NEW migration uses it,
    or that column reads as an unconstrained TEXT column and SCHEMA-0004 fires on a
    properly typed column.
    """
    marker_re = re.compile((doc.get("scan") or {}).get("marker_pattern", ""), re.I)
    # Per FILE, not per line, and deliberately so: that is the semantics
    # inboxxhq-infra/scripts/check-destructive-ddl.py has enforced at the promotion
    # gate since 2026-08-29. A file this gate failed but the promotion gate passed
    # would be a rule with two answers.
    expand_re = re.compile((doc.get("scan") or {}).get("expand_contract_pattern", ""), re.I)
    expand_contract_files: set[str] = set()
    enum_types: set[str] = set()
    check_bodies: list[str] = []

    # Pass 1 over the WHOLE repository: enum types and every CHECK body, so a
    # constraint declared in an earlier migration still counts for a later column.
    for f in all_files:
        try:
            sql = strip_comments(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for stmt, _ in statements(sql):
            for m in RE_ENUM.finditer(stmt):
                enum_types.add(m.group(1).split(".")[-1].lower())
            for m in re.finditer(r"\bCHECK\s*\(", stmt, re.I):
                check_bodies.append(balanced(stmt, m.end() - 1))

    sites: list[dict] = []
    markers: list[dict] = []
    files_scanned = 0
    parse_errors: list[str] = []

    for f in targets:
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parse_errors.append(f"{f}: {exc}")
            continue
        files_scanned += 1
        rel = str(f.relative_to(root)) if _within(f, root) else str(f)

        if marker_re.pattern:
            for ln, line in enumerate(raw.split("\n"), 1):
                m = marker_re.search(line)
                if m:
                    markers.append({"file": rel, "line": ln,
                                    "reason": m.group("reason"), "adr": m.group("adr")})
        if expand_re.pattern and expand_re.search(raw):
            expand_contract_files.add(rel)

        sql = strip_comments(raw)
        stmts = statements(sql)
        created_here = set()
        readded: set[str] = set()
        for stmt, _ in stmts:
            m = RE_CRE_TBL.search(stmt)
            if m:
                created_here.add(m.group(1).split(".")[-1].lower())
            # A CHECK constraint cannot be extended in place, so the house idiom is
            # DROP CONSTRAINT IF EXISTS x followed by ADD CONSTRAINT x in the same
            # migration - which keeps the table constrained for the whole transaction
            # and loses nothing. Without this, that idiom reads as eight destructive
            # statements in chat-core-postgres 000004 alone, and a gate whose loudest
            # output is its own false positives gets switched off.
            for am in RE_ADD_CON.finditer(stmt):
                readded.add(am.group(1).lower())

        for stmt, sline in stmts:
            span = (sline, sline + stmt.count("\n"))

            for name, pat in DESTRUCTIVE:
                for m in pat.finditer(stmt):
                    t = RE_ALT_TBL.search(stmt)
                    tbl = t.group(1).split(".")[-1].lower() if t else ""
                    if tbl and tbl in created_here:
                        continue
                    if name == "drop_constraint":
                        dm = re.search(r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)",
                                       stmt[m.start():], re.I)
                        if dm and dm.group(1).lower() in readded:
                            continue
                    sites.append({"rule": "destructive", "form": name, "file": rel,
                                  "line": line_of(stmt, sline, m.start()), "span": span,
                                  "detail": f"{name.replace('_', ' ')} on "
                                            f"{tbl or 'an existing object'}"})

            for m in RE_ADD_CON.finditer(stmt):
                t = RE_ALT_TBL.search(stmt)
                tbl = t.group(1).split(".")[-1].lower() if t else "?"
                if tbl in created_here:
                    continue
                if re.search(r"\bNOT\s+VALID\s*$", stmt.strip(), re.I):
                    continue
                sites.append({"rule": "not_valid", "file": rel,
                              "line": line_of(stmt, sline, m.start()), "span": span,
                              "detail": f"{m.group(1)} on {tbl}"})

            if RE_DOLLAR.search(stmt):
                for m in RE_ENUM.finditer(stmt):
                    sites.append({"rule": "enum_in_do", "file": rel,
                                  "line": line_of(stmt, sline, m.start()), "span": span,
                                  "detail": m.group(1)})

            for col in _columns(stmt, sline, enum_types):
                sites.append({"rule": "column", "file": rel, "line": col["line"],
                              "span": span, "detail": f"{col['table']}.{col['column']}",
                              "column": col})

    return {"files_scanned": files_scanned, "sites": sites, "markers": markers,
            "expand_contract_files": expand_contract_files,
            "enum_types": enum_types, "check_bodies": check_bodies,
            "objects_scanned": len(sites), "parse_errors": parse_errors}


def _columns(stmt: str, sline: int, enum_types: set[str]) -> list[dict]:
    """Column definitions introduced by this statement, with their declared type."""
    out: list[dict] = []
    cm = RE_CRE_TBL.search(stmt)
    if cm:
        tbl = cm.group(1).split(".")[-1].lower()
        body, body_off = table_body_off(stmt)
        off = body_off
        for part in split_top(body):
            part_off = off
            off += len(part) + 1
            if CONSTRAINT_KW.match(part):
                continue
            pm = re.match(r"\s*(\w+)\s+(.+)", part, re.S)
            if not pm:
                continue
            col, rest = pm.group(1), pm.group(2)
            typ = rest.split("\n")[0].strip()
            base = re.match(r"([\w ]+(?:\(\d+\))?)", typ)
            out.append({"table": tbl, "column": col.lower(),
                        "type": type_head(base.group(1) if base else typ),
                        "enum_typed": type_head(base.group(1) if base else typ) in enum_types,
                        "inline_check": re.search(r"\bCHECK\s*\(", rest, re.I) is not None,
                        "line": line_of(stmt, sline, part_off + pm.start(1))})
        return out
    if re.search(r"\bALTER\s+TABLE\b", stmt, re.I):
        t = RE_ALT_TBL.search(stmt)
        tbl = t.group(1).split(".")[-1].lower() if t else "?"
        for m in RE_ADD_COL.finditer(stmt):
            typ = type_head(m.group(2))
            tail = stmt[m.end():m.end() + 300]
            out.append({"table": tbl, "column": m.group(1).lower(), "type": typ,
                        "enum_typed": typ in enum_types,
                        "inline_check": re.match(r"[^,;]*\bCHECK\s*\(", tail, re.I) is not None,
                        "line": line_of(stmt, sline, m.start())})
    return out


def reach_for(site: dict) -> int:
    return MARKER_REACH_COLUMN if site["rule"] == "column" else MARKER_REACH_STATEMENT


def excused(site: dict, markers: list[dict], tokens: set[str]) -> bool:
    """Is this site covered by a marker citing a sanctioned reason?"""
    reach = reach_for(site)
    for mk in markers:
        if mk["file"] != site["file"] or mk["reason"] not in tokens:
            continue
        if 0 < site["line"] - mk["line"] <= reach:
            return True
        # A marker inside the statement's own span covers it, but only for statement
        # sites: a CREATE TABLE spans every column it declares, so span containment
        # would hand one marker the whole table.
        lo, hi = site["span"]
        if site["rule"] != "column" and lo <= mk["line"] <= hi:
            return True
    return False


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _mk(c: dict, site: dict, message: str) -> Finding:
    return Finding(control=c["id"], severity=c["severity"], stage=c["stage"],
                   file=site["file"], line=site["line"], message=message,
                   remediation=c["remediation"], refs=list(c["refs"]))


def rule_destructive_statement(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["SCHEMA-0001"]
    if c["status"] != "active":
        return []
    # The file-level `-- expand-contract: <ticket>` marker, as the promotion gate reads
    # it; `schema:allow reason=empty-table` remains for the cases the parser cannot see.
    exp = facts.get("expand_contract_files") or set()
    return [_mk(c, s, f"destructive DDL with no expand-contract marker: {s['detail']}")
            for s in facts["sites"] if s["rule"] == "destructive"
            and s["file"] not in exp
            and not excused(s, facts["markers"], {"empty-table"})]


def rule_add_constraint_not_valid(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["SCHEMA-0002"]
    if c["status"] != "active":
        return []
    tokens = {"empty-table"}
    return [_mk(c, s, f"ADD CONSTRAINT without NOT VALID takes ACCESS EXCLUSIVE and "
                      f"scans the table: {s['detail']}")
            for s in facts["sites"] if s["rule"] == "not_valid"
            and not excused(s, facts["markers"], tokens)]


def rule_enum_hidden_from_codegen(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["SCHEMA-0003"]
    if c["status"] != "active":
        return []
    # Unwaivable: no marker is consulted.
    return [_mk(c, s, f"CREATE TYPE ... AS ENUM inside a DO block is invisible to sqlc, "
                      f"which generates interface{{}} and fails at runtime: {s['detail']}")
            for s in facts["sites"] if s["rule"] == "enum_in_do"]


def rule_unconstrained_enumerated_column(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["SCHEMA-0004"]
    if c["status"] != "active":
        return []
    suffixes = tuple(x["name"] for x in doc["enumerated_suffixes"])
    names = {x["name"] for x in doc["enumerated_names"]}
    excluded = {x["name"] for x in doc.get("enumerated_exclusions") or []}
    tokens = {"free-form", "provider-vocabulary"}
    bodies = "\n".join(facts["check_bodies"]).lower()
    out: list[Finding] = []
    for s in facts["sites"]:
        if s["rule"] != "column":
            continue
        col = s["column"]
        if not TEXTY.match(col["type"]):
            continue
        if col["enum_typed"] or col["inline_check"]:
            continue
        if col["column"] in excluded:
            continue
        if not (col["column"].endswith(suffixes) or col["column"] in names):
            continue
        # A CHECK anywhere in the repository that names this column counts: the
        # constraint is frequently added as a table-level clause or a later migration.
        if re.search(rf"\b{re.escape(col['column'])}\b", bodies):
            continue
        if excused(s, facts["markers"], tokens):
            continue
        out.append(_mk(c, s, f"enumerated-shaped column with no CHECK and no enum type: "
                             f"{s['detail']} {col['type'].upper()}"))
    return out


def rule_orphan_marker(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["SCHEMA-0005"]
    if c["status"] != "active":
        return []
    tokens = {r["token"] for r in doc["reasons"]}
    out: list[Finding] = []
    for mk in facts["markers"]:
        if mk["reason"] not in tokens:
            out.append(Finding(
                control=c["id"], severity=c["severity"], stage=c["stage"],
                file=mk["file"], line=mk["line"],
                message=f"marker cites reason {mk['reason']!r}, which is not in "
                        f"`reasons` in the catalog",
                remediation=c["remediation"], refs=list(c["refs"])))
            continue
        covers = any(
            s["file"] == mk["file"]
            and (0 < s["line"] - mk["line"] <= reach_for(s)
                 or (s["rule"] != "column" and s["span"][0] <= mk["line"] <= s["span"][1]))
            for s in facts["sites"])
        if not covers:
            out.append(Finding(
                control=c["id"], severity=c["severity"], stage=c["stage"],
                file=mk["file"], line=mk["line"],
                message=f"marker reason={mk['reason']} excuses nothing on or near this line",
                remediation=c["remediation"], refs=list(c["refs"])))
    return out


def evaluate(facts: dict, doc: dict) -> list[Finding]:
    ctl = controls_by_id(doc)
    findings = rule_destructive_statement(facts, doc, ctl)
    findings += rule_add_constraint_not_valid(facts, doc, ctl)
    findings += rule_enum_hidden_from_codegen(facts, doc, ctl)
    findings += rule_unconstrained_enumerated_column(facts, doc, ctl)
    findings += rule_orphan_marker(facts, doc, ctl)
    # One finding per control per site: a reviewer needs the site once, not once per
    # rule that happened to reach it.
    seen: set = set()
    unique: list[Finding] = []
    for f in sorted(findings, key=lambda f: (f.file, f.line, f.control)):
        key = (f.control, f.file, f.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def emit_text(facts: dict, findings: list[Finding], mode: str) -> int:
    enforced = [f for f in findings if f.stage == "enforce"]
    warned = [f for f in findings if f.stage == "warn"]
    observed = [f for f in findings if f.stage == "observe"]

    for f in enforced:
        print(f"::error file={f.file},line={f.line}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")
    for f in warned:
        print(f"::warning file={f.file},line={f.line}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")

    for err in facts.get("parse_errors") or []:
        print(f"::warning::schema-lint could not parse {err}")

    scanned = (f"{facts.get('files_scanned', 0)} migration(s), "
               f"{facts.get('objects_scanned', 0)} statement site(s), mode={mode}")
    # baseline never fails: it is the inventory pass, and every finding it can reach
    # sits in a migration that is immutable and therefore uncorrectable in place.
    if enforced and mode != "baseline":
        print(f"schema-lint: FAILED - {len(enforced)} enforced violation(s), "
              f"{len(warned)} advisory, {len(observed)} observed, {scanned} scanned.")
        return 1
    if enforced:
        print(f"schema-lint: OK - {len(enforced)} enforced finding(s) recorded but "
              f"baseline mode never fails, {len(warned)} advisory, {scanned} scanned.")
        return 0
    print(f"schema-lint: OK - no enforced violations, {len(warned)} advisory, "
          f"{len(observed)} observed, {scanned} scanned.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".", help="repository to check")
    ap.add_argument("--control", default=str(DEFAULT_CONTROL),
                    help="schema-lint control catalog")
    ap.add_argument("--mode", choices=("diff", "baseline"), default="diff",
                    help="diff: only migrations this branch adds (the gate). "
                         "baseline: every migration, never fails (the inventory)")
    ap.add_argument("--base-ref", default="origin/main",
                    help="base ref for --mode diff")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--report", help="write the findings report here")
    args = ap.parse_args()

    root = Path(args.repo_root).resolve()
    if not root.is_dir():
        cannot_run(f"--repo-root {root} is not a directory")
    doc = load_catalog(Path(args.control).resolve())

    excluded = excluded_paths(root)
    all_files = discover(root, doc, excluded)
    if not all_files:
        print("schema-lint: no schema/migrations/*.up.sql in this repository; skipping.")
        return 0

    if args.mode == "diff":
        targets = added_migrations(root, args.base_ref, all_files)
        if not targets:
            print("schema-lint: this branch adds no migrations; skipping.")
            return 0
    else:
        targets = all_files

    if args.format == "text":
        for skipped in excluded:
            print(f"[skip] {skipped.name}/: the checker's own checkout, not the caller's code")

    facts = collect(root, targets, doc, all_files)
    if facts.get("files_scanned", 0) == 0:
        cannot_run(
            f"{root} has migration files but the scan parsed 0 of them; refusing to "
            f"report a pass from an empty scan")

    findings = evaluate(facts, doc)

    if args.report:
        Path(args.report).write_text(json.dumps({
            "root": str(root),
            "mode": args.mode,
            "scan": {k: facts.get(k) for k in ("files_scanned", "objects_scanned")},
            "findings": [f.__dict__ for f in findings],
        }, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps({"findings": [f.__dict__ for f in findings],
                          "mode": args.mode,
                          "scan": {k: facts.get(k) for k in
                                   ("files_scanned", "objects_scanned")}}, indent=2))
        return 1 if (args.mode != "baseline"
                     and any(f.stage == "enforce" for f in findings)) else 0

    return emit_text(facts, findings, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
