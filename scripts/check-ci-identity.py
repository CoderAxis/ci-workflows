#!/usr/bin/env python3
"""Enforce controls/ci-identity.yaml against the Terraform that defines CI's AWS trust policies.

Answers one question no other checker asks: will these roles still admit our repositories after one
of them is renamed? GitHub's immutable `sub` claim embeds numeric owner and repository ids, and a
repository adopts that format the moment it is created, renamed or transferred. A trust policy
matching only the old name-based spelling therefore stops working on an event that touches nothing in
this repository, so review sees a policy that admits everything it currently needs and no check
disagrees. That has cost four outages here; see the catalog header for the list.

    ./scripts/check-ci-identity.py path/to/inboxxhq-infra
    ./scripts/check-ci-identity.py path/to/repo --format json

SCOPE, AND WHAT THIS DELIBERATELY DOES NOT CLAIM. This reads Terraform SOURCE. It cannot tell you
what is deployed, and the two have differed here before - ece69ccb was applied with
`aws iam update-assume-role-policy` before its Terraform landed. A green result means the
configuration is right, not that the account is. Detecting live drift needs AWS read access and is a
separate control.

It also parses text rather than evaluating HCL, because evaluating it would mean running Terraform
with a backend and credentials. Patterns are recognised as `"repo:..."` string literals grouped into
runs of adjacent lines, which is how all three shapes in use here render them: an inline list on an
`aws_iam_policy_document` condition, an inline list in a `jsonencode`d policy, and a `for`-generated
local. A construct this cannot decompose is REPORTED rather than skipped, on the grounds that a
credential boundary the checker cannot read is not evidence of anything.

Exit 0 when every control is upheld at or above --fail-on, 1 on violations, 2 on a bad catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a policy failure
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "ci-identity.yaml"

SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
MAX_DETAILS = 12

DOCS_BEGIN = "<!-- BEGIN ci-identity-controls (generated: scripts/check-ci-identity.py --write-docs) -->"
DOCS_END = "<!-- END ci-identity-controls -->"

# A `sub` pattern literal. Only `repo:`-prefixed subjects are GitHub Actions repository subjects;
# `system:serviceaccount:` subjects belong to an EKS cluster's OIDC provider, a different issuer with
# a different claim grammar that GitHub's change cannot affect, and must not be dragged in here.
PATTERN_RE = re.compile(r'"(repo:[^"]*)"')

# A `for <name> in [...]` binding, used to generate both owner spellings from one pattern literal.
FOR_BINDING_RE = re.compile(r"for\s+([A-Za-z_]\w*)\s+in\s+(.+)$")

# `${...}` interpolation. Masked before splitting a pattern into segments so that a `/`, `:` or `@`
# inside an expression cannot be mistaken for a segment delimiter.
INTERP_RE = re.compile(r"\$\{[^}]*\}")


class Finding(NamedTuple):
    control: str
    severity: str
    title: str
    result: str  # "pass" | "fail"
    evidence: str
    remediation: str


class Pattern(NamedTuple):
    """One `sub` pattern, decomposed into the three segments of GitHub's claim grammar.

    `repo:<owner>/<repository>:<subject>` — where `<owner>` is `NAME` classically and `NAME@ID` in
    the immutable spelling, and `<repository>` likewise. `subject` is everything after, e.g.
    `ref:refs/heads/main` or `environment:production`.
    """

    raw: str
    owner: str
    repository: str
    subject: str
    file: str
    line: int


class Group(NamedTuple):
    """Adjacent pattern literals — the patterns of a single `sub` condition.

    Grouping matters for CID-0001 and CID-0002, which are properties of a condition as a whole
    (does it offer an immutable spelling; do the spellings agree) rather than of any one pattern.
    """

    patterns: list[Pattern]
    file: str
    line: int
    binding: str | None  # the `for` expression generating these, when there is one


def load_catalog(path: Path) -> tuple[dict, dict[str, dict]]:
    """Return the raw catalog document and its controls indexed by id."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"::error::{path}: unreadable control catalog: {exc}")
    controls = (doc or {}).get("controls")
    if not isinstance(controls, list) or not controls:
        raise SystemExit(f"::error::{path}: invalid control catalog (expected a 'controls:' list)")
    by_id: dict[str, dict] = {}
    for c in controls:
        cid = c.get("id")
        if not cid or c.get("severity") not in SEVERITY_ORDER:
            raise SystemExit(f"::error::{path}: control {cid!r} missing a valid id/severity")
        by_id[cid] = c
    for required in ("CID-0001", "CID-0002", "CID-0003", "CID-0004"):
        if required not in by_id:
            raise SystemExit(f"::error::{path}: catalog is missing {required}, which has a detector")
    return doc, by_id


