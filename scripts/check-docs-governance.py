#!/usr/bin/env python3
"""Documentation-governance guard (CI). Executable, data-driven policy-as-code for the
generic documentation-governance contract every governed docs repo shares.

This is the single source of the documentation-governance LOGIC. It is consumed by the
`docs-governance.yaml` reusable workflow and run against a governed docs repository
(``--root``). It is the docs analogue of scripts/check-delivery-model.py /
scripts/check-dockerfile-standard.py.

Framework (how mature governance systems layer):
  ADR (intent) -> control catalog (policy + severity + ownership + lifecycle)
              -> detector (verifies compliance) -> reusable workflow -> CI (executes).

Design:
  * DATA-DRIVEN. controls/docs-governance.yaml defines POLICY ONLY. This file provides
    DETECTOR implementations; each control's `detector` binds it to a function here and may
    evolve (regex -> AST) without touching the catalog.
  * SHARED LOGIC, PER-REPO DATA. The logic is central; a repo owns only its data:
    governance/OWNER_DIRECTORY.md, optional governance/CLIENT_SCOPE.md, and its catalog/.
    Controls declare `applies_when` (always | client-scope | catalog); a control is skipped
    when its capability is absent (e.g. the shared-engine repo, which legitimately names
    every client, carries no CLIENT_SCOPE.md and is never subject to client-scope isolation).
  * DELEGATED DOMAIN LOGIC. Catalog schema + generated-artifact drift is domain-specific
    (each repo's catalog models its own domain), so it is DELEGATED to the repo's own
    scripts/build_catalog.py --check, invoked by the reusable workflow. It is declared here
    (DOC-0010) but never duplicated.
  * SEVERITY- + LIFECYCLE-AWARE. critical/major fail CI; minor is advisory (--fail-on).
  * EVIDENCE-PRODUCING / ACTIONABLE / MACHINE-READABLE / SELF-DOCUMENTING, exactly as the
    delivery-model and dockerfile-standard checkers.

Policy SSOT (architecture owned by the ADR; this checker only enforces it):
  ADR-0081 - Centralized, reusable documentation-governance CI (coderaxis/core-docs)

Usage:
  check-docs-governance.py [root] [--controls PATH] [--config PATH]
                           [--format text|json|markdown] [--fail-on critical|major|minor]
                           [--grace N] [--today YYYY-MM-DD] [--report PATH]
  check-docs-governance.py --write-docs README.md      # regenerate the docs block
  check-docs-governance.py --verify-docs README.md     # fail if the docs block drifted
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

DEFAULT_CONTROLS = Path(__file__).resolve().parent.parent / "controls" / "docs-governance.yaml"
SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"active", "deprecated", "superseded"}
VALID_SCOPE = {"document", "decision-record", "repository", "catalog"}
VALID_APPLIES = {"always", "client-scope", "catalog"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "applies_when",
                   "policy", "rationale", "remediation", "detector", "refs")

DOCS_BEGIN = "<!-- BEGIN docs-governance-controls (generated: scripts/check-docs-governance.py --write-docs) -->"
DOCS_END = "<!-- END docs-governance-controls -->"

# --- platform-wide documentation vocabulary (central defaults; a repo may EXTEND, never
#     shrink, via its optional docs-governance config: extra_doc_types / extra_doc_roots) --
DEFAULT_DOC_ROOTS = (
    "adrs", "architecture", "capabilities", "compliance", "contracts", "domains", "governance",
    "journeys", "messaging", "onboarding", "operations", "playbooks", "policy", "prds",
    "reference", "rfcs", "scalability", "security", "standards", "testing", "workflows",
)
# Document folders are plural. These singular names were in DEFAULT_DOC_ROOTS while the
# directories on disk were already `adrs`/`rfcs`, so the roots resolved to nothing and were
# skipped in silence: 82 ADRs in core-docs had never been validated. DOC-0011 now rejects the
# singular name outright rather than quietly governing nothing.
SINGULAR_FOLDER_FIXES = {"adr": "adrs", "rfc": "rfcs", "prd": "prds", "product": "prds"}
# Folders holding decision records, for DOC-0006 supersession indexing. This was previously
# spelled inline as ("adr", "rfc", "rfcs"), so every ADR under adrs/ fell outside the index
# and supersession was structurally unverifiable for ADRs across the whole fleet.
DECISION_FOLDERS = ("adrs", "rfcs")

# --- identifier vocabulary: one width, one shape, everywhere ------------------------------
# Every identifier on this platform is 4-digit zero-padded: ADR-0001, RFC-0001, PRD-0001, and
# the control ids in github-actions/controls/. Mixed widths are not cosmetic - `DOC-0001` and
# `DOC-0001` are different strings, so a grep for one silently misses citations written as the
# other, and any tool that sorts ids lexically interleaves the two families. 4 digits is also
# the width at which zero-padded sort order matches numeric order for every id this platform
# will ever allocate.
ID_WIDTH = 4
# Numbered documents. The prefix must agree with the folder (an RFC-* file under adrs/ is a
# filing error, not a naming one) and the slug is kebab-case with no leading, trailing or
# doubled hyphen - `[a-z0-9-]+` would admit all three.
NUMBERED_DOC_FOLDERS = {"adrs": "ADR", "rfcs": "RFC", "prds": "PRD"}
DOCUMENT_FILENAME_RE = re.compile(
    r"^(?P<prefix>ADR|RFC|PRD)-(?P<number>\d{4})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
# Files that legitimately live in a numbered-document folder without being one: the generated
# indexes, folder READMEs, and `_`-prefixed partials.
NON_DOCUMENT_STEMS = re.compile(r"^(README|_.*|[A-Z]+_INDEX)$")
# Control ids in github-actions/controls/*.yaml (DOC-0001, API-0001, DM-0001, DS-0001,
# WFC-0001). Enforced here because until now `load_controls` only required `id` to be
# non-empty, so `DOC-1`, `doc-0001` or a bare `1` would all have been accepted.
CONTROL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d{4}$")

# Namespace registry (central policy data) and the per-namespace reserved-id register.
NAMESPACE_REGISTRY = Path(__file__).resolve().parent.parent / "controls" / "doc-namespaces.yaml"
RESERVED_IDS_PATH = Path("governance/RESERVED_IDS.md")
# A citation, optionally preceded by a namespace qualifier. The leading token is only treated as
# a qualifier when it is a registered alias, so ordinary prose ("See ADR-0069", "supersedes
# ADR-0012") reads as a bare citation rather than as a citation into a namespace called "See".
CITATION_RE = re.compile(r"(?:(?P<qual>[A-Za-z][A-Za-z0-9-]*)[ \t]+)?(?P<id>(?:ADR|RFC|PRD)-\d{4})\b")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# IETF RFCs share the `RFC-NNNN` shape with platform RFCs: docs legitimately cite RFC-2119
# (MUST/SHOULD keywords), RFC-3339 (timestamps), RFC-7807/9457 (problem details). Platform RFC
# numbers are allocated from 0001 and are therefore always zero-padded with a leading zero, so an
# unpadded four-digit RFC is an external standard - unless it resolves in a namespace, which
# keeps the transitional `RFC-9\d{3}` convention (RFC-9001) checkable.
EXTERNAL_STANDARD_RE = re.compile(r"^RFC-[1-9]\d{3}$")
# The 9000 block is a reserved band for TRANSITIONAL documents, not part of the main allocation
# sequence - a pre-existing convention that check_capability_certification.py already matches as
# `(RFC|ADR)-9\d{3}`. Excluded from sequence checks, or a single RFC-9001 would report ~9000
# phantom gaps beneath it.
TRANSITIONAL_BAND_FLOOR = 9000
REQUIRED_KEYS = ("owner", "status", "last_reviewed", "review_cycle",
                 "related_services", "related_rfcs", "related_adrs")
VALID_STATUSES = {"draft", "proposed", "accepted", "active", "deprecated", "superseded"}
VALID_REVIEW_CYCLES = {"event-driven", "quarterly", "semiannual", "annual"}
DEFAULT_DOC_TYPES = {
    "architecture-standard", "workflow", "rfc", "adr", "governance-standard", "runbook",
    "index", "scorecard", "roadmap", "reference", "standard", "playbook", "compliance",
    "prd", "dashboard", "lifecycle", "journey", "incident", "known-failure", "diagnostic",
    "ai-context",
}
VALID_TIERS = {"platform-critical", "platform-standard", "platform-supporting"}
VALID_SERVICE_TIERS = {"tier-0", "tier-1", "tier-2", "tier-3", "experimental"}
VALID_CRITICALITY = {"high", "medium", "low"}
CYCLE_DAYS = {"quarterly": 92, "semiannual": 184, "annual": 366}

OWNER_DIRECTORY_PATH = Path("governance/OWNER_DIRECTORY.md")
CLIENT_SCOPE_PATH = Path("governance/CLIENT_SCOPE.md")
ESCALATION_ALIAS_RE = re.compile(r"^[a-z0-9-]+-(oncall|primary)$")
RELATED_DOCS_HEADING_RE = re.compile(r"^##+\s+(?:\d+\.\s*)?related docs\s*$", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
BLOCK_LIST_ITEM_RE = re.compile(r"^  - \S")
TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:")
# Pinned to exactly ID_WIDTH digits. `\d+` accepted `ADR-12-foo.md`, which then entered the
# DOC-0006 supersession index under the id `ADR-12` - an id no citation would ever spell, so
# its chains were unverifiable while reporting as indexed.
DECISION_ID_RE = re.compile(r"^(ADR|RFC)-\d{4}", re.IGNORECASE)
RELATED_LIST_KEYS = ("related_services", "related_rfcs", "related_adrs")
LEGACY_RELATED_KEYS = {"related_rfc": "related_rfcs", "related_adr": "related_adrs"}
MAX_DETAILS = 50  # cap per-control violation lines printed, to avoid flooding CI logs

# Source-code citations in prose, for DOC-0012. Matches a backticked token that names a
# file with a code extension, optionally with a `:NNN` line suffix. Deliberately narrow:
# it must carry a recognised extension, so prose like `Policy` or `make openapi-contract`
# is never treated as a path.
CODE_EXTENSIONS = ("go", "py", "ts", "tsx", "js", "sh", "proto", "sql", "tf", "yaml", "yml")
CODE_CITATION_RE = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:" + "|".join(CODE_EXTENSIONS) + r")(?::\d+)?)`"
)

# --- DOC-0025: normative logging rules appear only in their declared homes ------------------
# The homes are not listed here. They are read from the log schema control file, so that the
# schema and the check that protects it cannot disagree about which document owns the rule.
LOG_SCHEMA_CONTROL_PATH = Path("github-actions/controls/log-schema.yaml")

# A violation needs a subject AND an imperative, close together, with no citation in the block.
# Every part of that is deliberately narrow, because the failure mode that matters for a
# governance check is the false positive: one bad hit teaches every reader that this control
# cries wolf, and a control nobody believes is worth less than no control. A missed restatement
# costs one stale paragraph; a bogus one costs the control.
#
# Subjects come in two tiers. Tier A names something that can only be the service log. Tier B is
# vocabulary this platform also uses elsewhere - `schema_version` is an outbox envelope field in
# ADR-0069 and a log field here, `redact` is an event-gateway policy verb in ADR-0077 - so it
# counts only in a sentence that is already talking about logging.
LOG_SUBJECT_A_RE = re.compile(
    r"""(?ix)
    \b(?:
        log \s (?:record|line|field|level|schema|output|format|entry|envelope)
      | logging \s (?:client|library|call|contract|rule|configuration)
      | (?:structured|json|access|application) \s log
      | logrus | log/slog | slogclient | loggingclient
      | jsonformatter | textformatter
      | LOG_LEVEL | LOG_FORMAT
    )\b
    """
)
# Tier B is mechanism vocabulary, not subject-matter vocabulary. The distinction is load-bearing
# and is the layering this control exists to preserve: the security and privacy standards own
# *what* is PII and *where* it may not go - "PII must not appear in logs, Kafka payloads or
# analytics streams" is their sentence, and it stays theirs. The log schema standard owns *how a
# log record implements that*: which key is masked, which is dropped, what the masked form looks
# like. So a sentence that reaches for a mechanism - redacted, masked, dropped-at-source - has
# crossed into the schema standard's territory and must cite it. A sentence that merely names
# logs as one sink among several has not.
LOG_SUBJECT_B_RE = re.compile(
    r"""(?ix)
    \b(?:
        schema_version | event_name
      | redact(?:s|ed|ion|ing)? | mask(?:s|ed|ing)?
      | stdout | stderr
      | loki \s (?:label|index) | structured \s metadata
      | timestamp \s format | rfc3339 | utc
    )\b
    """
)
LOG_CONTEXT_RE = re.compile(r"(?i)\blog(?:s|ged|ging|ger)?\b")
LOG_IMPERATIVE_RE = re.compile(
    r"""(?ix)
    \b(?:
        must(?:\s+not)? | shall(?:\s+not)? | never | may\s+not
      | (?:is|are)\s+(?:required|prohibited|forbidden|mandatory)
      | do\s+not | don't
    )\b
    """
)
# "Elevated access MUST be covered by an audit-log entry" is a rule about audit records, not
# about how services log. The term is folded away before matching rather than excluded with a
# lookbehind, so that `log entry` stays a single readable alternation above.
AUDIT_LOG_RE = re.compile(r"(?i)\baudit[-\s]?log(s)?\b")
# An imperative and a subject can share a sentence and still be two unrelated claims joined by a
# comma - "the gauge is defined, registered and never written, and that hook only wrote a log
# line" is a finding about metrics, not a logging rule. Requiring them inside one clause-sized
# window is the cheapest approximation of "this sentence states a rule about logging" that does
# not need a parser.
LOG_CLAUSE_WINDOW = 140
# Citing the owner is the whole point of the control, so a block that links or names the owning
# document is never a violation, however imperative it reads. "Logs MUST follow ADR-0101" is the
# behaviour this control is trying to produce, not the behaviour it is trying to stop.
LOG_CITATION_RE = re.compile(
    r"(?i)\b(?:ADR-0101|log-schema-standard(?:\.md)?|log-schema\.yaml)\b"
)
LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]\s|\d+[.)]\s)")
FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")


@dataclass
class Finding:
    ok: bool
    evidence: str
    details: list[str] = field(default_factory=list)


# --- frontmatter primitives (ported verbatim-in-spirit from the per-repo validators) -------

def extract_frontmatter_lines(text: str):
    """Return (lines|None, first_line_number|None, error|None)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], 2, None
    return None, None, "missing closing frontmatter fence"


