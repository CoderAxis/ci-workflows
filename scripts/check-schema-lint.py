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
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    # Exit 2, not 1. `raise SystemExit("...")` exits 1, which is this gate's code for
    # "a control found a violation" - so a runner whose dependency step was skipped
    # reported every repository as non-conformant instead of reporting itself broken.
    print("::error::PyYAML required: python3 -m pip install PyYAML")
    sys.exit(2)

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
# Four, not three. The preamble this constant exists for is FOUR lines, not three:
#     -- schema:allow ...
#     -- +goose StatementBegin
#     DO $$
#     BEGIN
#         ALTER TABLE ...
# so a marker written where the comment says to write it fell one line short of the
# statement it excused, and the excuse was refused as an orphan.
MARKER_REACH_STATEMENT = 4
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
    """Blank comments AND the contents of single-quoted literals, keeping line numbers.

    One scanner rather than two passes, because the two interleave: `--` inside a
    string is data, and an apostrophe inside a comment ("-- it's fine") is not a
    string. Stripping either one first corrupts the other.

    Blanking string CONTENTS matters because without it the rules match DDL keywords
    inside DATA. A seed migration writing
    `INSERT INTO change_log (note) VALUES ('drop column legacy_note in the contract
    release')` was reported as destructive DDL, and so was
    `COMMENT ON COLUMN orders.note IS 'we will drop column legacy soon'` - a comment
    ABOUT a drop, read as a drop.

    Dollar-quoted bodies are deliberately left intact: a `DO $$ ... $$` block holds
    real statements this gate must still see, which is why statements() tracks
    dollar-quoting rather than splitting on it.

    Line numbers are this gate's whole output, so everything removed is replaced by
    whitespace of the same shape rather than deleted.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    in_str = in_block = False
    dollar: str | None = None
    tag = re.compile(r"\$[A-Za-z_]*\$")
    while i < n:
        ch = sql[i]
        if dollar:
            if sql.startswith(dollar, i):
                out.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            out.append(ch)
            i += 1
            continue
        if in_block:
            if sql.startswith("*/", i):
                out.append("  ")
                i += 2
                in_block = False
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if in_str:
            if ch == "'":
                if sql.startswith("''", i):
                    out.append("  ")
                    i += 2
                    continue
                in_str = False
                out.append("'")
            else:
                out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if sql.startswith("/*", i):
            in_block = True
            out.append("  ")
            i += 2
            continue
        m = tag.match(sql, i)
        if m:
            dollar = m.group(0)
            out.append(dollar)
            i += len(dollar)
            continue
        if ch == "'":
            in_str = True
        out.append(ch)
        i += 1
    return "".join(out)


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
        #
        # No DEFAULT lookahead here, deliberately. A trailing (?![^;]*DEFAULT) only sees
        # text AFTER "NOT NULL", so it passed `ADD COLUMN x bool NOT NULL DEFAULT false`
        # and refused `ADD COLUMN x bool DEFAULT false NOT NULL` - the same migration,
        # written the more common way round, and the SAFE form in both orders. The
        # clause is checked for DEFAULT as a whole in collect() instead.
        r"\bADD\s+(?!CONSTRAINT\b)(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+[^;,]*?\bNOT\s+NULL\b",
        re.I)),
)

# The clause a single ADD COLUMN introduces: from the ADD to the next top-level comma
# or the end of the statement. Read whole so DEFAULT is found wherever it sits.
RE_DROP_TABLE_NAME = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)", re.I)
# strip_comments() blanks the CONTENTS of the quoted body but leaves the statement,
# so presence is readable here and wording is not. That is the right split: this gate
# asks whether a table was described, never whether the description is any good.
RE_COMMENT_TBL = re.compile(r"\bCOMMENT\s+ON\s+TABLE\s+([\w.]+)\s+IS\b", re.I)
RE_PARTITION_OF = re.compile(r"\bPARTITION\s+OF\b", re.I)

# Contract forms beyond SCHEMA-0001's: what removes or narrows a thing a build of the
# release before may still use, so a build one migration behind the schema
# (dbmigrate Options.MaxAhead) breaks on it. SCHEMA-0007 judges them, with every
# SCHEMA-0001 site.
#
# DROP of a type, domain, function, procedure or view is exempt when the same
# migration creates the same name again: that is the rebuild idiom (a domain cannot
# change its CHECK in place, a function its return type), and like the constraint
# replace SCHEMA-0001 already exempts, the object exists for the whole transaction.
CONTRACT = (
    ("drop_type", re.compile(r"\bDROP\s+(TYPE|DOMAIN)\s+(?:IF\s+EXISTS\s+)?([\w.]+)", re.I)),
    ("drop_routine", re.compile(
        r"\bDROP\s+(FUNCTION|PROCEDURE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+EXISTS\s+)?([\w.]+)", re.I)),
    ("rename_value", re.compile(r"\bRENAME\s+(VALUE|ATTRIBUTE)\b()", re.I)),
    ("drop_attribute", re.compile(r"\bDROP\s+(ATTRIBUTE)\b()", re.I)),
)
RE_CREATED_NAME = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TYPE|DOMAIN|FUNCTION|PROCEDURE|VIEW|MATERIALIZED\s+VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I)
# Any constraint a table may be given after it holds rows. RE_ADD_CON stays CHECK and
# FOREIGN KEY because SCHEMA-0002's NOT VALID applies to those two alone.
RE_ADD_CON_ANY = re.compile(
    r"\bADD\s+CONSTRAINT\s+(\w+)\s+(CHECK|FOREIGN\s+KEY|UNIQUE|EXCLUDE|PRIMARY\s+KEY)\b", re.I)
RE_ALTER_DOMAIN_ADD = re.compile(r"\bALTER\s+DOMAIN\s+([\w.]+)\s+ADD\b", re.I)
RE_WORD = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
# strip_comments() leaves a dollar-quoted body as it is, so a DO block keeps its
# `--` comments; these detectors read code only, and a comment ABOUT an ALTER DOMAIN
# inside a DO block was read as one.
RE_LINE_COMMENT = re.compile(r"--[^\n]*")


def code_of(stmt: str) -> str:
    """stmt with every remaining `--` comment blanked, newlines kept. Literal contents
    are already blank, so a `--` left in a statement is a comment in a dollar body."""
    return RE_LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), stmt)


def constrained_words(clause: str, kind: str) -> set[str]:
    """The identifiers a constraint holds a table's rows to: the column list of a
    FOREIGN KEY, UNIQUE or PRIMARY KEY (never the REFERENCES side, which names the
    other table's columns), and every word of a CHECK or EXCLUDE."""
    if kind.upper().split()[0] in ("FOREIGN", "UNIQUE", "PRIMARY"):
        i = clause.find("(")
        clause = balanced(clause, i) if i >= 0 else ""
    return {w.lower() for w in RE_WORD.findall(clause)}
# A CHECK's body is compared on the RAW text: the stripped text blanks literals, and
# the values are the whole question.
RE_CHECK_NAMED = r"\bCONSTRAINT\s+{name}\s+CHECK\s*\("
RE_LITERAL = re.compile(r"'((?:[^']|'')*)'")
RE_VALUE_LIST = re.compile(r"(not\s+)?(?:\bin\s*\(|<@\s*array\s*\[)([^)\]]*)[)\]]")


def raw_code(text: str) -> str:
    """text with `--` comments removed outside string literals, literals kept whole."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if text.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def check_body(raw: str, name: str) -> str | None:
    """The normalised body of the last CHECK named `name` in raw text, or None."""
    text = raw_code(raw)
    found = None
    for m in re.finditer(RE_CHECK_NAMED.format(name=re.escape(name)), text, re.I):
        found = balanced(text, m.end() - 1)
    return None if found is None else normalised(found)


def normalised(body: str) -> str:
    """A CHECK body with casts, case outside literals and spacing made uniform, and
    redundant outer parentheses removed, so two spellings of one rule compare equal."""
    parts = re.split(r"('(?:[^']|'')*')", body)
    body = "".join(p if p.startswith("'") else
                   re.sub(r"\s*([^\w\s])\s*", r"\1", re.sub(r"\s+", " ", re.sub(r"::\s*[\w\[\]]+", "", p).lower()))
                   for p in parts).strip()
    while body.startswith("(") and balanced(body, 0) == body[1:-1]:
        body = body[1:-1].strip()
    return body


def widens(old: str, new: str) -> bool:
    """Whether a CHECK replaced by `new` accepts every row `old` did: the same rule,
    `new` holding `old` as one of its OR-ed terms, or the same rule with each list of
    allowed values (IN, <@ ARRAY) the same or larger and none of them a NOT IN."""
    if old == new or old in (normalised(t) for t in split_or(new)):
        return True
    # Every OR-ed term of the old rule is one of the new rule's: it accepts more.
    old_or, new_or = {normalised(t) for t in _split_bool(old, "or")}, {normalised(t) for t in _split_bool(new, "or")}
    if len(old_or) > 1 and old_or <= new_or:
        return True
    # The new rule is the old one with conditions taken away: it accepts more.
    old_and, new_and = {normalised(t) for t in _split_bool(old, "and")}, {normalised(t) for t in _split_bool(new, "and")}
    if len(old_and) > 1 and new_and <= old_and:
        return True
    olds, news = RE_VALUE_LIST.findall(old), RE_VALUE_LIST.findall(new)
    if not olds or len(olds) != len(news) or RE_VALUE_LIST.sub("#", old) != RE_VALUE_LIST.sub("#", new):
        return False
    for (o_not, o_vals), (n_not, n_vals) in zip(olds, news):
        if o_not or n_not or not set(RE_LITERAL.findall(o_vals)) <= set(RE_LITERAL.findall(n_vals)):
            return False
    return True


def split_or(body: str) -> list[str]:
    """body's terms joined by OR at the top level of its parentheses."""
    terms, depth, cur, i = [], 0, "", 0
    while i < len(body):
        ch = body[i]
        if ch == "'":
            j = i + 1
            while j < len(body) and not (body[j] == "'" and not body.startswith("''", j)):
                j += 2 if body.startswith("''", j) else 1
            cur += body[i:j + 1]
            i = j + 1
            continue
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and re.match(r" or ", body[i:i + 4]):
            terms.append(cur)
            cur, i = "", i + 4
            continue
        cur += ch
        i += 1
    return terms + [cur]


# A constraint's kind as an earlier migration declared it, by name, for a DROP
# CONSTRAINT: dropping a CHECK or a FOREIGN KEY only accepts more, while dropping a
# UNIQUE or PRIMARY KEY can break an older build's ON CONFLICT. PostgreSQL's own
# names for unnamed constraints carry the kind as a suffix.
RE_DECLARED_KIND = r"\bCONSTRAINT\s+{name}\s+(CHECK|FOREIGN\s+KEY|REFERENCES|UNIQUE|PRIMARY\s+KEY|EXCLUDE)\b"
RE_PK_INLINE = re.compile(r"^\s*(\w+)\s+[^,]*\bPRIMARY\s+KEY\b", re.I)
RE_PK_TABLE = re.compile(r"\bPRIMARY\s+KEY\s*\(([^)]*)\)", re.I)
# The marker's release: `unused-since: <module> vX.Y.Z` (or <module>@vX.Y.Z), on the
# expand-contract line or a comment line of its own.
RE_RELEASE = re.compile(
    r"--[^\n]*?\bunused-since:\s*(?P<module>[A-Za-z0-9][A-Za-z0-9._/-]*?)(?:@|\s+)(?P<version>v[0-9]+\.[0-9]+\.[0-9]+)\b", re.I)


def dropped_kind(name: str, earlier_raw: str) -> str:
    """CHECK, FOREIGN, UNIQUE, PRIMARY or EXCLUDE for a constraint an earlier
    migration declared under `name`, from its declaration or PostgreSQL's naming
    of an unnamed one; "" when neither says."""
    m = None
    for m in re.finditer(RE_DECLARED_KIND.format(name=re.escape(name)), earlier_raw, re.I):
        pass
    if m:
        kind = m.group(1).upper().split()[0]
        return "FOREIGN" if kind == "REFERENCES" else kind
    for suffix, kind in (("_check", "CHECK"), ("_fkey", "FOREIGN"), ("_pkey", "PRIMARY"),
                         ("_key", "UNIQUE"), ("_excl", "EXCLUDE")):
        if name.endswith(suffix):
            return kind
    return ""


def module_of(root: Path) -> str:
    """The repository's module name: the last element of go.mod's module path, else
    the repository's own name (GITHUB_REPOSITORY in CI), else the directory's."""
    try:
        for line in (root / "go.mod").read_text(encoding="utf-8").splitlines():
            if line.startswith("module "):
                return line.split()[1].rstrip("/").split("/")[-1].lower()
    except OSError:
        pass
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo and Path(os.environ.get("GITHUB_WORKSPACE", "")).resolve() == root:
        return repo.split("/")[-1].lower()
    return root.name.lower()


def _git(root: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def release_problem(root: Path, module: str, version: str, created: dict[str, str]) -> str:
    """Why `module version` is not a release that could have stopped using what the
    contract removes, or "" when it is: the module must be this repository's own, the
    version one of its tags, an ancestor of HEAD other than HEAD itself, and later
    than every commit that created what the contract drops or changes (`created`,
    object -> commit). A tag older than the object, or pushed onto the root commit,
    names a release that never had it to stop using."""
    own = module_of(root)
    if module.lower() != own:
        return f"names {module} {version}, but this repository is {own}"
    rc, tag = _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{version}^{{commit}}")
    if rc != 0:
        return f"names {module} {version}, which is not a tag of this repository"
    _, head = _git(root, "rev-parse", "HEAD")
    if _git(root, "merge-base", "--is-ancestor", tag, "HEAD")[0] != 0 or tag == head:
        return f"names {module} {version}, which is not a release before this head"
    for obj, commit in sorted(created.items()):
        if not commit:
            return f"names {module} {version}, but no migration of this repository creates {obj}, so no release can be shown to come after it"
        if commit == tag or _git(root, "merge-base", "--is-ancestor", commit, tag)[0] != 0:
            return f"names {module} {version}, which does not come after the commit that created {obj}"
    return ""


def objects_of(code: str, tbl: str) -> list[tuple]:
    """What one contract statement drops or changes, as the creation index keys it."""
    out: list[tuple] = []
    for m in re.finditer(r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(\w+)", code, re.I):
        out.append(("column", tbl, m.group(1).lower()))
    for m in re.finditer(r"\bALTER\s+(?:COLUMN\s+)?(\w+)\s+(?:SET|DROP|TYPE)\b", code, re.I):
        if m.group(1).lower() not in ("table", "column", "constraint", "domain", "type"):
            out.append(("column", tbl, m.group(1).lower()))
    for m in re.finditer(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)", code, re.I):
        out.append(("table", obj_name(m.group(1))))
    for m in re.finditer(r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)", code, re.I):
        out.append(("constraint", m.group(1).lower()))
    for m in re.finditer(r"\bDROP\s+(?:FUNCTION|PROCEDURE)\s+(?:IF\s+EXISTS\s+)?([\w.]+)", code, re.I):
        out.append(("routine", obj_name(m.group(1))))
    for m in re.finditer(r"\bDROP\s+(?:TYPE|DOMAIN)\s+(?:IF\s+EXISTS\s+)?([\w.]+)", code, re.I):
        out.append(("type", obj_name(m.group(1))))
    for m in re.finditer(r"\b(?:DROP|CREATE\s+UNIQUE)\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(\w+)", code, re.I):
        out.append(("index", obj_name(m.group(1))))
    if tbl and not out:
        out.append(("table", tbl))
    return out


TEXTY = re.compile(r"^(TEXT|VARCHAR|CHARACTER\s+VARYING|CHAR)$", re.I)


# ---------------------------------------------------------------------------
# SCHEMA-0007: what a build of the release before trips on
# ---------------------------------------------------------------------------
# A build one migration behind the schema (dbmigrate Options.MaxAhead) runs its own
# statements against the newer schema. Each detector below names a change that can
# refuse one of them: a write a new constraint or index rejects, an ON CONFLICT whose
# unique index is gone, an INSERT that relied on a default, a call to a signature that
# no longer exists, a row level security or privilege change. A change is cleared only
# where the parser can show it refuses nothing such a build does; everything else is a
# contract, cleared by a named, published release or by a reviewed marker.

RE_CREATE_UNIQUE_INDEX = re.compile(
    r"\bCREATE\s+UNIQUE\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(?:ONLY\s+)?([\w.]+)", re.I)
RE_CREATE_ANY_INDEX = re.compile(
    r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(?:ONLY\s+)?([\w.]+)", re.I)
RE_DROP_INDEX = re.compile(r"\bDROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([\w.,\s]+?)(?:\s+(?:CASCADE|RESTRICT))?\s*$", re.I)
RE_DROP_DEFAULT = re.compile(r"\bALTER\s+(?:COLUMN\s+)?(\w+)\s+DROP\s+DEFAULT\b", re.I)
RE_RLS = re.compile(r"\b(ENABLE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b", re.I)
RE_REVOKE = re.compile(r"\bREVOKE\b(.*?)\bON\b\s+(.*?)\s+\bFROM\b", re.I | re.S)
RE_ROUTINE_DEF = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE)\s+([\w.]+)\s*\(", re.I)
RE_CREATED_TYPE = re.compile(r"\bCREATE\s+(?:TYPE|DOMAIN)\s+([\w.]+)", re.I)
RE_CREATE_INDEX_NAME = re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
RE_CONSTRAINT_NAME = re.compile(r"\bCONSTRAINT\s+(\w+)", re.I)
RE_ROUTINE_DROP = re.compile(r"\bDROP\s+(FUNCTION|PROCEDURE)\s+(?:IF\s+EXISTS\s+)?([\w.]+)\s*(\()?", re.I)
RE_ADD_COLUMN_CLAUSE = re.compile(r"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+", re.I)
TYPE_WORDS = {"double", "character", "timestamp", "time", "bit", "interval", "national", "varying", "precision"}


def obj_name(name: str) -> str:
    return name.split(".")[-1].strip().strip('"').lower()


def old_build_columns(code: str, tbl: str) -> dict[str, tuple[str, str]]:
    """The columns this statement adds to `tbl`, each as a build that does not know it
    leaves it: ("null", ""), ("lit", <literal>) for a literal default, or ("set", "")
    for any other default or a generated column (some value, not NULL)."""
    out: dict[str, tuple[str, str]] = {}
    for m in RE_ADD_COLUMN_CLAUSE.finditer(code):
        clause = split_top(code[m.end():])[0]
        if re.search(r"\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b|\b(?:BIG)?SERIAL\b|\bDEFAULT\s+(?:nextval|gen_random_uuid|uuidv7|uuid_generate_v[14])\s*\(", clause, re.I):
            # A value the database makes unique for each row it writes.
            out[m.group(1).lower()] = ("ident", "")
            continue
        if re.search(r"\bGENERATED\b", clause, re.I):
            out[m.group(1).lower()] = ("set", "")
            continue
        d = re.search(r"\bDEFAULT\s+('(?:[^']|'')*'|-?\d+(?:\.\d+)?(?![\w.(])|(?:true|false|null)\b)", clause, re.I)
        if d:
            v = d.group(1)
            out[m.group(1).lower()] = ("null", "") if v.lower() == "null" else ("lit", v.strip("'").lower())
        elif re.search(r"\bDEFAULT\b", clause, re.I):
            out[m.group(1).lower()] = ("set", "")
        else:
            out[m.group(1).lower()] = ("null", "")
    return out


# --- a CHECK, judged for a build that does not know this migration's columns -------
#
# Four values. T and N pass a CHECK (a CHECK refuses only FALSE), F refuses, and D
# depends on what the build writes into columns it already knew, which no parser can
# see: D is a contract.

def _and(vals: list[str]) -> str:
    return "F" if "F" in vals else "D" if "D" in vals else "N" if "N" in vals else "T"


def _or(vals: list[str]) -> str:
    return "T" if "T" in vals else "D" if "D" in vals else "N" if "N" in vals else "F"


def _split_bool(expr: str, word: str) -> list[str]:
    """expr's terms joined by `word` (and / or) at the top level, outside literals, and
    never the AND of a BETWEEN."""
    terms, cur, depth, i, between = [], "", 0, 0, 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "'":
            j = i + 1
            while j < n and not (expr[j] == "'" and not expr.startswith("''", j)):
                j += 2 if expr.startswith("''", j) else 1
            cur += expr[i:j + 1]
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        boundary = i == 0 or not (expr[i - 1].isalnum() or expr[i - 1] == "_")
        if depth == 0 and boundary and re.match(r"between\b", expr[i:]):
            between += 1
        if depth == 0 and boundary and re.match(rf"{word}(?![\w])", expr[i:]):
            if word == "and" and between:
                between -= 1
            else:
                terms.append(cur)
                cur = ""
                i += len(word)
                continue
        cur += ch
        i += 1
    return terms + [cur]


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and balanced(expr, 0) == expr[1:-1]:
        expr = expr[1:-1].strip()
    return expr


def fold(expr: str, value) -> str:
    """expr as a build that does not know this migration's columns meets it: each term
    whose value is known for such a build (T or F) folded away, so a rule that only
    adds conditions on new columns reads as the rule it replaced."""
    expr = _strip_parens(expr).strip()
    for word, unit, zero in (("or", "false", "true"), ("and", "true", "false")):
        terms = _split_bool(expr, word)
        if len(terms) > 1:
            kept = []
            for t in terms:
                v = fold(t, value)
                if v == zero:
                    return zero
                if v != unit:
                    kept.append(v if len(_split_bool(v, "or" if word == "and" else "and")) == 1 else f"({v})")
            return f" {word} ".join(kept) if kept else unit
    v = judge(expr, value)
    return {"T": "true", "F": "false"}.get(v, expr)


def judge(expr: str, value) -> str:
    """T, F, N or D for a normalised boolean expression, `value(col)` giving each
    column's old-build state: ("null"|"lit"|"set"|"unknown", literal)."""
    expr = _strip_parens(expr).strip()
    terms = _split_bool(expr, "or")
    if len(terms) > 1:
        return _or([judge(t, value) for t in terms])
    terms = _split_bool(expr, "and")
    if len(terms) > 1:
        return _and([judge(t, value) for t in terms])
    if re.match(r"not(?![\w])", expr):
        v = judge(expr[3:], value)
        return {"T": "F", "F": "T"}.get(v, v)
    return _atom(expr, value)


def _atom(expr: str, value) -> str:
    if expr == "true":
        return "T"
    if expr == "false":
        return "F"
    if re.fullmatch(r"[a-z_]\w*", expr) and expr not in SQL_WORDS:
        # A boolean column: its default, for a build that does not know it.
        state, lit = value(expr)[:2]
        if state == "null":
            return "N"
        if state == "lit" and lit in ("true", "false"):
            return "T" if lit == "true" else "F"
        return "D"
    m = re.fullmatch(r"(\w+) is (not )?null", expr)
    if m:
        state = value(m.group(1))[0]
        if state == "unknown":
            return "D"
        isnull = state == "null"
        return "T" if isnull != bool(m.group(2)) else "F"
    # (a) = (b): two booleans compared.
    m = re.fullmatch(r"(\(.*\))\s*(=|<>|!=)\s*(\(.*\))", expr)
    if m and balanced(m.group(1), 0) == m.group(1)[1:-1] and balanced(m.group(3), 0) == m.group(3)[1:-1]:
        a, b = judge(m.group(1), value), judge(m.group(3), value)
        if "D" in (a, b):
            return "D"
        if "N" in (a, b):
            return "N"
        return "T" if (a == b) == (m.group(2) == "=") else "F"
    if re.search(r"\b(coalesce|nullif|case|distinct)\b", expr):
        return "D"
    m = re.fullmatch(r"(\w+)\s+(not\s+)?in\s*\((.*)\)", expr) or re.fullmatch(r"(\w+)\s*(=|<>|!=)\s*('(?:[^']|'')*')", expr)
    if m and value(m.group(1))[3]:
        # An existing column whose earlier CHECK names every value it can hold.
        dom = value(m.group(1))[3]
        vals = {v.lower() for v in RE_LITERAL.findall(m.group(3))}
        negate = bool(m.group(2)) and m.group(2).strip() in ("not", "<>", "!=")
        if dom <= vals:
            return "F" if negate else "T"
        if not dom & vals:
            return "T" if negate else "F"
        return "D"
    cols = [w for w in re.findall(r"(?<!['\w])([a-z_]\w*)\b(?!\s*\()", re.sub(r"'(?:[^']|'')*'", "''", expr))
            if w not in SQL_WORDS]
    states = [value(c) for c in cols]
    if any(s[0] == "null" for s in states):
        return "N"
    if cols and all(s[0] == "lit" for s in states):
        got = _literal_atom(expr, dict(zip(cols, (s[1] for s in states))))
        if got != "D":
            return got
    if cols and all(s[2] for s in states):
        # Only columns this migration adds: a build that does not know them writes
        # their defaults, which the same migration gives them to satisfy this.
        return "T"
    return "D"


SQL_WORDS = {"and", "or", "not", "in", "is", "null", "true", "false", "like", "ilike", "between",
             "array", "any", "all", "some", "similar", "to", "escape", "collate", "text", "uuid",
             "int", "integer", "bigint", "numeric", "boolean", "varchar", "jsonb", "json",
             "timestamptz", "timestamp", "date", "interval", "current_date", "current_timestamp", "now"}


def _literal_atom(expr: str, lits: dict[str, str]) -> str:
    """A comparison of columns whose value is a known literal: = <>, IN, NOT IN."""
    m = re.fullmatch(r"(\w+)\s*(=|<>|!=|is not|is)\s*(true|false)", expr)
    if m and m.group(1) in lits and lits[m.group(1)] in ("true", "false"):
        eq = lits[m.group(1)] == m.group(3)
        return "T" if eq == (m.group(2) in ("=", "is")) else "F"
    m = re.fullmatch(r"(\w+)\s*(=|<>|!=)\s*'((?:[^']|'')*)'", expr)
    if m and m.group(1) in lits:
        eq = lits[m.group(1)] == m.group(3).lower()
        return "T" if eq == (m.group(2) == "=") else "F"
    m = re.fullmatch(r"(\w+)\s+(not\s+)?in\s*\((.*)\)", expr)
    if m and m.group(1) in lits:
        vals = {v.lower() for v in RE_LITERAL.findall(m.group(3))}
        return "T" if (lits[m.group(1)] in vals) != bool(m.group(2)) else "F"
    m = re.fullmatch(r"(\w+)\s*(>=|<=|>|<|=|<>|!=)\s*(-?\d+(?:\.\d+)?)", expr)
    if m and m.group(1) in lits and re.fullmatch(r"-?\d+(?:\.\d+)?", lits[m.group(1)]):
        a, b = float(lits[m.group(1)]), float(m.group(3))
        ok = {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "=": a == b, "<>": a != b, "!=": a != b}[m.group(2)]
        return "T" if ok else "F"
    m = re.fullmatch(r"(\w+)\s+between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", expr)
    if m and m.group(1) in lits and re.fullmatch(r"-?\d+(?:\.\d+)?", lits[m.group(1)]):
        return "T" if float(m.group(2)) <= float(lits[m.group(1)]) <= float(m.group(3)) else "F"
    return "D"


def column_state(tbl: str, new_cols: dict[str, tuple[str, str]], notnull: set,
                 domains: dict | None = None) -> "callable":
    """value(col) -> (state, literal, added by this migration, allowed values): what a
    build that predates the migration can have written into col. The allowed values
    are an earlier CHECK's `col IN (...)` list, which held every row that build wrote."""
    domains = domains or {}

    def value(col: str) -> tuple[str, str, bool, frozenset | None]:
        if col in new_cols:
            return (*new_cols[col], True, None)
        dom = domains.get((tbl, col))
        if (tbl, col) in notnull:
            return ("set", "", False, dom)
        return ("unknown", "", False, dom)
    return value


RE_IN_DOMAIN = re.compile(r"\bCHECK\s*\(\s*\(?\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)?\s*\)", re.I)


def track_domains(text: str, domains: dict) -> None:
    """Fold a migration's single-column `col IN (...)` CHECKs into domains, by
    (table, column): the values every row written under them holds."""
    for stmt, _ in statements(text):
        cm = RE_CRE_TBL.search(stmt) or RE_ALT_TBL.search(stmt)
        if not cm:
            continue
        tbl = obj_name(cm.group(1))
        for m in RE_IN_DOMAIN.finditer(stmt):
            domains[(tbl, m.group(1).lower())] = frozenset(v.lower() for v in RE_LITERAL.findall(m.group(2)))


def track_not_null(stmt: str, notnull: set) -> None:
    """Fold one statement into the set of (table, column) that are NOT NULL."""
    cm = RE_CRE_TBL.search(stmt)
    if cm:
        tbl = obj_name(cm.group(1))
        body, _ = table_body_off(stmt)
        for part in split_top(body):
            pk = RE_PK_TABLE.search(part)
            if CONSTRAINT_KW.match(part):
                if pk:
                    notnull.update((tbl, c.strip().lower()) for c in pk.group(1).split(","))
                continue
            pm = re.match(r"\s*(\w+)\s+(.+)", part, re.S)
            if pm and re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", pm.group(2), re.I):
                notnull.add((tbl, pm.group(1).lower()))
        return
    t = RE_ALT_TBL.search(stmt)
    if not t:
        return
    tbl = obj_name(t.group(1))
    for m in RE_ADD_COLUMN_CLAUSE.finditer(stmt):
        if re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", split_top(stmt[m.end():])[0], re.I):
            notnull.add((tbl, m.group(1).lower()))
    for m in re.finditer(r"\bALTER\s+(?:COLUMN\s+)?(\w+)\s+(SET|DROP)\s+NOT\s+NULL\b", stmt, re.I):
        (notnull.add if m.group(2).upper() == "SET" else notnull.discard)((tbl, m.group(1).lower()))


# --- definitions read from earlier migrations ---------------------------------------

def last_index_def(raw: str, name: str) -> tuple[bool, str] | None:
    """(unique, normalised definition) of the last CREATE INDEX of `name`, or None."""
    text = raw_code(raw)
    found = None
    for m in RE_CREATE_ANY_INDEX.finditer(text):
        if obj_name(m.group(2)) == name:
            end = text.find(";", m.end())
            found = (bool(m.group(1)), normalised(text[m.start(3):end if end > 0 else len(text)]))
    return found


def index_def(code: str, m: re.Match) -> str:
    end = code.find(";", m.end())
    return normalised(code[m.start(2):end if end > 0 else len(code)])


def routine_signature(text: str, start: int) -> tuple[str, ...]:
    """The argument types of the routine whose parameter list opens at `start`."""
    args = balanced(text, start)
    out = []
    for part in split_top(args):
        part = re.split(r"\s+default\s+|\s*=\s*", part.strip().lower())[0].strip()
        words = part.split()
        if words and words[0] in ("in", "out", "inout", "variadic"):
            words = words[1:]
        if len(words) >= 2 and words[0] not in TYPE_WORDS:
            words = words[1:]
        if words:
            out.append(" ".join(words))
    return tuple(out)


def routine_def(text: str, name: str) -> tuple[tuple[str, ...], str] | None:
    """(argument types, normalised RETURNS) of the last CREATE of routine `name`."""
    found = None
    for m in RE_ROUTINE_DEF.finditer(text):
        if obj_name(m.group(2)) == name:
            sig = routine_signature(text, m.end() - 1)
            after = text[m.end() - 1 + len(balanced(text, m.end() - 1)) + 2:]
            r = re.match(r"\s*returns\s+(.*?)(?:\s+(?:language|as|immutable|stable|volatile|security|set|strict)\b|$)",
                         after, re.I | re.S)
            found = (sig, normalised(r.group(1)) if r else "")
    return found


def contract_sites(stmt: str, sline: int, span: tuple[int, int], rel: str, raw_stmt: str, ctx: dict) -> list[dict]:
    """Every SCHEMA-0007 site of one statement (destructive SCHEMA-0001 forms are added
    where SCHEMA-0001 finds them). `stmt` locates (its literals are blanked), and
    `raw_stmt`, the statement's own lines as written, gives the literals a CHECK, a
    default or an index predicate is judged by."""
    code = code_of(stmt)
    rawc = raw_code(raw_stmt)
    sites: list[dict] = []

    def site(form: str, offset: int, detail: str) -> None:
        sites.append({"rule": "contract", "form": form, "file": rel,
                      "line": line_of(stmt, sline, offset), "span": span, "detail": detail})

    # Types, attributes, views: gone or renamed.
    for name, pat in CONTRACT:
        if name == "drop_routine":
            continue
        for m in pat.finditer(code):
            obj = obj_name(m.group(2) or "")
            if obj and obj in ctx["created_names"]:
                continue
            what = " ".join(m.group(1).lower().split())
            site(name, m.start(), f"drop {what} {obj}" if obj
                 else f"{'rename' if name == 'rename_value' else 'drop'} {what} of a type")
    for m in re.finditer(r"\bDROP\s+(VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+EXISTS\s+)?([\w.]+)", code, re.I):
        if obj_name(m.group(2)) not in ctx["created_names"]:
            site("drop_routine", m.start(), f"drop {' '.join(m.group(1).lower().split())} {obj_name(m.group(2))}")

    # Functions and procedures: gone, or back with another signature or result.
    for m in RE_ROUTINE_DROP.finditer(code):
        name = obj_name(m.group(2))
        kind = m.group(1).lower()
        new = ctx["routines_here"].get(name)
        if new is None:
            site("drop_routine", m.start(), f"drop {kind} {name}")
            continue
        old_sig = routine_signature(code, m.end() - 1) if m.group(3) else None
        old = routine_def(ctx["raw_before"], name)
        if old_sig is None and old:
            old_sig = old[0]
        if old_sig is None or old_sig != new[0] or (old and old[1] and new[1] and old[1] != new[1]):
            site("drop_routine", m.start(), f"{kind} {name} comes back as {name}({', '.join(new[0])}), "
                 f"not {name}({', '.join(old_sig or ('?',))})")

    t = RE_ALT_TBL.search(code)
    tbl = obj_name(t.group(1)) if t else ""
    existing = bool(tbl) and tbl not in ctx["created_here"]
    new_cols = ctx["new_cols"].get(tbl, {})
    value = column_state(tbl, new_cols, ctx["notnull_before"], ctx["domains"])

    if existing:
        for m in RE_ADD_CON_ANY.finditer(code):
            clause = split_top(code[m.end(2):])[0]
            kind = m.group(2).upper().split()[0]
            con = m.group(1).lower()
            cols = constrained_words(clause, m.group(2))
            if kind == "CHECK":
                body = check_body(raw_stmt, con)
                if body is None:
                    body = normalised(balanced(clause, clause.find("("))) if "(" in clause else ""
                if con in ctx["dropped_constraints"]:
                    old_body = check_body(ctx["raw_before"], con)
                    if old_body is not None and (widens(old_body, body) or widens(old_body, normalised(fold(body, value)))):
                        continue
                if body and judge(body, value) in ("T", "N"):
                    continue
                site("narrow_constraint", m.start(), f"CHECK {m.group(1)} on {tbl} can refuse a row "
                     f"an older build writes{' (NOT VALID still checks new rows)' if 'not valid' in clause.lower() else ''}")
            elif kind == "FOREIGN":
                if cols and all(new_cols.get(c, ("unknown",))[0] == "null" for c in cols):
                    continue
                site("narrow_constraint", m.start(), f"FOREIGN KEY {m.group(1)} on {tbl}({', '.join(sorted(cols))}) "
                     f"refuses a row an older build writes")
            elif kind == "UNIQUE":
                if con in ctx["dropped_constraints"] and ctx["old_unique"].get(con) == tuple(sorted(cols)):
                    continue
                if ctx["pk"].get(tbl) and ctx["pk"][tbl] <= cols:
                    continue
                if any(new_cols.get(c, ("unknown",))[0] == "null" for c in cols) and con not in ctx["dropped_constraints"]:
                    continue
                site("narrow_constraint", m.start(), f"UNIQUE {m.group(1)} on {tbl}({', '.join(sorted(cols))}) "
                     f"refuses a duplicate an older build may write, or moves its ON CONFLICT target")
            else:
                site("narrow_constraint", m.start(), f"{m.group(2).upper()} {m.group(1)} on {tbl} "
                     f"refuses rows an older build writes, or moves its ON CONFLICT target")
        for m in RE_DROP_DEFAULT.finditer(code):
            col = m.group(1).lower()
            if col not in new_cols:
                site("drop_default", m.start(), f"{tbl}.{col} loses its default, which an older build's INSERT relies on")
        for m in RE_RLS.finditer(code):
            site("row_security", m.start(), f"{m.group(1).upper()} ROW LEVEL SECURITY on {tbl}, whose rows an older "
                 f"build reads and writes without that policy")
    for m in RE_ALTER_DOMAIN_ADD.finditer(code):
        site("narrow_constraint", m.start(), f"a constraint added to domain {obj_name(m.group(1))}")

    # Unique indexes: one that is new on a table an older build writes, and one whose
    # definition changes under the same name (an ON CONFLICT names its columns).
    for m in RE_CREATE_UNIQUE_INDEX.finditer(code):
        name, itbl = obj_name(m.group(1)), obj_name(m.group(2))
        if itbl in ctx["created_here"]:
            continue
        rm = next((x for x in RE_CREATE_UNIQUE_INDEX.finditer(rawc) if obj_name(x.group(1)) == name), None)
        new_def = index_def(rawc, rm) if rm else index_def(code, m)
        old = last_index_def(ctx["raw_before"], name) if name in ctx["dropped_indexes"] else None
        if old:
            if old[1] == new_def:
                continue
            site("unique_index", m.start(), f"unique index {name} on {itbl} is rebuilt on another definition: "
                 f"an older build's ON CONFLICT on the old columns finds no matching index")
            continue
        on = new_def.split("(", 1)
        icols = set(re.findall(r"[a-z_]\w*", on[1].split(")", 1)[0])) - SQL_WORDS if len(on) > 1 else set()
        inew = ctx["new_cols"].get(itbl, {})
        ivalue = column_state(itbl, inew, ctx["notnull_before"], ctx["domains"])
        where = re.search(r"\bwhere\b(.*)$", new_def)
        if where and judge(where.group(1).strip(), ivalue) in ("F", "N"):
            continue
        if "nulls not distinct" not in new_def and any(inew.get(c, ("unknown",))[0] == "null" for c in icols):
            continue
        if any(inew.get(c, ("unknown",))[0] == "ident" for c in icols):
            continue
        if ctx["pk"].get(itbl) and ctx["pk"][itbl] <= icols:
            continue
        site("unique_index", m.start(), f"unique index {name} on {itbl}({', '.join(sorted(icols))}) refuses a "
             f"duplicate an older build may write")
    dm = RE_DROP_INDEX.search(code.strip())
    if dm:
        for raw_name in dm.group(1).split(","):
            name = obj_name(raw_name)
            if not name or name in ctx["unique_here"]:
                continue
            # Only a unique index an earlier migration made: a plain index carries no
            # ON CONFLICT target, and one this migration re-creates is judged there.
            old = last_index_def(ctx["raw_before"], name)
            if old and old[0]:
                site("unique_index", dm.start(), f"unique index {name} is dropped: an older build's ON CONFLICT "
                     f"on its columns finds no matching index")

    # Privileges taken away from objects an older build uses.
    for m in RE_REVOKE.finditer(code):
        target = m.group(2).strip()
        if re.match(r"all\s+(tables|sequences|functions|routines)\s+in\s+schema\b", target, re.I):
            site("revoke", m.start(), f"REVOKE {' '.join(m.group(1).split())} ON {' '.join(target.split())}")
            continue
        target = re.sub(r"^(table|sequence|function|procedure|routine|schema|database|type|domain)\s+", "",
                        target, flags=re.I)
        names = [obj_name(re.split(r"\(", n)[0]) for n in split_top(target)]
        if names and all(n in ctx["created_here"] or n in ctx["created_names"] for n in names):
            continue
        site("revoke", m.start(), f"REVOKE {' '.join(m.group(1).split())} ON {' '.join(m.group(2).split())}, which "
             f"an older build may use")
    return sites


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
    """Run git, or exit 2. Never let a git failure surface as a finding.

    check=True raised CalledProcessError out of this function and Python printed a
    traceback and exited 1 - this gate's code for "a control found a violation". A
    SHALLOW clone hit it every time: `rev-parse --verify origin/main` succeeds on the
    ref, and `merge-base` then fails because the history is not there, so the guard
    above this passed and the crash was reported as a non-conformant repository.
    """
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if res.returncode != 0:
        cannot_run(
            f"git {' '.join(args)} failed in {root}: "
            f"{(res.stderr or res.stdout).strip().splitlines()[0] if (res.stderr or res.stdout).strip() else 'no output'}. "
            f"A shallow clone is the usual cause - the checkout needs fetch-depth: 0")
    return res.stdout.strip()


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
    # SCHEMA-0007: the release a contract migration names, per file, and why it is
    # not a release of this module before its head ("" when it is). A name is checked
    # against the repository's own tags, never taken on trust.
    contract_release_files: dict[str, str] = {}
    contract_release_problems: dict[str, str] = {}
    release_of: dict[str, tuple[str, str]] = {}
    pk_columns: dict[str, set[str]] = {}
    enum_types: set[str] = set()
    check_bodies: list[str] = []
    described_tables: set[str] = set()

    # Pass 1 over the WHOLE repository: enum types and every CHECK body, so a
    # constraint declared in an earlier migration still counts for a later column.
    # Table comments are gathered here for the same reason: a table created in the
    # baseline and described by a later migration IS described, and in diff mode
    # only that later migration is a target.
    #
    # Columns are gathered per file, in migration order, for SCHEMA-0007: a constraint
    # narrows only what a table already held when the migration runs, so a CHECK over a
    # column added in the same migration constrains nothing an older build writes.
    columns_before: dict[Path, set[tuple[str, str]]] = {}
    raw_before: dict[Path, str] = {}
    seen_columns: set[tuple[str, str]] = set()
    # Which columns were NOT NULL before each file: an older build wrote a value there,
    # so `col IS NOT NULL` holds for every row it writes.
    notnull_before: dict[Path, set[tuple[str, str]]] = {}
    seen_notnull: set[tuple[str, str]] = set()
    seen_raw = ""
    # Where each object was first created: a contract's release must come after it.
    created_in: dict[tuple, Path] = {}
    domains_before: dict[Path, dict] = {}
    seen_domains: dict[tuple[str, str], frozenset] = {}
    for f in all_files:
        domains_before[f] = dict(seen_domains)
        columns_before[f] = set(seen_columns)
        notnull_before[f] = set(seen_notnull)
        raw_before[f] = seen_raw
        try:
            raw_all = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen_raw += "\n" + raw_all
        track_domains(raw_code(raw_all), seen_domains)
        sql = strip_comments(raw_all)
        for stmt, sline in statements(sql):
            for m in RE_ENUM.finditer(stmt):
                enum_types.add(m.group(1).split(".")[-1].lower())
            for m in re.finditer(r"\bCHECK\s*\(", stmt, re.I):
                check_bodies.append(balanced(stmt, m.end() - 1))
            for m in RE_COMMENT_TBL.finditer(stmt):
                described_tables.add(m.group(1).split(".")[-1].lower())
            for col in _columns(stmt, sline, set()):
                seen_columns.add((col["table"], col["column"]))
            track_not_null(stmt, seen_notnull)
            for col in _columns(stmt, sline, set()):
                created_in.setdefault(("column", col["table"], col["column"]), f)
            for key, rx in (("table", RE_CRE_TBL), ("routine", RE_ROUTINE_DEF), ("type", RE_CREATED_TYPE),
                            ("index", RE_CREATE_INDEX_NAME), ("constraint", RE_CONSTRAINT_NAME)):
                for m in rx.finditer(stmt):
                    created_in.setdefault((key, obj_name(m.group(m.lastindex))), f)
            cm = RE_CRE_TBL.search(stmt)
            if cm:
                tbl = cm.group(1).split(".")[-1].lower()
                body, _ = table_body_off(stmt)
                for part in split_top(body):
                    pm = RE_PK_TABLE.search(part) if CONSTRAINT_KW.match(part) else None
                    if pm:
                        pk_columns[tbl] = {c.strip().lower() for c in pm.group(1).split(",")}
                    elif not CONSTRAINT_KW.match(part):
                        im = RE_PK_INLINE.match(part)
                        if im:
                            pk_columns[tbl] = {im.group(1).lower()}

    sites: list[dict] = []
    markers: list[dict] = []
    stmt_spans: list[dict] = []
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
        rm = RE_RELEASE.search(raw)
        if rm and expand_re.pattern and expand_re.search(raw):
            contract_release_files[rel] = f"{rm.group('module')} {rm.group('version')}"
            release_of[rel] = (rm.group("module"), rm.group("version"))

        sql = strip_comments(raw)
        raw_lines = raw.split("\n")
        stmts = statements(sql)
        created_here = set()
        readded: set[str] = set()
        dropped_constraints: set[str] = set()
        readded_any: set[str] = set()
        indexes_here: set[str] = set()
        unique_here: set[str] = set()
        dropped_indexes: set[str] = set()
        new_cols: dict[str, dict[str, tuple[str, str]]] = {}
        created_names: set[str] = set()
        added_here: set[tuple[str, str]] = set()
        for stmt, sline in stmts:
            m = RE_CRE_TBL.search(stmt)
            if m:
                created_here.add(m.group(1).split(".")[-1].lower())
            for cm2 in RE_CREATED_NAME.finditer(stmt):
                created_names.add(cm2.group(1).split(".")[-1].lower())
            for dm in re.finditer(r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)", stmt, re.I):
                dropped_constraints.add(dm.group(1).lower())
            for am in RE_ADD_CON_ANY.finditer(stmt):
                readded_any.add(am.group(1).lower())
            for im in RE_CREATE_ANY_INDEX.finditer(stmt):
                indexes_here.add(obj_name(im.group(2)))
                if im.group(1):
                    unique_here.add(obj_name(im.group(2)))
            dim = RE_DROP_INDEX.search(code_of(stmt).strip())
            if dim:
                dropped_indexes.update(obj_name(n) for n in dim.group(1).split(","))
            at = RE_ALT_TBL.search(stmt)
            if at:
                lo_, hi_ = sline, sline + stmt.count("\n")
                new_cols.setdefault(obj_name(at.group(1)), {}).update(
                    old_build_columns(raw_code("\n".join(raw_lines[lo_ - 1:hi_])), obj_name(at.group(1))))
            for col in _columns(stmt, sline, set()):
                added_here.add((col["table"], col["column"]))
            # A CHECK constraint cannot be extended in place, so the house idiom is
            # DROP CONSTRAINT IF EXISTS x followed by ADD CONSTRAINT x in the same
            # migration - which keeps the table constrained for the whole transaction
            # and loses nothing. Without this, that idiom reads as eight destructive
            # statements in chat-core-postgres 000004 alone, and a gate whose loudest
            # output is its own false positives gets switched off.
            for am in RE_ADD_CON.finditer(stmt):
                readded.add(am.group(1).lower())

        rawc_file = raw_code(raw)
        routines_here = {}
        for rm_ in RE_ROUTINE_DEF.finditer(rawc_file):
            got = routine_def(rawc_file, obj_name(rm_.group(2)))
            if got:
                routines_here[obj_name(rm_.group(2))] = got
        before = raw_before.get(f, "")
        old_unique = {m_.group(1).lower(): tuple(sorted(c.strip().lower() for c in m_.group(2).split(",")))
                      for m_ in re.finditer(r"\bCONSTRAINT\s+(\w+)\s+UNIQUE\s*\(([^)]*)\)", raw_code(before), re.I)}
        ctx = {"created_here": created_here, "created_names": created_names, "routines_here": routines_here,
               "raw_before": before, "new_cols": new_cols, "notnull_before": notnull_before.get(f, set()),
               "dropped_constraints": dropped_constraints, "old_unique": old_unique, "pk": pk_columns,
               "dropped_indexes": dropped_indexes, "indexes_here": indexes_here, "unique_here": unique_here,
               "domains": domains_before.get(f, {})}

        for stmt, sline in stmts:
            span = (sline, sline + stmt.count("\n"))
            # Every DDL statement's span, whatever this pass decides about it. Only
            # SCHEMA-0005 reads these: a marker is stale when the code it excused has
            # MOVED OR GONE, and a marker sitting beside a statement the parser happens
            # to auto-exempt has not gone stale - it is a defensive declaration that
            # agrees with the parser. Judging orphanhood against findings alone failed
            # the build for writing one.
            stmt_spans.append({"file": rel, "span": span})

            for name, pat in DESTRUCTIVE:
                for m in pat.finditer(stmt):
                    t = RE_ALT_TBL.search(stmt)
                    tbl = t.group(1).split(".")[-1].lower() if t else ""
                    if tbl and tbl in created_here:
                        continue
                    if name == "drop_table":
                        # A table this migration created is one it may drop: the
                        # scratch/backfill idiom is CREATE TABLE tmp_x, populate from
                        # it, DROP TABLE tmp_x, and nothing outside the migration ever
                        # saw it. tbl is "" for DROP TABLE because there is no ALTER
                        # TABLE to read it from, so the same-migration check above
                        # could not fire.
                        dt = RE_DROP_TABLE_NAME.search(stmt[m.start():])
                        if dt and dt.group(1).split(".")[-1].lower() in created_here:
                            continue
                        if dt:
                            tbl = dt.group(1).split(".")[-1].lower()
                    if name == "add_column_not_null_without_default":
                        # DEFAULT anywhere in this column's clause makes the addition
                        # safe, whichever side of NOT NULL it is written on.
                        tail = stmt[m.start():]
                        clause = split_top(tail)[0] if split_top(tail) else tail
                        if re.search(r"\bDEFAULT\b|\bGENERATED\b", clause, re.I):
                            continue
                    contract = True
                    if name == "drop_constraint":
                        dm = re.search(r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)",
                                       stmt[m.start():], re.I)
                        if dm and dm.group(1).lower() in readded:
                            continue
                        # SCHEMA-0007: a constraint re-added under its name is judged
                        # as the re-add; a dropped CHECK or FOREIGN KEY accepts more,
                        # which no older build can trip on.
                        if dm and (dm.group(1).lower() in readded_any
                                   or dropped_kind(dm.group(1).lower(), raw_before.get(f, "")) in ("CHECK", "FOREIGN")):
                            contract = False
                    sites.append({"rule": "destructive", "form": name, "file": rel, "contract": contract,
                                  "objects": objects_of(code_of(stmt), tbl),
                                  "line": line_of(stmt, sline, m.start()), "span": span,
                                  "detail": f"{name.replace('_', ' ')} on "
                                            f"{tbl or 'an existing object'}"})

            # SCHEMA-0007's own forms: what an older build's statements trip on.
            n_before = len(sites)
            sites += contract_sites(stmt, sline, span, rel, "\n".join(raw_lines[span[0] - 1:span[1]]), ctx)
            tm = RE_ALT_TBL.search(code_of(stmt))
            for s_ in sites[n_before:]:
                s_["objects"] = objects_of(code_of(stmt), obj_name(tm.group(1)) if tm else "")

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

            # A partition child is excluded: it carries no identity of its own and
            # inherits the parent description, so demanding one would add a comment
            # per month of retention.
            cm = RE_CRE_TBL.search(stmt)
            if cm and not RE_PARTITION_OF.search(stmt):
                sites.append({"rule": "table", "file": rel,
                              "line": line_of(stmt, sline, cm.start()), "span": span,
                              "detail": cm.group(1).split(".")[-1].lower(),
                              "table": cm.group(1).split(".")[-1].lower()})

            for col in _columns(stmt, sline, enum_types):
                sites.append({"rule": "column", "file": rel, "line": col["line"],
                              "span": span, "detail": f"{col['table']}.{col['column']}",
                              "column": col})

    # A contract's release is judged against what its own sites drop or change.
    commit_of: dict[Path, str] = {}
    for rel_, (module, version) in release_of.items():
        created: dict[str, str] = {}
        for s_ in sites:
            if s_["file"] != rel_ or s_["rule"] not in ("destructive", "contract"):
                continue
            for obj in s_.get("objects") or []:
                src = created_in.get(obj) or (created_in.get(("table", obj[1])) if obj[0] == "column" else None)
                label = " ".join(obj[1:]) if obj[0] != "column" else f"{obj[1]}.{obj[2]}"
                if src is None:
                    created[f"{obj[0]} {label}"] = ""
                    continue
                if src not in commit_of:
                    _, log = _git(root, "log", "--diff-filter=A", "--format=%H", "--", str(src.relative_to(root)))
                    commit_of[src] = log.split("\n")[-1] if log else ""
                created[f"{obj[0]} {label}"] = commit_of[src] or ""
        contract_release_problems[rel_] = release_problem(root, module, version, created)

    return {"files_scanned": files_scanned, "sites": sites, "markers": markers,
            "module": module_of(root),
            "stmt_spans": stmt_spans,
            "expand_contract_files": expand_contract_files,
            "contract_release_files": contract_release_files,
            "contract_release_problems": contract_release_problems,
            "enum_types": enum_types, "check_bodies": check_bodies,
            "described_tables": described_tables,
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
        # `<=` at the lower bound, so a marker written as a TRAILING comment on the
        # line it excuses counts. Requiring it strictly above refused the most natural
        # placement for a column: `note_kind text, -- schema:allow reason=free-form ...`
        if 0 <= site["line"] - mk["line"] <= reach:
            return True
        lo, hi = site["span"]
        if site["rule"] != "column":
            # Within reach of where the STATEMENT begins, not of where the finding
            # landed. Counting to the finding meant the reach had to absorb the
            # statement's own internal layout: a goose-guarded constraint puts
            # `ALTER TABLE` on one line and `ADD CONSTRAINT` on the next, so the
            # distance depended on how the author wrapped their SQL. Anchoring to the
            # statement start is what "the marker sits above this statement" actually
            # means, and it does not change with formatting.
            if 0 <= lo - mk["line"] <= reach:
                return True
            # A marker inside the statement's own span covers it, but only for
            # statement sites: a CREATE TABLE spans every column it declares, so span
            # containment would hand one marker the whole table.
            if lo <= mk["line"] <= hi:
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


def rule_contract_names_release(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    """SCHEMA-0007: a contract migration names the release that stopped using it.

    Every SCHEMA-0001 site that removes or narrows (not a dropped CHECK or FOREIGN
    KEY, which only accept more) and every contract site counts. The release is read
    from the file's own marker and must be a tag of this module before its head
    (release_problem). `empty-table` and `nothing-refused` excuse only a narrowed
    constraint the parser cannot judge, never a drop or a rename: those are cleared
    by a named, verified release alone.
    """
    c = ctl.get("SCHEMA-0007")
    if not c or c["status"] != "active":
        return []
    # Report-only until a repository is switched on: enforce_in names the modules
    # whose findings fail CI; everywhere else the control's own stage applies.
    if facts.get("module") in {str(m).lower() for m in c.get("enforce_in") or []}:
        c = dict(c, stage="enforce")
    named = facts.get("contract_release_files") or {}
    problems = facts.get("contract_release_problems") or {}
    out: list[Finding] = []
    for s in facts["sites"]:
        if s["rule"] == "destructive" and not s.get("contract", True):
            continue
        if s["rule"] not in ("destructive", "contract"):
            continue
        if s.get("form") == "narrow_constraint" and excused(s, facts["markers"], {"empty-table", "nothing-refused"}):
            continue
        if s["file"] in named and not problems.get(s["file"]):
            continue
        why = (f"its marker {problems[s['file']]}" if s["file"] in named
               else "it names no release that stopped using what it removes or newly refuses")
        out.append(_mk(c, s, f"contract migration: {why}: {s['detail']}"))
    return out


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


def rule_table_without_comment(facts: dict, doc: dict, ctl: dict) -> list[Finding]:
    """SCHEMA-0006: a table this PR creates carries no COMMENT ON TABLE.

    Presence only. What a good description says is a review question, and a gate
    that tried to judge wording would be a gate people learn to satisfy with one
    word. The point is that the answer lives in pg_description rather than in a
    `--` banner no tool, BI client or `\\d+` can read.

    Satisfied by a comment anywhere in the repository, not just in the same
    migration, because a migration is immutable once applied: describing an older
    table means adding a new migration that comments it, and that must count.
    """
    c = ctl.get("SCHEMA-0006")
    if not c or c["status"] != "active":
        return []
    described = facts.get("described_tables") or set()
    return [_mk(c, s, f"table created with no COMMENT ON TABLE: {s['detail']}")
            for s in facts["sites"]
            if s["rule"] == "table"
            and s["table"] not in described
            and not excused(s, facts["markers"], {"scratch-table"})]


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
            and (0 <= s["line"] - mk["line"] <= reach_for(s)
                 or (s["rule"] != "column" and s["span"][0] <= mk["line"] <= s["span"][1]))
            for s in facts["sites"])
        # Or any DDL statement at all within reach. See stmt_spans in collect(): a
        # marker beside a statement the parser auto-exempts is a declaration that
        # agrees with the parser, not an excuse that outlived its code.
        if not covers:
            covers = any(
                s["file"] == mk["file"]
                and (0 <= s["span"][0] - mk["line"] <= MARKER_REACH_STATEMENT
                     or s["span"][0] <= mk["line"] <= s["span"][1])
                for s in facts.get("stmt_spans") or [])
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
    findings += rule_table_without_comment(facts, doc, ctl)
    findings += rule_contract_names_release(facts, doc, ctl)
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

def write_summary(findings: list[Finding], mode: str) -> None:
    """Every finding, enforced or not, as a table in the job summary, where a
    report-only control is read: an annotation alone is easy to miss."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### Schema lint ({mode})", ""]
    if not findings:
        lines.append("No findings.")
    else:
        lines += ["| Control | Stage | Where | Finding |", "|---|---|---|---|"]
        for f in findings:
            msg = f.message.replace("|", "\\|")
            lines.append(f"| {f.control} | {f.stage} | `{f.location}` | {msg} |")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
    except OSError as exc:
        print(f"::warning::schema-lint could not write the job summary: {exc}")


def emit_text(facts: dict, findings: list[Finding], mode: str) -> int:
    write_summary(findings, mode)
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
