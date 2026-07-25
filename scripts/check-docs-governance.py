#!/usr/bin/env python3
"""Documentation-governance guard (CI). Executable, data-driven policy-as-code for the
generic documentation-governance contract every governed docs repo shares.

This is the single source of the documentation-governance LOGIC. It is consumed by the
`docs-governance.yml` reusable workflow and run against a governed docs repository
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
    (DOC-010) but never duplicated.
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
    "adr", "architecture", "compliance", "contracts", "governance", "journeys", "messaging",
    "onboarding", "operations", "playbooks", "policy", "product", "reference", "rfc", "rfcs",
    "scalability", "security", "standards", "testing", "workflows",
)
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
DECISION_ID_RE = re.compile(r"^(ADR|RFC)-\d+", re.IGNORECASE)
RELATED_LIST_KEYS = ("related_services", "related_rfcs", "related_adrs")
LEGACY_RELATED_KEYS = {"related_rfc": "related_rfcs", "related_adr": "related_adrs"}
MAX_DETAILS = 50  # cap per-control violation lines printed, to avoid flooding CI logs


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
    def __init__(self, root: Path, config: dict, today: dt.date, grace: int):
        self.root = root
        self.today = today
        self.grace = grace
        extra_roots = tuple(config.get("extra_doc_roots") or ())
        self.doc_roots = tuple(dict.fromkeys(DEFAULT_DOC_ROOTS + extra_roots))
        self.doc_types = DEFAULT_DOC_TYPES | set(config.get("extra_doc_types") or ())
        self.governed: list[DocFile] = self._load_governed()
        self.all_md: list[Path] = [p for p in sorted(root.rglob("*.md")) if ".git" not in p.parts]
        self.owner_slugs, self.owner_errors = self._load_owner_registry()
        self.has_catalog = (root / "catalog").is_dir()
        self.client_scope_terms = self._load_client_scope_terms()

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
            slugs.add(slug.strip())
        return slugs, errors

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
        if d.rel.parts[0] not in ("adr", "rfc", "rfcs"):
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


def owner_registry_usage(repo: DocsRepo) -> Finding:
    used = {d.data["owner"].strip() for d in repo.governed
            if d.data and isinstance(d.data.get("owner"), str) and d.data["owner"].strip()
            and not is_template_doc(d.rel)}
    unused = sorted(repo.owner_slugs - used)
    if unused:
        return Finding(False, f"{len(unused)} registered owner slug(s) are unused",
                       [f"unused owner slug: {s}" for s in unused])
    return Finding(True, f"all {len(repo.owner_slugs)} registered owner slugs are in use")


DETECTORS = {
    "frontmatter_structure": frontmatter_structure,
    "owner_registered": owner_registered,
    "controlled_vocabulary": controlled_vocabulary,
    "related_key_hygiene": related_key_hygiene,
    "related_docs_links": related_docs_links,
    "supersession_integrity": supersession_integrity,
    "freshness_sla": freshness_sla,
    "client_scope_isolation": client_scope_isolation,
    "owner_registry_usage": owner_registry_usage,
}
# Domain-specific catalog schema+drift is delegated to the repo's own scripts/build_catalog.py
# --check, invoked by the docs-governance reusable workflow. Declared as policy, never
# duplicated here.
DELEGATED_DETECTORS = {
    "delegated_catalog_build": "delegated to the docs-governance workflow catalog step "
                               "(scripts/build_catalog.py --check); not evaluated by this checker",
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


def build_report(repo: DocsRepo, results, ssot, fail_on, threshold) -> dict:
    enforced = [r for r in results if is_enforced(r, threshold)]
    return {
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
    repo = DocsRepo(root, load_config(root, args.config), today, args.grace)

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