def parse_frontmatter(text: str):
    """Return (data|None, error|None). data is the parsed mapping, or None if absent/invalid."""
    fm_lines, _, err = extract_frontmatter_lines(text)
    if err is not None:
        return None, err
    if fm_lines is None:
        return None, None
    try:
        data = yaml.safe_load("\n".join(fm_lines) + "\n") or {}
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {getattr(exc, 'problem', exc)}"
    return (data, None) if isinstance(data, dict) else (None, "frontmatter must parse to a YAML object")


def validate_date(value: object) -> bool:
    if isinstance(value, dt.date):
        return True
    if not isinstance(value, str):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def as_date(value: object):
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def is_template_doc(rel: Path) -> bool:
    return "templates" in rel.parts


def is_service_scorecard(rel: Path) -> bool:
    return (
        rel.parts[:2] == ("governance", "scorecards")
        and rel.name != "README.md"
        and not rel.name.startswith("_")
    )


def _decision_id(path: Path):
    m = DECISION_ID_RE.match(path.stem)
    return m.group(0).upper() if m else None


def extract_related_doc_links(text: str):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if RELATED_DOCS_HEADING_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("## "):
            end = i
            break
    return MARKDOWN_LINK_RE.findall("\n".join(lines[start:end]))


def has_related_doc_link(reference: str, links) -> bool:
    token = reference.strip().lower()
    return any(token in f"{label} {target}".lower() for label, target in links)


def validate_related_key_declarations(fm_lines, rel, first_ln):
    errors, counts, first_seen = [], Counter(), {}
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
    for i, line in enumerate(fm_lines):
        m = key_re.match(line)
        if m is None:
            continue
        key = m.group(1)
        if key not in RELATED_LIST_KEYS and key not in LEGACY_RELATED_KEYS:
            continue
        counts[key] += 1
        first_seen.setdefault(key, first_ln + i)
    for legacy, canonical in LEGACY_RELATED_KEYS.items():
        if counts[legacy]:
            errors.append(f"{rel}:{first_seen[legacy]}: frontmatter key '{legacy}' is removed; "
                          f"use '{canonical}' exactly once")
    for key in RELATED_LIST_KEYS:
        if counts[key] > 1:
            errors.append(f"{rel}:{first_seen[key]}: frontmatter key '{key}' appears "
                          f"{counts[key]} times; declare it exactly once")
    return errors


def validate_block_style_list(fm_lines, key, rel, first_ln):
    errors = []
    header = re.compile(rf"^{re.escape(key)}:(.*)$")
    for i, line in enumerate(fm_lines):
        m = header.match(line)
        if m is None:
            continue
        remainder = m.group(1)
        if re.fullmatch(r"\s*\[\s*\]\s*", remainder):
            return errors
        if remainder.strip():
            errors.append(f"{rel}:{first_ln + i}: frontmatter key '{key}' must use block list "
                          f"style (`{key}:` with `  - item` lines) or empty `[]`, not flow style")
            return errors
        j = i + 1
        while j < len(fm_lines):
            item = fm_lines[j]
            if TOP_LEVEL_KEY_RE.match(item):
                break
            if not item.strip():
                errors.append(f"{rel}:{first_ln + j}: frontmatter key '{key}' must not contain "
                              "blank lines inside the list")
            elif not BLOCK_LIST_ITEM_RE.match(item):
                errors.append(f"{rel}:{first_ln + j}: frontmatter key '{key}' items must use "
                              "exactly two-space indent as `  - value`")
            j += 1
        return errors
    return errors


# --- repository context: parse every governed doc once, shared by all detectors ------------

@dataclass
class DocFile:
    path: Path
    rel: Path
    text: str
    fm_lines: list  # list[str] | None
    first_line: int  # int | None
    fm_error: str  # str | None
    data: dict  # dict | None
    parse_error: str  # str | None