def render_docs(doc: dict) -> str:
    domain = doc.get("domain", "ci-identity")
    lines = [
        DOCS_BEGIN,
        "",
        f"_Generated from `controls/{domain}.yaml` by "
        "`scripts/check-ci-identity.py --write-docs` — do not edit by hand._",
        "",
        "| Control | Policy | Severity | Scope | Owner | Status |",
        "| ------- | ------ | -------- | ----- | ----- | ------ |",
    ]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        lines.append(
            f"| {c['id']} | {policy} | {c['severity']} | {c['scope']} | {c['owner']} | {c['status']} |"
        )
    lines += ["", DOCS_END]
    return "\n".join(lines)


def _extract_block(text: str) -> str | None:
    if DOCS_BEGIN in text and DOCS_END in text:
        return DOCS_BEGIN + text.split(DOCS_BEGIN, 1)[1].split(DOCS_END, 1)[0] + DOCS_END
    return None


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        print(
            f"::error::{path}: markers not found. Add these two lines where the table should go:\n"
            f"  {DOCS_BEGIN}\n  {DOCS_END}"
        )
        return 1
    new = text.split(DOCS_BEGIN, 1)[0] + render_docs(doc) + text.split(DOCS_END, 1)[1]
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"ci-identity: wrote generated control table into {path}")
    else:
        print(f"ci-identity: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    current = _extract_block(path.read_text(encoding="utf-8"))
    if current is None:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    if current.strip() != render_docs(doc).strip():
        print(
            f"::error::{path}: control table is out of sync with controls catalog; run: "
            f"python3 scripts/check-ci-identity.py --write-docs {path}"
        )
        return 1
    print(f"ci-identity: {path} control table is in sync with the catalog")
    return 0


def decompose(raw: str, file: str, line: int) -> Pattern | None:
    """Split a `repo:` subject into owner / repository / subject.

    Interpolations are masked first. `${var.terraform_apply_environment_prefix}` could in principle
    hold a `:` and `${var.github_org}` a `/`; masking means a delimiter inside an expression cannot
    be mistaken for a real one, which would silently mis-attribute segments and make every downstream
    judgement wrong in a way that still looked like a clean parse.
    """
    masks: list[str] = []

    def _mask(m: re.Match) -> str:
        masks.append(m.group(0))
        return f"\x00{len(masks) - 1}\x00"

    masked = INTERP_RE.sub(_mask, raw)

    def _unmask(s: str) -> str:
        for i, original in enumerate(masks):
            s = s.replace(f"\x00{i}\x00", original)
        return s

    body = masked[len("repo:"):]
    if "/" not in body:
        return None
    owner, rest = body.split("/", 1)
    if ":" not in rest:
        return None
    repository, subject = rest.split(":", 1)
    return Pattern(raw, _unmask(owner), _unmask(repository), _unmask(subject), file, line)


def collect_groups(root: Path) -> list[Group]:
    """Every run of adjacent `repo:` pattern literals in the Terraform under `root`."""
    groups: list[Group] = []
    for tf in sorted(root.rglob("*.tf")):
        # .terraform holds vendored provider and module copies - other people's code, and often many
        # copies of it, which would report the same finding once per copy.
        if any(part in {".terraform", ".git"} for part in tf.parts):
            continue
        try:
            lines = tf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        rel = str(tf.relative_to(root))
        current: list[Pattern] = []
        binding: str | None = None
        pending_binding: str | None = None
        for n, text in enumerate(lines, start=1):
            found = PATTERN_RE.search(text)
            if found:
                pat = decompose(found.group(1), rel, n)
                if pat is None:
                    # Unparseable, and deliberately surfaced as a synthetic pattern rather than
                    # dropped: a subject this cannot read is not evidence that it is well formed.
                    pat = Pattern(found.group(1), "", "", "", rel, n)
                if not current:
                    # Consume it. A binding explains the group that immediately follows it and
                    # nothing after that: left set, the first `for` in a file would attach itself to
                    # every later condition too, and since a binding suppresses the CID-0002
                    # comparison, that silently turned the rest of the file into passes.
                    binding, pending_binding = pending_binding, None
                current.append(pat)
                continue
            m = FOR_BINDING_RE.search(text)
            if m:
                pending_binding = m.group(2).strip()
            if current:
                groups.append(Group(current, rel, current[0].line, binding))
                current, binding = [], None
        if current:
            groups.append(Group(current, rel, current[0].line, binding))
    return groups


def owner_kind(pattern: Pattern, group: Group) -> str:
    """"immutable" | "classic" | "unresolved".

    A bare `${x}` owner is neither on its face: `local.ci_caller_subs` writes one literal and emits
    both spellings by iterating over `[o, "${o}@${id}"]`, so the spelling lives in the binding rather
    than in the pattern. When a binding is in scope and constructs an `@` form, the literal yields
    both spellings and counts as immutable. With no binding to explain it, the owner is unresolved -
    reported, not assumed good.
    """
    owner = pattern.owner
    if not owner:
        return "unresolved"
    if "@" in owner:
        return "immutable"
    # Exactly one interpolation and nothing else: the spelling is decided by whatever binds it.
    if INTERP_RE.fullmatch(owner):
        inner = owner[2:-1].strip()
        if inner.startswith(("var.", "local.", "data.")):
            return "classic"  # a plain variable holding a name
        if group.binding and "@" in group.binding:
            return "immutable"
        return "unresolved" if group.binding is None else "classic"
    return "classic"


def _immutable_pairs(group: Group) -> list[Pattern]:
    """Patterns that yield an immutable claim, however that spelling arises."""
    return [p for p in group.patterns if owner_kind(p, group) == "immutable"]


def _owner_spelt_inline(group: Group) -> list[Pattern]:
    """Patterns whose own owner segment carries the `@<id>`, so the id is readable here.

    Distinct from `_immutable_pairs`: a `for`-generated pattern is immutable because its BINDING
    constructs the `@` form, and its own owner segment is a bare interpolation with no id to inspect.
    Those are checked against the binding instead, so conflating the two crashed on a missing split.
    """
    return [p for p in group.patterns if "@" in p.owner]


def sub_offers_immutable_spelling(groups: list[Group]) -> tuple[bool, str, list[str]]:
    """CID-0001: every condition offers a pattern in the immutable-identifier spelling."""
    violations: list[str] = []
    for g in groups:
        kinds = {owner_kind(p, g) for p in g.patterns}
        if "immutable" in kinds:
            continue
        if "unresolved" in kinds:
            violations.append(
                f"{g.file}:{g.line}: cannot determine the owner spelling of "
                f"{len(g.patterns)} pattern(s); the checker will not treat an unreadable trust "
                f"condition as compliant"
            )
            continue
        violations.append(
            f"{g.file}:{g.line}: {len(g.patterns)} pattern(s), all in the classic name-only "
            f"spelling; this condition stops admitting a repository the moment it is renamed "
            f"(e.g. {g.patterns[0].raw})"
        )
    if violations:
        return False, f"{len(violations)} sub condition(s) without an immutable spelling", violations
    return True, f"all {len(groups)} sub condition(s) offer an immutable-identifier spelling", []


def spellings_cover_same_subjects(groups: list[Group]) -> tuple[bool, str, list[str]]:
    """CID-0002: the classic and immutable spellings of a condition reach the same subjects."""
    violations: list[str] = []
    checked = 0
    for g in groups:
        # A `for`-generated group writes each subject once and emits it under both spellings, so the
        # two sets are equal by construction and there is nothing to compare.
        if g.binding and "@" in g.binding:
            continue
        classic = {p.subject for p in g.patterns if owner_kind(p, g) == "classic"}
        immutable = {p.subject for p in g.patterns if owner_kind(p, g) == "immutable"}
        if not classic or not immutable:
            continue
        checked += 1
        only_classic = sorted(classic - immutable)
        only_immutable = sorted(immutable - classic)
        if only_classic:
            violations.append(
                f"{g.file}:{g.line}: subject(s) {only_classic} are reachable only under the classic "
                f"spelling, so they are revoked when a repository is renamed"
            )
        if only_immutable:
            violations.append(
                f"{g.file}:{g.line}: subject(s) {only_immutable} are reachable only under the "
                f"immutable spelling, so they do not apply until a repository is renamed"
            )
    if violations:
        return False, f"{len(violations)} spelling asymmetr(ies)", violations
    return True, f"both spellings cover the same subjects in all {checked} paired condition(s)", []


def owner_id_is_pinned(groups: list[Group]) -> tuple[bool, str, list[str]]:
    """CID-0003: the owner id in an immutable pattern is pinned, never wildcarded."""
    violations: list[str] = []
    checked = 0
    for g in groups:
        for p in _owner_spelt_inline(g):
            checked += 1
            owner_id = p.owner.split("@", 1)[1]
            if "*" in owner_id:
                violations.append(
                    f"{p.file}:{p.line}: owner id is wildcarded (`@{owner_id}`), which keeps the "
                    f"immutable syntax and discards the guarantee it exists for - a recycled owner "
                    f"name would match again"
                )
        # The binding case: the `@` construction lives in the `for`, so inspect it there.
        if g.binding and "@*" in g.binding:
            violations.append(
                f"{g.file}:{g.line}: the owner spelling is generated with a wildcarded id "
                f"(`@*`) in `{g.binding[:60]}`"
            )
    if violations:
        return False, f"{len(violations)} wildcarded owner id(s)", violations
    return True, f"every owner id is pinned across {checked} immutable pattern(s)", []


def repo_segment_pins_id_not_name(groups: list[Group]) -> tuple[bool, str, list[str]]:
    """CID-0004: an immutable pattern's repository segment is `*` or `*@<pinned id>`."""
    violations: list[str] = []
    checked = 0
    for g in groups:
        for p in _immutable_pairs(g):
            checked += 1
            repo = p.repository
            if repo == "*":
                continue  # owner-wide; the pinned owner id carries the boundary
            if "@" not in repo:
                violations.append(
                    f"{p.file}:{p.line}: repository segment `{repo}` carries no id, so it can never "
                    f"match an immutable claim - that format always includes the repository id and "
                    f"it cannot be removed. This pattern is dead."
                )
                continue
            name, repo_id = repo.split("@", 1)
            if "*" in repo_id:
                violations.append(
                    f"{p.file}:{p.line}: repository segment `{repo}` pins the NAME and wildcards the "
                    f"id - backwards. A rename changes the name and keeps the id, so this breaks on "
                    f"the very event the immutable spelling exists to survive, and the wildcarded id "
                    f"readmits a recycled name. Use `*@<repo-id>`."
                )
            elif name != "*":
                violations.append(
                    f"{p.file}:{p.line}: repository segment `{repo}` pins the repository NAME "
                    f"alongside its id; the id alone identifies it uniquely, and the name only adds "
                    f"a failure on rename. Use `*@<repo-id>`."
                )
    if violations:
        return False, f"{len(violations)} mis-scoped repository segment(s)", violations
    return True, f"every repository segment pins the id across {checked} immutable pattern(s)", []


DETECTORS = {
    "sub_offers_immutable_spelling": sub_offers_immutable_spelling,
    "spellings_cover_same_subjects": spellings_cover_same_subjects,
    "owner_id_is_pinned": owner_id_is_pinned,
    "repo_segment_pins_id_not_name": repo_segment_pins_id_not_name,
}


def check_root(root: Path, controls: dict[str, dict]) -> tuple[list[Finding], int]:
    groups = collect_groups(root)
    findings: list[Finding] = []
    for cid, control in sorted(controls.items()):
        detector = DETECTORS.get(control.get("detector"))
        if detector is None:
            continue
        ok, evidence, details = detector(groups)
        if details:
            evidence = evidence + ": " + "; ".join(details[:MAX_DETAILS])
        findings.append(
            Finding(
                cid,
                control["severity"],
                control["title"],
                "pass" if ok else "fail",
                evidence,
                str(control.get("remediation", "")).strip(),
            )
        )
    return findings, len(groups)


def report(roots: Iterable[Path], controls: dict[str, dict], fail_on: str, fmt: str) -> int:
    threshold = SEVERITY_ORDER[fail_on]
    failed = 0
    payload: list[dict] = []
    total_groups = 0

    for root in roots:
        findings, n_groups = check_root(root, controls)
        total_groups += n_groups
        if not n_groups:
            if fmt == "text":
                print(f"[skip] {root}: no GitHub-OIDC sub patterns found in any .tf file")
            continue
        payload.append({"root": str(root), "sub_conditions": n_groups,
                        "findings": [f._asdict() for f in findings]})
        for f in findings:
            blocking = f.result == "fail" and SEVERITY_ORDER[f.severity] >= threshold
            if f.result == "fail":
                failed += blocking
            if fmt != "text":
                continue
            if f.result == "pass":
                print(f"[ok] {f.control} [{f.severity}] {f.title}: {f.evidence}")
            elif blocking:
                print(f"::error::[{f.control}][{f.severity}] {f.title} - {f.evidence}")
                print(f"    remediation: {f.remediation}")
            else:
                print(
                    f"::warning::[{f.control}][{f.severity}] {f.title} - {f.evidence} "
                    "(advisory; below fail-on)"
                )

    if fmt == "json":
        print(json.dumps({"fail_on": fail_on, "roots": payload}, indent=2))
    elif fmt == "text":
        verdict = "FAIL" if failed else "OK"
        print(
            f"\nci-identity: {verdict} - {len(payload)} root(s) checked, "
            f"{total_groups} GitHub-OIDC sub condition(s) (fail-on={fail_on})."
        )
        print("Policy SSOT: ADR-0051, ADR-0110. Source-only: this does not read live AWS policy.")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", default=["."], help="Terraform root(s) to check (default: .)")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--fail-on", choices=tuple(SEVERITY_ORDER), default="major")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv)

    doc, controls = load_catalog(Path(args.controls))
    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))

    roots = [Path(r).resolve() for r in (args.roots or ["."])]
    return report(roots, controls, args.fail_on, args.format)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