class DocsRepo:
    def __init__(self, root: Path, config: dict, today: dt.date, grace: int,
                 source_roots: tuple = (), peer_roots: dict = None):
        self.root = root
        self.today = today
        self.grace = grace
        self.source_roots = tuple(Path(p).resolve() for p in source_roots)
        extra_roots = tuple(config.get("extra_doc_roots") or ())
        self.doc_roots = tuple(dict.fromkeys(DEFAULT_DOC_ROOTS + extra_roots))
        # A default root is allowed to be absent - no repo has all of them. A root the repo
        # explicitly declared must exist, because a typo there governs nothing and says so
        # nowhere.
        self.missing_declared_roots = tuple(r for r in extra_roots if not (root / r).is_dir())
        self.doc_types = DEFAULT_DOC_TYPES | set(config.get("extra_doc_types") or ())
        self.governed: list[DocFile] = self._load_governed()
        # Dot-directories are tooling, not governed documentation. `.git` was already excluded;
        # the general form matters because `docs/inboxxhq-platform-docs/.core-docs/` is a
        # gitignored local mirror of the core docs, 675 files deep and as stale as whenever it
        # was last synced. Scanning it made every content control report against a copy that CI
        # never sees - a local failure that cannot be reproduced in CI, and a CI pass that
        # cannot be reproduced locally.
        self.all_md: list[Path] = [p for p in sorted(root.rglob("*.md"))
                                   if not any(part.startswith(".") for part in p.parts)]
        self.owner_slugs, self.owner_errors = self._load_owner_registry()
        self.has_catalog = (root / "catalog").is_dir()
        self.client_scope_terms = self._load_client_scope_terms()
        self.log_schema_homes = self._load_log_schema_homes()
        self.namespace = config.get("namespace")
        self.namespaces = self._load_namespace_registry()
        self.qualifier_aliases = self._build_qualifier_aliases()
        self.peer_roots = {k: Path(v) if Path(v).is_absolute() else (root / v)
                           for k, v in (config.get("peer_roots") or {}).items()}
        self.peer_roots.update(peer_roots or {})
        self.local_ids = self._ids_in_tree(root)
        self.reserved_ids = self._load_reserved_ids()
        self.domain_vocabulary = tuple(config.get("domain_vocabulary") or ())
        self.platform_concern_domains = tuple(config.get("platform_concern_domains") or ())
        self._peer_id_cache: dict = {}

    @staticmethod
    def _ids_in_tree(root: Path) -> set:
        """Every well-formed numbered document id anywhere in a namespace.

        Deliberately not restricted to adrs/ | rfcs/ | prds/. DOC-0013 governs where a numbered
        document *should* live, but resolution has to answer a different question - does the
        cited document exist - and core-docs really does hold numbered documents outside those
        folders (PRD-0030 and RFC-0030 sit in an architecture/ review bundle). Indexing only the
        canonical folders would report live documents as dangling citations.
        """
        ids = set()
        for path in root.rglob("*.md"):
            # Dot-directories are never part of a namespace's own document set. This is
            # load-bearing, not hygiene: the client hub vendors a full core-docs checkout at
            # `.core-docs/`, so indexing it would register all 80 platform ADRs as the hub's own
            # ids. Every cross-namespace citation would then "resolve" locally, which is the
            # resolve-by-search-order behaviour ADR-0084 explicitly rejects - and the check would
            # report a confident pass while verifying nothing.
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            m = DOCUMENT_FILENAME_RE.match(path.name)
            if m is not None:
                ids.add(f"{m.group('prefix')}-{m.group('number')}")
        return ids

    def _load_namespace_registry(self) -> list:
        if not NAMESPACE_REGISTRY.is_file():
            return []
        data = yaml.safe_load(NAMESPACE_REGISTRY.read_text(encoding="utf-8")) or {}
        entries = data.get("namespaces")
        return [e for e in entries if isinstance(e, dict) and e.get("name")] if isinstance(entries, list) else []

    def _build_qualifier_aliases(self) -> dict:
        """Map every recognised spelling of a namespace to its registry entry, case-folded.

        The canonical qualifier is registered; the namespace directory name and its `-docs`
        stem are also accepted as *recognised* spellings so DOC-0016 can report them as
        non-canonical. Without the aliases, `core ADR-0045` would parse as a bare citation of
        ADR-0045 and be reported as a dangling local id - a true failure, but for the wrong
        reason and with useless remediation.
        """
        aliases: dict = {}
        for entry in self.namespaces:
            name = entry["name"]
            tokens = {entry.get("qualifier") or name, name}
            if name.endswith("-docs"):
                tokens.add(name[: -len("-docs")])
            for token in tokens:
                if token:
                    aliases[str(token).lower()] = entry
        return aliases

    def namespace_qualifier(self, name: str) -> str:
        for entry in self.namespaces:
            if entry["name"] == name:
                return entry.get("qualifier") or name
        return name

    def platform_namespace(self):
        """The namespace registered as the platform tier - the one every other namespace derives from.

        Read from the registry rather than hardcoded, so DOC-0024 keeps working if the platform
        namespace is ever renamed, and so a fleet with no platform tier simply has no obligation
        rather than a broken reference to a namespace that never existed.
        """
        for entry in self.namespaces:
            if entry.get("tier") == "platform":
                return entry.get("name")
        return None

    def peer_ids(self, name: str):
        """Ids in a peer namespace, or None when that namespace is not present in this run."""
        if name in self._peer_id_cache:
            return self._peer_id_cache[name]
        root = self.peer_roots.get(name)
        ids = self._ids_in_tree(root) if root and root.is_dir() else None
        self._peer_id_cache[name] = ids
        return ids

    def _load_reserved_ids(self) -> set:
        path = self.root / RESERVED_IDS_PATH
        if not path.is_file():
            return set()
        data, err = parse_frontmatter(path.read_text(encoding="utf-8"))
        if err is not None or not isinstance(data, dict):
            return set()
        # Three different reasons an id resolves without a document behind it, kept as separate
        # lists so the distinction stays reviewable rather than collapsing into one allowlist:
        # `reserved_ids` are cited-but-not-yet-written; `known_gaps` are numbers that will never be
        # written but are named in prose *about* the numbering sequence; `retired_ids` were
        # allocated, then merged into another document and deleted. A retired id must keep
        # resolving because prose that explains the merge cites it, and it must never be
        # reassigned - reusing it would silently repoint every historical mention.
        resolved = set()
        for key in ("reserved_ids", "known_gaps", "retired_ids"):
            entries = data.get(key)
            if isinstance(entries, list):
                resolved |= {str(e["id"]).strip() for e in entries
                             if isinstance(e, dict) and isinstance(e.get("id"), str)}
        return resolved

    def _load_governed(self) -> list[DocFile]:
        docs: list[DocFile] = []
        for doc_root in self.doc_roots:
            base = self.root / doc_root
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.md")):
                text = path.read_text(encoding="utf-8", errors="ignore")
                fm_lines, first_line, fm_error = extract_frontmatter_lines(text)
                data, parse_error = (None, None)
                if fm_error is None and fm_lines is not None:
                    data, parse_error = parse_frontmatter(text)
                docs.append(DocFile(path, path.relative_to(self.root), text, fm_lines,
                                    first_line, fm_error, data, parse_error))
        return docs

    def resolve_source_path(self, rel: str):
        """Resolve a doc-cited source path to a filesystem path, or None if unresolvable.

        A citation is resolved against the docs repo first, then against any --source-root
        checkouts the caller supplied. Returning None means "cannot be checked here" (the
        owning repository is not present), which DOC-0012 treats as skipped, never failed.
        """
        candidate = self.root / rel
        if candidate.exists():
            return candidate
        for base in self.source_roots:
            candidate = base / rel
            if candidate.exists():
                return candidate
        # Only claim a path is missing when its owning tree is actually present; otherwise
        # the citation points into a repo this run cannot see.
        head = Path(rel).parts[0] if Path(rel).parts else ""
        for base in (self.root,) + self.source_roots:
            if head and (base / head).is_dir():
                return base / rel
        return None

    def with_frontmatter(self) -> list[DocFile]:
        return [d for d in self.governed if d.fm_lines is not None or d.fm_error is not None]

    def _load_owner_registry(self):
        owner_file = self.root / OWNER_DIRECTORY_PATH
        if not owner_file.exists():
            return set(), [f"{OWNER_DIRECTORY_PATH}: missing owner directory used by governance"]
        data, err = parse_frontmatter(owner_file.read_text(encoding="utf-8"))
        if err is not None or data is None:
            return set(), [f"{OWNER_DIRECTORY_PATH}: {err or 'missing frontmatter'}"]
        registry = data.get("owner_registry")
        if not isinstance(registry, list):
            return set(), [f"{OWNER_DIRECTORY_PATH}: frontmatter key 'owner_registry' must be a list"]
        slugs, errors = set(), []
        for idx, entry in enumerate(registry, start=1):
            if not isinstance(entry, dict):
                errors.append(f"{OWNER_DIRECTORY_PATH}: owner_registry item {idx} must be an object")
                continue
            slug = entry.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                errors.append(f"{OWNER_DIRECTORY_PATH}: owner_registry item {idx} needs a non-empty 'slug'")
                continue
            escalation = entry.get("escalation")
            if isinstance(escalation, str) and escalation.strip() and not ESCALATION_ALIAS_RE.match(escalation.strip()):
                errors.append(f"{OWNER_DIRECTORY_PATH}: owner_registry '{slug.strip()}' has invalid "
                              f"escalation '{escalation.strip()}', expected '*-oncall' or '*-primary'")
            # Contact metadata is optional, but a declared-and-empty contact is worse than an
            # absent one: it projects into Backstage as a real route that pages nobody.
            for contact_key in ("slack", "pagerduty"):
                if contact_key in entry:
                    value = entry.get(contact_key)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(
                            f"{OWNER_DIRECTORY_PATH}: owner_registry '{slug.strip()}' has an empty "
                            f"'{contact_key}'; omit it or set a non-empty value")
            slugs.add(slug.strip())
        return slugs, errors

    def _load_log_schema_homes(self):
        """Return the normative_homes declared by the log schema control, or None when the
        control file is not in this checkout.

        The docs repos are checked out standalone in some jobs and as a monorepo subtree in
        others, so the control is looked for at the root and then upwards. Returning None
        rather than [] keeps "I could not find the file" distinguishable from "the file
        declares no homes"; the first is a skip, the second is a broken control.
        """
        for base in (self.root, *self.root.resolve().parents):
            control = base / LOG_SCHEMA_CONTROL_PATH
            if not control.exists():
                control = base / LOG_SCHEMA_CONTROL_PATH.name
                if base.name != "controls" or not control.exists():
                    continue
            data = yaml.safe_load(control.read_text(encoding="utf-8")) or {}
            homes = data.get("normative_homes")
            return [str(h).lstrip("./") for h in homes] if isinstance(homes, list) else []
        return None

    def _load_client_scope_terms(self):
        policy = self.root / CLIENT_SCOPE_PATH
        if not policy.exists():
            return None
        data, _ = parse_frontmatter(policy.read_text(encoding="utf-8"))
        scope = (data or {}).get("client_scope") or {}
        terms = scope.get("forbidden_terms")
        return [str(t) for t in terms] if isinstance(terms, list) and terms else []


def _capped(details: list[str]) -> list[str]:
    if len(details) <= MAX_DETAILS:
        return details
    return details[:MAX_DETAILS] + [f"... and {len(details) - MAX_DETAILS} more"]


# --- detectors: (DocsRepo) -> Finding(ok, evidence, details) -------------------------------

def frontmatter_structure(repo: DocsRepo) -> Finding:
    violations: list[str] = []
    checked = 0
    for d in repo.governed:
        if d.fm_error is not None:
            violations.append(f"{d.rel}: {d.fm_error}")
            continue
        if d.fm_lines is None:
            # A document with no frontmatter at all was skipped here in silence, so the one
            # control whose job is to require frontmatter was the one control that could not
            # see its own worst case: 12 documents in core-docs - among them ADR-0030,
            # ADR-0031, RFC-0030 and PRD-0030 - were never validated by anything, and their
            # ids duplicated governed ones while DOC-0013 and DOC-0014 both reported green.
            # Templates are the one exemption; their frontmatter is placeholder by design.
            if is_template_doc(d.rel):
                continue
            violations.append(f"{d.rel}: no frontmatter block - every governed document opens "
                              f"with a fenced YAML frontmatter block")
            continue
        checked += 1
        for offset, line in enumerate(d.fm_lines, start=d.first_line):
            if "\t" in line:
                violations.append(f"{d.rel}:{offset}: tabs are not allowed in YAML frontmatter")
        decl = validate_related_key_declarations(d.fm_lines, d.rel, d.first_line or 2)
        if decl:
            violations.extend(decl)
            continue
        if d.parse_error is not None:
            violations.append(f"{d.rel}: {d.parse_error}")
            continue
        for key in REQUIRED_KEYS:
            if key not in d.data:
                violations.append(f"{d.rel}: missing required frontmatter key '{key}'")
        owner = d.data.get("owner")
        if "owner" in d.data and (not isinstance(owner, str) or not owner.strip()):
            violations.append(f"{d.rel}: frontmatter key 'owner' must be a non-empty string")
        for key in RELATED_LIST_KEYS:
            if key in d.data and not isinstance(d.data[key], list):
                violations.append(f"{d.rel}: frontmatter key '{key}' must be a list")
    if violations:
        return Finding(False, f"{len(violations)} frontmatter-structure violation(s) across "
                              f"{checked} documents", _capped(violations))
    return Finding(True, f"{checked} documents carry well-formed, complete frontmatter")


def owner_registered(repo: DocsRepo) -> Finding:
    violations = list(repo.owner_errors)
    checked = 0
    for d in repo.governed:
        if d.data is None or is_template_doc(d.rel):
            continue
        owner = d.data.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            continue
        checked += 1
        if owner.strip() not in repo.owner_slugs:
            violations.append(f"{d.rel}: owner '{owner.strip()}' is not listed in {OWNER_DIRECTORY_PATH}")
    if violations:
        return Finding(False, f"{len(violations)} owner-governance violation(s)", _capped(violations))
    return Finding(True, f"{checked} documents carry an owner registered in {OWNER_DIRECTORY_PATH} "
                         f"({len(repo.owner_slugs)} slugs)")


def controlled_vocabulary(repo: DocsRepo) -> Finding:
    violations: list[str] = []
    checked = 0

    def enum(d, key, allowed):
        if key in d.data and d.data[key] not in allowed:
            violations.append(f"{d.rel}: frontmatter key '{key}' must be one of {sorted(allowed)}")

    for d in repo.governed:
        if d.data is None:
            continue
        checked += 1
        if "status" in d.data and d.data["status"] not in VALID_STATUSES:
            violations.append(f"{d.rel}: frontmatter key 'status' must be one of {sorted(VALID_STATUSES)}")
        if "review_cycle" in d.data and d.data["review_cycle"] not in VALID_REVIEW_CYCLES:
            violations.append(f"{d.rel}: frontmatter key 'review_cycle' must be one of {sorted(VALID_REVIEW_CYCLES)}")
        if "last_reviewed" in d.data and not validate_date(d.data["last_reviewed"]):
            violations.append(f"{d.rel}: frontmatter key 'last_reviewed' must be an ISO date (YYYY-MM-DD)")
        enum(d, "doc_type", repo.doc_types)
        enum(d, "tier", VALID_TIERS)
        enum(d, "service_tier", VALID_SERVICE_TIERS)
        enum(d, "criticality", VALID_CRITICALITY)
        if is_service_scorecard(d.rel):
            if d.data.get("doc_type") != "scorecard":
                violations.append(f"{d.rel}: service scorecards must set doc_type to 'scorecard'")
            for req in ("service_tier", "criticality"):
                if req not in d.data:
                    violations.append(f"{d.rel}: service scorecards must include frontmatter key '{req}'")
    if violations:
        return Finding(False, f"{len(violations)} controlled-vocabulary violation(s)", _capped(violations))
    return Finding(True, f"{checked} documents use only controlled vocabulary values")


def related_key_hygiene(repo: DocsRepo) -> Finding:
    violations: list[str] = []
    for d in repo.governed:
        if d.fm_lines is None:
            continue
        for key in RELATED_LIST_KEYS:
            violations.extend(validate_block_style_list(d.fm_lines, key, d.rel, d.first_line or 2))
    if violations:
        return Finding(False, f"{len(violations)} related_* list-style violation(s)", _capped(violations))
    return Finding(True, "related_* keys use canonical block-list style")


def related_docs_links(repo: DocsRepo) -> Finding:
    violations: list[str] = []
    checked = 0
    for d in repo.governed:
        if d.data is None:
            continue
        related = {k: [i for i in d.data.get(k, []) if isinstance(i, str) and i.strip()]
                   for k in RELATED_LIST_KEYS}
        if not any(related.values()):
            continue
        checked += 1
        links = extract_related_doc_links(d.text)
        if links is None:
            violations.append(f"{d.rel}: missing 'Related Docs' section for non-empty related_* frontmatter")
            continue
        for key, values in related.items():
            for value in values:
                if not has_related_doc_link(value, links):
                    violations.append(f"{d.rel}: Related Docs section missing link for '{value}' "
                                      f"from frontmatter key '{key}'")
    if violations:
        return Finding(False, f"{len(violations)} missing-related-link violation(s)", _capped(violations))
    return Finding(True, f"{checked} documents with related_* links resolve to their Related Docs section")


def supersession_integrity(repo: DocsRepo) -> Finding:
    records: dict[str, dict] = {}
    violations: list[str] = []
    for d in repo.governed:
        if d.rel.parts[0] not in DECISION_FOLDERS:
            continue
        decision_id = _decision_id(d.path)
        if decision_id is None or d.data is None:
            continue

        def ids(key):
            value = d.data.get(key)
            if value is None:
                return []
            if not isinstance(value, list):
                violations.append(f"{d.rel}: frontmatter key '{key}' must be a list")
                return []
            return [str(i).strip().upper() for i in value if isinstance(i, str)]

        records[decision_id] = {"rel": d.rel, "status": str(d.data.get("status", "")).strip(),
                                "supersedes": ids("supersedes"), "superseded_by": ids("superseded_by")}
    for did, rec in records.items():
        rel = rec["rel"]
        if rec["status"] == "superseded" and not rec["superseded_by"]:
            violations.append(f"{rel}: status is 'superseded' but no 'superseded_by' is declared")
        if rec["superseded_by"] and rec["status"] != "superseded":
            violations.append(f"{rel}: declares 'superseded_by' but status is '{rec['status']}'")
        for target in rec["superseded_by"]:
            if target not in records:
                violations.append(f"{rel}: 'superseded_by' references unknown decision '{target}'")
            elif did not in records[target]["supersedes"]:
                violations.append(f"{rel}: 'superseded_by: {target}' not reciprocated by '{target}.supersedes'")
        for target in rec["supersedes"]:
            if target not in records:
                violations.append(f"{rel}: 'supersedes' references unknown decision '{target}'")
            elif did not in records[target]["superseded_by"]:
                violations.append(f"{rel}: 'supersedes: {target}' not reciprocated by '{target}.superseded_by'")
    if violations:
        return Finding(False, f"{len(violations)} supersession-integrity violation(s)", _capped(violations))
    return Finding(True, f"{len(records)} decision records have consistent supersession chains")


def freshness_sla(repo: DocsRepo) -> Finding:
    stale: list[str] = []
    invalid: list[str] = []
    checked = exempt = 0
    for d in repo.governed:
        if d.data is None:
            continue
        cycle = str(d.data.get("review_cycle", "")).strip()
        reviewed = as_date(d.data.get("last_reviewed"))
        if cycle == "event-driven":
            exempt += 1
            continue
        if cycle not in CYCLE_DAYS:
            continue
        if reviewed is None:
            invalid.append(f"{d.rel}: missing/invalid 'last_reviewed' for SLA check")
            continue
        checked += 1
        due = reviewed + dt.timedelta(days=CYCLE_DAYS[cycle] + repo.grace)
        if repo.today > due:
            stale.append(f"{d.rel}: stale by {(repo.today - due).days}d (reviewed {reviewed}, {cycle}, due {due})")
    details = stale + invalid
    if details:
        return Finding(False, f"{len(stale)} stale, {len(invalid)} invalid ({checked} on calendar SLA, "
                              f"{exempt} event-driven exempt)", _capped(details))
    return Finding(True, f"{checked} documents within freshness SLA ({exempt} event-driven exempt)")


def client_scope_isolation(repo: DocsRepo) -> Finding:
    terms = repo.client_scope_terms or []
    patterns = [(t, re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)) for t in terms]
    policy_file = (repo.root / CLIENT_SCOPE_PATH).resolve()
    violations: list[str] = []
    scanned = 0
    for path in repo.all_md:
        if path.resolve() == policy_file:
            continue
        scanned += 1
        rel = path.relative_to(repo.root)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for term, pattern in patterns:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno}: forbidden sibling-client term '{term}': {line.strip()}")
    if violations:
        return Finding(False, f"{len(violations)} sibling-client reference(s) in {scanned} documents",
                       _capped(violations))
    return Finding(True, f"{scanned} documents scanned, 0 forbidden terms ({', '.join(terms)})")


def _prose_blocks(text: str) -> list:
    """Split Markdown into the units a rule can be stated in: paragraphs, list items, headings
    and table rows. Returns [(line_number, text)].

    Blocks, not lines, because these documents wrap prose - a rule split across two lines would
    be invisible to a line-at-a-time scan. Blocks, not whole documents, because a bullet list
    that names a field in one item and says "must" in the next is two statements, and joining
    them would invent a rule neither item makes. Fenced code and frontmatter are skipped: a
    `must` inside a YAML example is a value, not a claim about how services log.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break
    blocks: list = []
    buf: list = []
    buf_line = 0
    in_fence = False
    for offset in range(start, len(lines)):
        raw = lines[offset]
        lineno = offset + 1
        if FENCE_RE.match(raw):
            if buf:
                blocks.append((buf_line, " ".join(buf)))
                buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = raw.strip()
        breaks_block = (not stripped or stripped.startswith(("#", "|", ">"))
                        or LIST_ITEM_RE.match(raw) is not None)
        if breaks_block and buf:
            blocks.append((buf_line, " ".join(buf)))
            buf = []
        if not stripped:
            continue
        if stripped.startswith(("#", "|")):
            blocks.append((lineno, stripped))
            continue
        if not buf:
            buf_line = lineno
        buf.append(stripped)
    if buf:
        blocks.append((buf_line, " ".join(buf)))
    return blocks


# Clause boundaries that a subject and its imperative may not be on opposite sides of. Proximity
# alone is not enough, because "never" is as common in description as in prescription: in
#
#   a log line exists for every attempt - useful for reconstructing what happened, including
#   for attempts that never reach a handler-visible error
#
# "never" governs "reach", is 90 characters from "log line", and says nothing about logging. Every
# character between them is on the far side of a dash and a subordinating "including". Splitting
# first turns that from a near miss into a non-match, and costs nothing real: a rule and the thing
# it governs are not normally separated by a full stop or a parenthesis.
LOG_CLAUSE_SPLIT_RE = re.compile(r"(?:[.;:!?]\s|\s[-\u2013\u2014]\s|[()\[\]]|,\s+(?:including|such\s+as|e\.g\.|i\.e\.|which|useful)\b)")


def _logging_rule_in(sentence: str):
    """Return the matched logging subject when the sentence states a rule about it, else None."""
    text = AUDIT_LOG_RE.sub("auditrecord", sentence)
    if not LOG_IMPERATIVE_RE.search(text):
        return None
    has_context = bool(LOG_CONTEXT_RE.search(text))

    # Walk clauses rather than the whole block, so an imperative can only bind to a subject it
    # shares one with. The window still applies inside a clause; this only stops it reaching
    # across a boundary.
    for clause in LOG_CLAUSE_SPLIT_RE.split(text):
        if not clause:
            continue
        imperatives = [m.start() for m in LOG_IMPERATIVE_RE.finditer(clause)]
        if not imperatives:
            continue
        subjects = list(LOG_SUBJECT_A_RE.finditer(clause))
        if has_context:
            subjects += list(LOG_SUBJECT_B_RE.finditer(clause))
        for subject in subjects:
            if any(abs(pos - subject.start()) <= LOG_CLAUSE_WINDOW for pos in imperatives):
                return subject.group(0)
    return None


def logging_rule_restatement(repo: DocsRepo) -> Finding:
    homes = repo.log_schema_homes
    if homes is None:
        return Finding(True, f"{LOG_SCHEMA_CONTROL_PATH} is not present in this checkout; "
                             f"the normative homes it declares cannot be resolved, so no "
                             f"document is claimed to be outside them")
    if not homes:
        return Finding(False, f"{LOG_SCHEMA_CONTROL_PATH} declares no normative_homes",
                       ["log-schema.yaml: normative_homes is empty or missing - with no declared "
                        "home, every document is outside it and the control cannot be evaluated"])
    violations: list[str] = []
    scanned = 0
    for path in repo.all_md:
        rel = path.relative_to(repo.root)
        posix = path.resolve().as_posix()
        if any(posix.endswith(home) for home in homes) or is_template_doc(rel):
            continue
        scanned += 1
        for lineno, block in _prose_blocks(path.read_text(encoding="utf-8", errors="ignore")):
            if LOG_CITATION_RE.search(block):
                continue
            for sentence in SENTENCE_SPLIT_RE.split(block):
                hit = _logging_rule_in(sentence)
                if hit is None:
                    continue
                violations.append(
                    f"{rel}:{lineno}: normative statement about '{hit}' outside its declared "
                    f"home and without a citation: {sentence.strip()[:160]}")
    if violations:
        return Finding(False, f"{len(violations)} restated logging rule(s) across {scanned} "
                              f"documents outside {len(homes)} declared home(s)",
                       _capped(violations))
    return Finding(True, f"{scanned} documents outside the {len(homes)} declared home(s) cite the "
                         f"logging rules rather than restating them")


def owner_registry_usage(repo: DocsRepo) -> Finding:
    used = {d.data["owner"].strip() for d in repo.governed
            if d.data and isinstance(d.data.get("owner"), str) and d.data["owner"].strip()
            and not is_template_doc(d.rel)}
    unused = sorted(repo.owner_slugs - used)
    if unused:
        return Finding(False, f"{len(unused)} registered owner slug(s) are unused",
                       [f"unused owner slug: {s}" for s in unused])
    return Finding(True, f"all {len(repo.owner_slugs)} registered owner slugs are in use")


def doc_root_naming(repo: "DocsRepo") -> "Finding":
    details = []
    for rel in sorted(repo.missing_declared_roots):
        details.append(f"declared doc root does not exist: {rel}/ "
                       "(a typo here governs nothing and reports nothing)")
    for name, plural in SINGULAR_FOLDER_FIXES.items():
        for found in sorted(repo.root.rglob(name)):
            if not found.is_dir() or ".git" in found.parts:
                continue
            details.append(f"{found.relative_to(repo.root)}/: document folders are plural - "
                           f"rename to '{plural}'")
    if details:
        return Finding(False, f"{len(details)} doc-root naming/resolution problem(s)", details)
    return Finding(True, f"all {len(repo.doc_roots)} doc roots resolve and use plural names")


def source_path_citations(repo: "DocsRepo") -> "Finding":
    """DOC-0012: a prose citation of source code must be verifiable, or it is not evidence.

    Two failure modes, both observed in the contracts doc set:

      * `pkg/thing.go:176` - a line-number citation. It is stale the next time anyone
        edits above line 176, and nothing can ever detect that. Cite the file (and the
        symbol, in prose) instead.
      * `platform/apicontract/schema_gen.go` - a path that does not exist, because the
        code was renamed or refactored and the doc was not. Repo-local paths are
        resolved here; paths into other repositories are only resolved when the caller
        supplies --source-root, so this control never fails on an unavailable checkout.
    """
    line_cites: list[str] = []
    missing: list[str] = []
    checked = unresolved = 0

    for doc in repo.governed:
        for lineno, line in enumerate(doc.text.splitlines(), 1):
            for raw in CODE_CITATION_RE.findall(line):
                token = raw.strip()
                bare, _, suffix = token.partition(":")
                if suffix and suffix.isdigit():
                    line_cites.append(f"{doc.rel}:{lineno}: `{token}` cites a line number; "
                                      "cite the file and name the symbol in prose")
                    continue
                if not bare or bare.endswith("/"):
                    continue
                checked += 1
                resolved = repo.resolve_source_path(bare)
                if resolved is None:
                    unresolved += 1
                elif not resolved.exists():
                    missing.append(f"{doc.rel}:{lineno}: `{bare}` does not exist")

    details = _capped(sorted(set(line_cites)) + sorted(set(missing)))
    if line_cites or missing:
        return Finding(False, f"{len(set(line_cites))} line-number citation(s) and "
                              f"{len(set(missing))} dangling source path(s)", details)
    note = f" ({unresolved} external path(s) not resolvable without --source-root)" if unresolved else ""
    return Finding(True, f"{checked} source-path citation(s) resolve; no line-number citations{note}")


def _numbered_doc_candidates(repo: "DocsRepo"):
    """Yield (doc, expected_prefix) for every file that must be a well-formed numbered document.

    A file inside adrs/ | rfcs/ | prds/ is subject to the naming rule when EITHER it already
    claims to be a numbered document (its name begins with ADR/RFC/PRD in any case, so
    `ADR0001.md`, `ADR-kafka.md` and `adr1.md` are all caught rather than ignored for not
    matching), OR it sits at the immediate top level of the folder, where a numbered document
    is the only thing that belongs. The second arm is what rejects `kafka.md`.

    Nested trees are deliberately exempt unless they claim a prefix: core-docs/rfcs carries
    per-domain subfolders (api/, events/, data/docker/...) holding supporting prose such as
    IMAGE_BUILD.md, which are not decision records and were never meant to be numbered.
    """
    for doc in repo.governed:
        parts = doc.rel.parts
        folder = parts[0] if parts else ""
        expected = NUMBERED_DOC_FOLDERS.get(folder)
        if expected is None or is_template_doc(doc.rel):
            continue
        if NON_DOCUMENT_STEMS.match(doc.path.stem):
            continue
        claims_prefix = re.match(r"^(ADR|RFC|PRD)", doc.path.stem, re.IGNORECASE) is not None
        if claims_prefix or len(parts) == 2:
            yield doc, expected


def document_id_convention(repo: "DocsRepo") -> "Finding":
    """DOC-0013: numbered documents are `PREFIX-NNNN-kebab-slug.md`, filed under their prefix.

    Four failure modes, each of which breaks something concrete downstream:

      * wrong zero-padding (`ADR-12-...`) - the id is then spelled differently everywhere it
        is cited, so reference checking and grep both miss it;
      * a missing or malformed number (`ADR-kafka.md`, `adr1.md`) - the document has no id at
        all, so it cannot be cited, indexed or superseded;
      * a slug that is not kebab-case (`ADR-0002-Kafka.md`, doubled or trailing hyphens) -
        filenames become case-sensitivity hazards across macOS and Linux checkouts;
      * a prefix that disagrees with its folder (`RFC-0001-...` under adrs/) - a filing error
        that silently removes the document from its type's index.
    """
    details: list[str] = []
    checked = 0
    for doc, expected in _numbered_doc_candidates(repo):
        name = doc.path.name
        m = DOCUMENT_FILENAME_RE.match(name)
        if m is None:
            details.append(
                f"{doc.rel}: filename must be '{expected}-NNNN-kebab-case-slug.md' "
                f"({ID_WIDTH}-digit zero-padded number, lowercase slug with single hyphens)")
            continue
        checked += 1
        if m.group("prefix") != expected:
            details.append(
                f"{doc.rel}: is a {m.group('prefix')} but is filed under {doc.rel.parts[0]}/, "
                f"which holds {expected} documents - move it to the folder for its type")
    if details:
        return Finding(False, f"{len(details)} document(s) violate the id/filename convention",
                       details[:MAX_DETAILS])
    return Finding(True, f"all {checked} numbered document(s) use "
                         f"PREFIX-{'N' * ID_WIDTH}-kebab-slug.md and are filed under their type")


def document_id_frontmatter(repo: "DocsRepo") -> "Finding":
    """DOC-0017: a numbered document declares an `id:` that matches its filename.

    The identifier is derivable from the filename, so this key is deliberately redundant - and
    the redundancy is the control. `id:` is what the generated catalog and the docs site key
    entries on, rather than re-deriving them by parsing paths, so a stale value publishes the
    right document under the wrong identifier. That is exactly what a copied template produces:
    duplicate an ADR to start the next one, rename the file, and the inherited `id:` still names
    the document it was copied from. Only a numbered document carries this key; standards,
    runbooks and hubs have no identifier to declare.
    """
    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    for doc, _ in _numbered_doc_candidates(repo):
        m = DOCUMENT_FILENAME_RE.match(doc.path.name)
        if m is None:
            continue  # shape is DOC-0013's finding
        expected = f"{m.group('prefix')}-{m.group('number')}"
        if not isinstance(doc.data, dict):
            continue  # absent/invalid frontmatter is DOC-0001's finding
        checked += 1
        declared = doc.data.get("id")
        if declared is None:
            missing.append(f"{doc.rel}: numbered document must declare `id: {expected}` in "
                           f"frontmatter")
        elif str(declared).strip() != expected:
            mismatched.append(f"{doc.rel}: declares `id: {declared}` but its filename says "
                              f"{expected} - one of the two is wrong, and a copied template is "
                              f"the usual cause")
    details = mismatched + missing
    if details:
        return Finding(False, f"{len(mismatched)} mismatched and {len(missing)} missing "
                              f"document id declaration(s)", details[:MAX_DETAILS])
    return Finding(True, f"all {checked} numbered document(s) declare an id matching their filename")


def duplicate_document_ids(repo: "DocsRepo") -> "Finding":
    """DOC-0014: a document id is unique within its namespace.

    Scoped to this repository on purpose. Each documentation namespace (core-docs, and each
    client hub) allocates its own numbers, so core-docs ADR-0001 and a client's ADR-0001 are
    both legitimate and are NOT duplicates; they are disambiguated at the citation site by the
    namespace qualifier, not by being globally unique. Two files claiming the same id inside
    one namespace is the real defect: every citation of that id becomes ambiguous, and the
    generated catalog silently keeps whichever the walk reached last.
    """
    by_id: dict[str, list[str]] = {}
    for doc, _ in _numbered_doc_candidates(repo):
        m = DOCUMENT_FILENAME_RE.match(doc.path.name)
        if m is None:
            continue  # shape is DOC-0013's finding; do not double-report it here
        by_id.setdefault(f"{m.group('prefix')}-{m.group('number')}", []).append(str(doc.rel))
    details = [f"{doc_id} is claimed by {len(paths)} files: {', '.join(sorted(paths))}"
               for doc_id, paths in sorted(by_id.items()) if len(paths) > 1]
    if details:
        return Finding(False, f"{len(details)} duplicate document id(s) in this namespace",
                       details[:MAX_DETAILS])
    return Finding(True, f"all {len(by_id)} document id(s) are unique within this namespace")


# --- citation resolution (DOC-0015 / DOC-0016), the executable form of ADR-0084 -------------

@dataclass
class Citation:
    doc_id: str          # ADR-0069
    namespace: str       # peer namespace name, or None when the citation is bare (== local)
    qualifier: str       # the qualifier text as written, or "" when bare
    offset: int
    line: int
    linked: bool         # the citation sits inside the text of a markdown link


def _mask_uncitable(text: str) -> str:
    """Blank the regions where an identifier is not a citation, preserving offsets.

    Three regions are masked rather than removed, so reported line numbers stay true:

      * frontmatter - `related_adrs:` values are structured data governed by DOC-0004/DOC-0005,
        not prose citations;
      * fenced code blocks - a sample envelope or YAML snippet may name ids illustratively;
      * markdown link *targets* - `](../../core-docs/adrs/ADR-0069-....md)` contains an id that
        is a path component. Without masking it, every correctly-linked cross-namespace citation
        would also read as a bare local citation and fail;
      * inline code spans - a backticked `ADR-0001` names a *string* rather than citing a
        document. Standards and templates write `PREFIX-NNNN` examples this way, so treating
        them as citations reports the format documentation as a dangling reference.
    """
    out = list(text)

    def blank(start: int, end: int) -> None:
        for i in range(start, min(end, len(out))):
            if out[i] != "\n":
                out[i] = " "

    lines = text.splitlines(keepends=True)
    pos = 0
    if lines and lines[0].strip() == "---":
        pos = len(lines[0])
        for line in lines[1:]:
            end = pos + len(line)
            blank(pos, end)
            pos = end
            if line.strip() == "---":
                break
    in_fence = False
    cursor = 0
    for line in lines:
        end = cursor + len(line)
        if line.lstrip().startswith(("```", "~~~")):
            blank(cursor, end)
            in_fence = not in_fence
        elif in_fence:
            blank(cursor, end)
        cursor = end
    for m in MARKDOWN_LINK_RE.finditer(text):
        blank(m.start(2), m.end(2))
    for m in INLINE_CODE_RE.finditer(text):
        blank(m.start(), m.end())
    return "".join(out)


def _link_text_spans(text: str):
    return [(m.start(1), m.end(1)) for m in MARKDOWN_LINK_RE.finditer(text)]


def _citations(repo: "DocsRepo", doc: "DocFile"):
    """Yield every document-id citation in a document's prose.

    A citation is cross-namespace when the token immediately before the id is a registered
    qualifier for a namespace (or one of that namespace's recognised aliases, which is how the
    two spellings already in the wild - `core ADR-0045` and `core-docs ADR-0054` - are detected
    as non-canonical rather than silently read as prose). Anything else is a bare citation,
    which by ADR-0084 means *this* namespace.
    """
    masked = _mask_uncitable(doc.text)
    spans = _link_text_spans(doc.text)
    for m in CITATION_RE.finditer(masked):
        token = (m.group("qual") or "").strip()
        entry = repo.qualifier_aliases.get(token.lower()) if token else None
        start = m.start("id")
        yield Citation(
            doc_id=m.group("id"),
            namespace=entry["name"] if entry else None,
            qualifier=token if entry else "",
            offset=start,
            line=masked.count("\n", 0, start) + 1,
            linked=any(a <= start < b for a, b in spans),
        )


def _slugify_heading(text: str) -> str:
    """GitHub's heading-anchor algorithm: strip formatting, lowercase, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # links keep their text
    text = re.sub(r"[*_~]", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def internal_link_resolution(repo: "DocsRepo") -> "Finding":
    """DOC-0019: a relative link inside the docs tree resolves, anchor included.

    Only repository-internal links are checked. External URLs are deliberately out of scope: they
    fail for reasons that have nothing to do with the change under review - rate limits,
    transient outages, sites that block CI egress - and a gate that goes red for reasons a PR
    author cannot fix is a gate people learn to re-run rather than read.

    Anchors are resolved against the target document's headings, because a link to a section that
    was renamed is the most common form of documentation rot and the one least visible to a
    reviewer: the link still works, it just silently lands at the top of the page.
    """
    missing_file: list[str] = []
    missing_anchor: list[str] = []
    checked = 0
    heading_cache: dict = {}

    def anchors_for(path: Path) -> set:
        if path not in heading_cache:
            slugs, counts = set(), Counter()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                heading_cache[path] = set()
                return heading_cache[path]
            for line in text.splitlines():
                m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
                if m is None:
                    continue
                base = _slugify_heading(m.group(2))
                if not base:
                    continue
                # GitHub disambiguates repeated headings by appending -1, -2, ...
                slugs.add(base if not counts[base] else f"{base}-{counts[base]}")
                counts[base] += 1
            heading_cache[path] = slugs
        return heading_cache[path]

    for doc in repo.governed:
        for m in MARKDOWN_LINK_RE.finditer(doc.text):
            target = m.group(2).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#", "<")):
                continue
            if "{{" in target:
                continue  # a template placeholder, resolved when the template is instantiated
            target = target.split()[0]  # drop a trailing "title"
            rel_path, _, anchor = target.partition("#")
            if not rel_path:
                continue
            resolved = (doc.path.parent / rel_path).resolve()
            checked += 1
            if not resolved.exists():
                missing_file.append(f"{doc.rel}: link target does not exist: {rel_path}")
                continue
            if anchor and resolved.suffix == ".md":
                if anchor.lower() not in anchors_for(resolved):
                    missing_anchor.append(
                        f"{doc.rel}: `{rel_path}` exists but has no heading matching "
                        f"anchor #{anchor}")
    details = missing_file + missing_anchor
    if details:
        return Finding(False, f"{len(missing_file)} dangling internal link(s) and "
                              f"{len(missing_anchor)} unresolvable anchor(s)", details[:MAX_DETAILS])
    return Finding(True, f"all {checked} internal link(s) resolve, anchors included")


def sequential_id_allocation(repo: "DocsRepo") -> "Finding":
    """DOC-0018: a new document takes the next unallocated identifier in its namespace.

    Checked as an ALLOCATION rule, never as a retroactive audit of the whole sequence. The
    distinction matters: core-docs has a legitimate historical gap at ADR-0047, and a rule that
    demanded a contiguous sequence would fail every unrelated documentation change until someone
    back-filled a decision that was never made. A gate that cannot go green teaches people to
    ignore it.

    So the rule is about the *top* of the sequence, which is the only part a new document can get
    wrong: the highest allocated identifier must not leave a gap above the previous high-water
    mark that is neither an existing document nor a declared reservation. Skipping ahead - taking
    ADR-0090 when ADR-0085 is free - is what makes two people's concurrent ADRs collide later,
    and it is invisible the moment it happens.
    """
    by_prefix: dict = {}
    transitional: dict = {}
    for doc, _ in _numbered_doc_candidates(repo):
        m = DOCUMENT_FILENAME_RE.match(doc.path.name)
        if m is None:
            continue
        number = int(m.group("number"))
        bucket = transitional if number >= TRANSITIONAL_BAND_FLOOR else by_prefix
        bucket.setdefault(m.group("prefix"), set()).add(number)
    reserved: dict = {}
    for rid in repo.reserved_ids:
        prefix, _, num = rid.partition("-")
        if num.isdigit():
            reserved.setdefault(prefix, set()).add(int(num))

    details: list[str] = []
    for prefix, numbers in sorted(by_prefix.items()):
        known = numbers | reserved.get(prefix, set())
        highest = max(numbers)
        # Every number below the high-water mark must be an existing document or a declared
        # reservation/gap. Anything else is a slot that was skipped without being recorded.
        undeclared = sorted(n for n in range(1, highest + 1) if n not in known)
        if undeclared:
            shown = ", ".join(f"{prefix}-{n:04d}" for n in undeclared[:10])
            more = f" (+{len(undeclared) - 10} more)" if len(undeclared) > 10 else ""
            details.append(
                f"{prefix}: {len(undeclared)} identifier(s) below the highest allocated "
                f"{prefix}-{highest:04d} are neither allocated nor declared: {shown}{more}. "
                f"Either the number was skipped - take the lowest free one instead - or the gap "
                f"is intentional and belongs in {RESERVED_IDS_PATH} under known_gaps")
    if details:
        return Finding(False, f"{len(details)} identifier sequence(s) contain undeclared gaps",
                       details[:MAX_DETAILS])
    summary = ", ".join(f"{p}-{max(n):04d}" for p, n in sorted(by_prefix.items())) or "none"
    band = ""
    if transitional:
        count = sum(len(v) for v in transitional.values())
        band = f"; {count} transitional document(s) in the {TRANSITIONAL_BAND_FLOOR} band, " \
               f"outside the sequence"
    return Finding(True, f"identifier sequences are contiguous or declared "
                         f"(high-water: {summary}{band})")


def document_reference_resolution(repo: "DocsRepo") -> "Finding":
    """DOC-0015: every cited document identifier resolves, in the namespace the citation names.

    A bare id resolves against this namespace; a qualified id against the named peer. This is
    the control that could not be written before ADR-0084, because `ADR-0006` names a real
    document in more than one namespace and a checker had no way to know which was meant.

    A peer namespace that is not present in this run is reported as skipped, never as passing -
    the same rule DOC-0012 applies to source paths in unavailable repositories.
    """
    if not repo.namespace:
        return Finding(False, "this repository does not declare which namespace it is",
                       ["docs-governance.yaml: add `namespace: <name>` naming this repository's "
                        "entry in controls/doc-namespaces.yaml (ADR-0084 §1)"])
    dangling: list[str] = []
    skipped: set[str] = set()
    checked = 0
    for doc in repo.governed:
        for cite in _citations(repo, doc):
            # A qualified citation of this repository's OWN namespace is still a local citation.
            # core-docs prose legitimately writes "core-docs ADR-0035" when describing the
            # namespace itself; resolving that against a peer checkout would look for core-docs
            # inside core-docs and report it as an unavailable peer.
            if cite.namespace is None or cite.namespace == repo.namespace:
                resolved = (cite.doc_id in repo.local_ids
                            or cite.doc_id in repo.reserved_ids)
                if not resolved and EXTERNAL_STANDARD_RE.match(cite.doc_id):
                    continue  # an IETF standard, not a platform document
                checked += 1
                if not resolved:
                    dangling.append(
                        f"{doc.rel}:{cite.line}: `{cite.doc_id}` does not exist in this namespace "
                        f"({repo.namespace}). If it belongs to another namespace, qualify it "
                        f"(e.g. `Core {cite.doc_id}`); if it is a deliberate forward reference, "
                        f"declare it in {RESERVED_IDS_PATH}")
                continue
            peer_ids = repo.peer_ids(cite.namespace)
            if peer_ids is None:
                skipped.add(cite.namespace)
                continue
            checked += 1
            if cite.doc_id not in peer_ids:
                dangling.append(f"{doc.rel}:{cite.line}: `{cite.qualifier} {cite.doc_id}` does not "
                                f"exist in namespace {cite.namespace}")
    note = f" ({len(skipped)} peer namespace(s) not present in this run: "\
           f"{', '.join(sorted(skipped))})" if skipped else ""
    if dangling:
        return Finding(False, f"{len(dangling)} unresolvable document citation(s){note}",
                       dangling[:MAX_DETAILS])
    return Finding(True, f"all {checked} document citation(s) resolve{note}")


def cross_namespace_citation_form(repo: "DocsRepo") -> "Finding":
    """DOC-0016: a cross-namespace citation uses the registered qualifier and links on first use.

    Two failure modes, both live before ADR-0084. The qualifier was spelled three ways (`Core`,
    `core`, `core-docs`), so no parser could rely on it; and a qualifier is only a claim about
    which namespace is meant, so the first mention in a document must also be a resolvable link
    that makes the claim checkable on disk. Later mentions need no link - the document has
    already resolved the id once, and requiring it everywhere makes prose unreadable without
    adding any checking power.
    """
    if not repo.namespace:
        return Finding(False, "this repository does not declare which namespace it is", [])
    wrong_spelling: list[str] = []
    unlinked: list[str] = []
    seen: set[tuple] = set()
    checked = 0
    for doc in repo.governed:
        for cite in sorted(_citations(repo, doc), key=lambda c: c.offset):
            if cite.namespace is None or cite.namespace == repo.namespace:
                continue
            checked += 1
            canonical = repo.namespace_qualifier(cite.namespace)
            if cite.qualifier != canonical:
                wrong_spelling.append(
                    f"{doc.rel}:{cite.line}: `{cite.qualifier} {cite.doc_id}` must be written "
                    f"`{canonical} {cite.doc_id}` - the registered qualifier for "
                    f"{cite.namespace}")
            key = (doc.rel, cite.namespace, cite.doc_id)
            if key not in seen:
                seen.add(key)
                if not cite.linked:
                    unlinked.append(
                        f"{doc.rel}:{cite.line}: first mention of `{canonical} {cite.doc_id}` in "
                        f"this document must be a resolvable markdown link")
    details = wrong_spelling + unlinked
    if details:
        return Finding(False, f"{len(wrong_spelling)} non-canonical qualifier(s) and "
                              f"{len(unlinked)} unlinked first mention(s)", details[:MAX_DETAILS])
    return Finding(True, f"all {checked} cross-namespace citation(s) use the registered "
                         f"qualifier and link on first mention")


def decision_domain_vocabulary(repo: "DocsRepo") -> "Finding":
    """DOC-0023: every decision record declares a `domain`, drawn from this namespace's vocabulary.

    The vocabulary is namespace-scoped (`domain_vocabulary` in the repo's docs-governance.yaml)
    because the platform namespace groups decisions by architectural concern while a client hub
    groups them by business domain. One shared enum would fit neither.

    Two failures are reported, and the second is the reason this control exists. A missing `domain`
    leaves a decision out of every by-domain view, which is visible. A `domain` holding a value from
    a *different* namespace's vocabulary is invisible: it looks populated, filters silently match
    nothing, and it was already happening - hub ADRs carrying `security` where that namespace's
    taxonomy says `encryption`. Free-text frontmatter cannot tell those apart, which is why this is
    a closed list rather than a presence check.
    """
    if not repo.domain_vocabulary:
        return Finding(True, "no domain vocabulary declared for this namespace; DOC-0023 inactive")
    allowed = set(repo.domain_vocabulary)
    missing: list[str] = []
    unknown: list[str] = []
    checked = 0
    for doc in repo.governed:
        # A template is the shape of a decision, not one, so it has no domain to declare - the same
        # exemption DOC-0002, DOC-0014 and DOC-0017 already make.
        if is_template_doc(doc.rel):
            continue
        # A document whose frontmatter is absent or unparseable is DOC-0001's finding, not this
        # one's; reporting it twice buries the parse error under a domain error that cannot be
        # fixed until the parse error is.
        data = doc.data if isinstance(doc.data, dict) else {}
        declared = data.get("domain")
        # Required on decision records only. RFCs and PRDs in this fleet carry `tier`/`capability`
        # instead, and inventing a domain for them would be filling a field rather than using it.
        required = data.get("doc_type") == "adr"
        if declared is None or (isinstance(declared, str) and not declared.strip()):
            if required:
                missing.append(f"{doc.rel}: no `domain`; expected one of {sorted(allowed)}")
            continue
        checked += 1
        if not isinstance(declared, str):
            unknown.append(f"{doc.rel}: `domain` must be a single string, got {type(declared).__name__}")
        elif declared.strip() not in allowed:
            unknown.append(f"{doc.rel}: `domain: {declared.strip()}` is not in this namespace's "
                           f"vocabulary; expected one of {sorted(allowed)}")
    violations = missing + unknown
    if violations:
        return Finding(False, f"{len(missing)} decision(s) with no domain and {len(unknown)} "
                              f"outside the declared vocabulary", violations)
    return Finding(True, f"all {checked} declared domain(s) are in this namespace's vocabulary "
                         f"of {len(allowed)}")


def platform_concern_core_authority(repo: "DocsRepo") -> "Finding":
    """DOC-0024: a decision tagged with a platform concern declares which core decisions bind it.

    A client hub's `domain` vocabulary is a list of business bounded contexts, each with a directory
    describing what it owns. A few decisions genuinely do not fit that shape - cloud account
    topology, infrastructure repository layout, wire transport - because they provision or constrain
    the substrate every domain runs on rather than realizing a slice of the business model. Those
    are tagged from a second, closed tier (`platform_concern_domains`).

    That tier is the risk this control exists to contain. A category with no bounded context behind
    it is exactly where decisions get filed when nobody is sure where they go, and a hub decision
    about a platform-governed concern is precisely the kind that drifts from - or silently
    contradicts - the platform namespace. Both had already happened here: a hub ADR mandated a
    runtime HTTP fallback that the core contract ADR prohibits, and another restated core
    secret-management policy word for word while citing nothing.

    So membership in the tier carries an obligation: name the platform-namespace decisions that
    bind this one. An empty list is allowed and is the point - it is a claim on the record that no
    core decision governs this, which a reviewer can disagree with, rather than silence that cannot
    be distinguished from an oversight. Ids are namespace-unambiguous because the key names the
    namespace, which is what lets a hub declare `ADR-0002` here without colliding with its own.
    """
    concerns = set(repo.platform_concern_domains)
    if not concerns:
        return Finding(True, "no platform-concern domains declared for this namespace; "
                             "DOC-0024 inactive")
    violations: list[str] = []
    # A concern absent from the vocabulary can never be tagged, so the obligation hanging off it is
    # dead config that reads as an active rule.
    stray = sorted(concerns - set(repo.domain_vocabulary))
    if stray:
        violations.append(f"docs-governance.yaml: platform_concern_domains names {stray}, absent "
                          f"from domain_vocabulary and therefore never taggable")
    platform_ns = repo.platform_namespace()
    peer_ids = repo.peer_ids(platform_ns) if platform_ns else None
    declared = 0
    verified = 0
    for doc in repo.governed:
        if is_template_doc(doc.rel):
            continue
        data = doc.data if isinstance(doc.data, dict) else {}
        if data.get("doc_type") != "adr":
            continue
        domain = data.get("domain")
        if not isinstance(domain, str) or domain.strip() not in concerns:
            continue
        # Absent key and empty list are deliberately different: one is silence, the other a claim.
        if "core_authority" not in data:
            violations.append(
                f"{doc.rel}: `domain: {domain.strip()}` is a platform concern, so it must declare "
                f"`core_authority` naming the {platform_ns} decision(s) that bind it, or "
                f"`core_authority: []` to record that none does")
            continue
        declared += 1
        value = data.get("core_authority") or []
        if not isinstance(value, list):
            violations.append(f"{doc.rel}: `core_authority` must be a list of decision ids, got "
                              f"{type(value).__name__}")
            continue
        for entry in value:
            token = entry.strip() if isinstance(entry, str) else entry
            if not isinstance(token, str) or not DECISION_ID_RE.match(token):
                violations.append(f"{doc.rel}: `core_authority` entry {entry!r} is not a "
                                  f"four-digit decision id")
                continue
            if peer_ids is None:
                continue
            if token not in peer_ids:
                violations.append(f"{doc.rel}: `core_authority` names {token}, which does not "
                                  f"exist in namespace {platform_ns}")
                continue
            verified += 1
    if violations:
        return Finding(False, f"{len(violations)} platform-concern authority violation(s)",
                       violations[:MAX_DETAILS])
    note = "" if peer_ids is not None else \
        f" ({platform_ns or 'platform namespace'} not present in this run; ids unverified)"
    return Finding(True, f"all {declared} platform-concern decision(s) declare core_authority "
                         f"({verified} id(s) verified against {platform_ns}){note}")


DETECTORS = {
    "decision_domain_vocabulary": decision_domain_vocabulary,
    "platform_concern_core_authority": platform_concern_core_authority,
    "document_id_convention": document_id_convention,
    "document_id_frontmatter": document_id_frontmatter,
    "duplicate_document_ids": duplicate_document_ids,
    "sequential_id_allocation": sequential_id_allocation,
    "internal_link_resolution": internal_link_resolution,
    "document_reference_resolution": document_reference_resolution,
    "cross_namespace_citation_form": cross_namespace_citation_form,
    "doc_root_naming": doc_root_naming,
    "source_path_citations": source_path_citations,
    "frontmatter_structure": frontmatter_structure,
    "owner_registered": owner_registered,
    "controlled_vocabulary": controlled_vocabulary,
    "related_key_hygiene": related_key_hygiene,
    "related_docs_links": related_docs_links,
    "supersession_integrity": supersession_integrity,
    "freshness_sla": freshness_sla,
    "client_scope_isolation": client_scope_isolation,
    "logging_rule_restatement": logging_rule_restatement,
    "owner_registry_usage": owner_registry_usage,
}
# Domain-specific catalog schema+drift is delegated to the repo's own scripts/build_catalog.py
# --check, invoked by the docs-governance reusable workflow. Declared as policy, never
# duplicated here.
# Markdown quality checks that need a Node toolchain (mermaid-cli, markdownlint, cspell) are
# declared here as policy and executed by the reusable workflow. They are not reimplemented in
# Python: a second mermaid parser or spell checker would drift from the real one, and a check that
# disagrees with the tool developers run locally is worse than no check.
DELEGATED_DETECTORS = {
    "delegated_catalog_build": "delegated to the docs-governance workflow catalog step "
                               "(scripts/build_catalog.py --check); not evaluated by this checker",
    "delegated_mermaid_render": "delegated to the docs-governance workflow mermaid step "
                                "(@mermaid-js/mermaid-cli); not evaluated by this checker",
    "delegated_markdown_lint": "delegated to the docs-governance workflow markdownlint step "
                               "(markdownlint-cli2); not evaluated by this checker",
    "delegated_spellcheck": "delegated to the docs-governance workflow spellcheck step "
                            "(cspell); not evaluated by this checker",
}


def load_controls(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: invalid control catalog (expected a 'controls:' list)")
    errors: list[str] = []
    seen: set[str] = set()
    known = set(DETECTORS) | set(DELEGATED_DETECTORS)
    for i, c in enumerate(doc["controls"]):
        cid = c.get("id", f"#{i}")
        missing = [f for f in REQUIRED_FIELDS if not c.get(f)]
        if missing:
            errors.append(f"{cid}: missing required field(s) {missing}")
        # A control id is cited in scorecards, effective profiles, waiver records and PR
        # review comments. Until now its shape was unconstrained, so nothing stopped a new
        # catalog from introducing `DOC-1` or `doc-0001` alongside `DOC-0001` - three spellings
        # of one control, none of which grep finds together.
        if c.get("id") and not CONTROL_ID_RE.match(str(c["id"])):
            errors.append(f"{cid}: control id must be PREFIX-{'N' * ID_WIDTH} with an uppercase "
                          f"prefix and exactly {ID_WIDTH} digits (e.g. DOC-0001)")
        if c.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity {c.get('severity')!r} not in {sorted(VALID_SEVERITY)}")
        if c.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status {c.get('status')!r} not in {sorted(VALID_STATUS)}")
        if c.get("scope") not in VALID_SCOPE:
            errors.append(f"{cid}: scope {c.get('scope')!r} not in {sorted(VALID_SCOPE)}")
        if c.get("applies_when") not in VALID_APPLIES:
            errors.append(f"{cid}: applies_when {c.get('applies_when')!r} not in {sorted(VALID_APPLIES)}")
        if c.get("detector") not in known:
            errors.append(f"{cid}: unknown detector {c.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for e in errors:
            print(f"::error::docs-governance catalog invalid: {e}")
        raise SystemExit(1)
    return doc


def _applicable(control: dict, repo: DocsRepo):
    when = control["applies_when"]
    if when == "client-scope" and repo.client_scope_terms is None:
        return False, "no governance/CLIENT_SCOPE.md; single-client isolation not applicable"
    if when == "catalog" and not repo.has_catalog:
        return False, "no catalog/ directory; catalog governance not applicable"
    return True, ""


def evaluate(repo: DocsRepo, controls: list[dict]) -> list[dict]:
    results = []
    for c in controls:
        rec = {"control": c["id"], "title": c["title"], "severity": c["severity"],
               "scope": c["scope"], "applies_when": c["applies_when"], "owner": c["owner"],
               "status": c["status"], "result": None, "evidence": "", "details": [], "remediation": ""}
        applicable, reason = _applicable(c, repo)
        if c["status"] != "active":
            rec.update(result="skipped", evidence=f"lifecycle status={c['status']} (not evaluated)")
        elif not applicable:
            rec.update(result="skipped", evidence=reason)
        elif c["detector"] in DELEGATED_DETECTORS:
            rec.update(result="skipped", evidence=DELEGATED_DETECTORS[c["detector"]])
        else:
            f = DETECTORS[c["detector"]](repo)
            rec.update(result="pass" if f.ok else "fail", evidence=f.evidence, details=f.details,
                       remediation="" if f.ok else " ".join(str(c["remediation"]).split()))
        results.append(rec)
    return results


def is_enforced(rec: dict, threshold: int) -> bool:
    if rec["result"] == "error":
        return True
    return rec["result"] == "fail" and SEVERITY_ORDER.get(rec["severity"], 2) >= threshold


def governance_score(results) -> dict:
    """A severity-weighted pass rate over the controls this run actually evaluated.

    Weighted rather than a flat ratio, because a flat ratio prices a stale review date the same
    as an unresolvable citation, and a score that moves the same amount for both is not a signal
    anyone can act on. Skipped controls are excluded from numerator and denominator alike - a
    repository with no client-scope policy should neither be credited nor penalised for a control
    that does not apply to it, and folding skips into the denominator would cap it below 100 with
    no way to ever earn the remainder.
    """
    weighted = [(SEVERITY_ORDER.get(r["severity"], 2), r["result"] == "pass")
                for r in results if r["result"] in ("pass", "fail", "error")]
    possible = sum(w for w, _ in weighted)
    earned = sum(w for w, ok in weighted if ok)
    return {
        "score": round(100 * earned / possible) if possible else 100,
        "weighting": "severity (critical 3, major 2, minor 1); skipped controls excluded",
        "weight_earned": earned,
        "weight_possible": possible,
    }


def build_report(repo: DocsRepo, results, ssot, fail_on, threshold) -> dict:
    enforced = [r for r in results if is_enforced(r, threshold)]
    return {
        "governance_score": governance_score(results),
        "root": str(repo.root), "policy_ssot": ssot, "fail_on": fail_on,
        "capabilities": {"catalog": repo.has_catalog, "client_scope": repo.client_scope_terms is not None},
        "documents_scanned": len(repo.governed),
        "controls_total": len(results),
        "evaluated": sum(1 for r in results if r["result"] in ("pass", "fail")),
        "passed": sum(1 for r in results if r["result"] == "pass"),
        "failed": sum(1 for r in results if r["result"] in ("fail", "error")),
        "skipped": sum(1 for r in results if r["result"] == "skipped"),
        "enforced_failures": len(enforced), "ok": not enforced, "results": results,
    }


def render_text(report: dict, threshold: int) -> None:
    results = report["results"]
    print("::group::docs-governance controls (evidence)")
    for r in results:
        if r["result"] == "skipped":
            print(f"[--] {r['control']} skipped: {r['evidence']}")
            continue
        mark = "ok" if r["result"] == "pass" else "XX"
        print(f"[{mark}] {r['control']} [{r['severity']}/{r['scope']}/{r['owner']}] {r['title']}: {r['evidence']}")
    print("::endgroup::")

    for r in results:
        if r["result"] not in ("fail", "error"):
            continue
        enforced = is_enforced(r, threshold)
        head = "::error::" if enforced else "::warning::"
        suffix = "" if enforced else " (advisory; below fail-on)"
        print(f"{head}[{r['control']}][{r['severity']}] {r['title']} - {r['evidence']}{suffix}")
        for line in r["details"]:
            print(f"    - {line}")
        if enforced and r["remediation"]:
            print(f"    fix: {r['remediation']}")

    ssot = ", ".join(report["policy_ssot"])
    caps = ", ".join(k for k, v in report["capabilities"].items() if v) or "none"
    gs = report["governance_score"]
    print(f"Governance score: {gs['score']}/100 "
          f"({gs['weight_earned']}/{gs['weight_possible']} weighted; {gs['weighting']}).")
    if report["ok"]:
        print(f"docs-governance: OK - {report['passed']}/{report['evaluated']} controls upheld "
              f"({report['skipped']} skipped) across {report['documents_scanned']} documents "
              f"in {report['root']} [capabilities: {caps}].")
        print(f"Policy SSOT: {ssot} (fail-on={report['fail_on']}).")
    else:
        print(f"docs-governance: FAILED - {report['enforced_failures']} enforced violation(s), "
              f"{report['passed']}/{report['evaluated']} controls passing in {report['root']}.")
        print(f"Policy SSOT: {ssot}. Executable form of ADR-0081 (fail-on={report['fail_on']}).")


# --- generated docs (single source: the catalog) -------------------------------------------

def render_docs(doc: dict) -> str:
    lines = [
        DOCS_BEGIN, "",
        "_Generated from `controls/docs-governance.yaml` by `scripts/check-docs-governance.py "
        "--write-docs` — do not edit by hand._", "",
        "| Control | Policy | Severity | Scope | Applies when | Owner | Status |",
        "| ------- | ------ | -------- | ----- | ------------ | ----- | ------ |",
    ]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        lines.append(f"| {c['id']} | {policy} | {c['severity']} | {c['scope']} | "
                     f"{c['applies_when']} | {c['owner']} | {c['status']} |")
    lines += ["", DOCS_END]
    return "\n".join(lines)


def _extract_block(text: str):
    if DOCS_BEGIN in text and DOCS_END in text:
        return DOCS_BEGIN + text.split(DOCS_BEGIN, 1)[1].split(DOCS_END, 1)[0] + DOCS_END
    return None


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        print(f"::error::{path}: markers not found. Add these two lines where the table should go:\n"
              f"  {DOCS_BEGIN}\n  {DOCS_END}")
        return 1
    new = text.split(DOCS_BEGIN, 1)[0] + render_docs(doc) + text.split(DOCS_END, 1)[1]
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"docs-governance: wrote generated control table into {path}")
    else:
        print(f"docs-governance: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    current = _extract_block(path.read_text(encoding="utf-8"))
    if current is None:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    if current.strip() != render_docs(doc).strip():
        print(f"::error::{path}: control table is out of sync with the catalog; "
              "run: python3 scripts/check-docs-governance.py --write-docs " + str(path))
        return 1
    print(f"docs-governance: {path} control table is in sync with the catalog")
    return 0


def load_config(root: Path, explicit) -> dict:
    candidates = [explicit] if explicit else [root / "docs-governance.yaml",
                                              root / "governance" / "docs-governance.yaml"]
    for cand in candidates:
        if cand and Path(cand).is_file():
            data = yaml.safe_load(Path(cand).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    return {}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Documentation-governance guard (executable form of ADR-0081).")
    ap.add_argument("root", nargs="?", default=".", help="governed docs repository root to scan")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--config", default=None, help="optional per-repo docs-governance config YAML")
    ap.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--grace", type=int, default=0, help="days of grace past a freshness due date")
    ap.add_argument("--source-root", action="append", default=[], metavar="DIR",
                    help="checkout of a repository whose source paths docs may cite (DOC-0012); "
                         "repeatable. Citations into trees not supplied here are skipped.")
    ap.add_argument("--peer-root", action="append", default=[], metavar="NAME=DIR",
                    help="checkout of a peer documentation namespace whose ids this repo cites "
                         "(DOC-0015/DOC-0016); repeatable. Citations into namespaces not supplied "
                         "here are skipped, never passed.")
    ap.add_argument("--today", default=None, help="override today's date (ISO) for testing")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv[1:])

    controls_path = Path(args.controls)
    if not controls_path.is_file():
        print(f"::error::docs-governance: control catalog not found: {controls_path}")
        return 1
    doc = load_controls(controls_path)

    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_docs(doc))
        return 0

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"::error::docs-governance: root is not a directory: {root}")
        return 1
    today = as_date(args.today) or dt.date.today()
    config = load_config(root, args.config)
    source_roots = tuple(args.source_root) or tuple(config.get("source_roots") or ())
    peer_roots = {}
    for spec in args.peer_root:
        name, _, path = spec.partition("=")
        if not name or not path:
            print(f"::error::docs-governance: --peer-root expects NAME=DIR, got {spec!r}")
            return 1
        peer_roots[name] = Path(path)
    repo = DocsRepo(root, config, today, args.grace, source_roots, peer_roots)

    results = evaluate(repo, doc["controls"])
    threshold = SEVERITY_ORDER[args.fail_on]
    report = build_report(repo, results, doc.get("policy_ssot", []), args.fail_on, threshold)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        render_text(report, threshold)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
