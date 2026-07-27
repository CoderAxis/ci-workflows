#!/usr/bin/env python3
"""Enforce controls/workflow-centralization.yaml against a consumer repository.

Answers one question no other checker asks: does this repo USE the central workflows, or does it
keep its own copies of them? Every other control catalog governs what a central workflow does once
it is called; none governs adoption, so a bespoke copy of a published workflow violates nothing and
CI stays green while the repo runs an older policy than the fleet believes it runs.

The set of "central workflows" is NOT hardcoded. It is derived from the workflows in this repository
that declare `on.workflow_call`, so publishing a new reusable workflow automatically extends the
control's reach and retiring one automatically narrows it. There is no list to keep in step.

    ./scripts/check-workflow-centralization.py                 # check the repo in $PWD
    ./scripts/check-workflow-centralization.py path/to/repo …  # check specific repo roots
    ./scripts/check-workflow-centralization.py --format json   # machine-readable report

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
DEFAULT_CONTROLS = SELF_REPO / "controls" / "workflow-centralization.yaml"
WORKFLOW_DIR = Path(".github") / "workflows"

SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}

DOCS_BEGIN = (
    "<!-- BEGIN workflow-centralization-controls "
    "(generated: scripts/check-workflow-centralization.py --write-docs) -->"
)
DOCS_END = "<!-- END workflow-centralization-controls -->"

# `uses: coderaxis/github-actions/.github/workflows/<name>@<ref>` — a reusable-workflow call.
# Deliberately distinct from `coderaxis/github-actions/<action>@<ref>`, which is a composite ACTION.
# Conflating the two overstates how much of the fleet is centralised: a workflow that merely borrows
# a central action still owns all of its own steps.
CENTRAL_CALL_RE = re.compile(
    r"uses:\s*coderaxis/github-actions/\.github/workflows/([\w.-]+)@([\w./-]+)"
)
WORKFLOW_CALL_RE = re.compile(r"^\s*workflow_call:", re.M)
MAJOR_TAG_RE = re.compile(r"^v\d+$")


class Finding(NamedTuple):
    control: str
    severity: str
    title: str
    result: str  # "pass" | "fail"
    evidence: str
    remediation: str


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
    for required in ("WFC-001", "WFC-002", "WFC-004", "WFC-005"):
        if required not in by_id:
            raise SystemExit(f"::error::{path}: catalog is missing {required}, which has a detector")
    return doc, by_id


# `write` satisfies a declared `read`; the reverse is not true. `none` grants nothing.
_ACCESS_RANK = {"none": 0, "read": 1, "write": 2}

# Absence of a `permissions:` key is NOT the empty set. With no block anywhere, the job receives the
# repository's default token, which under `default_workflow_permissions: read` still carries
# contents/metadata/packages read. Declaring a block is what drops every unlisted scope to `none`.
# Modelling absence as "grants nothing" reports every permissionless caller of a `contents: read`
# workflow as broken, including ones that demonstrably pass today.
#
# `id-token` is deliberately absent: it is never granted implicitly at any default setting, which is
# exactly why the OIDC-based calls are the ones that break.
_DEFAULT_TOKEN = {"contents": "read", "metadata": "read", "packages": "read"}


def _declared_permissions(text: str) -> dict[str, str] | str | None:
    """The most a called workflow can ask for: its workflow-level block UNIONED with every job's.

    Checking only the workflow-level block is not enough. A called workflow's jobs are each capped
    by what the CALLER granted, so a single job asking for one extra scope is enough to break the
    call - and that is easy to introduce, because adding the scope to the job looks locally correct
    and the file it breaks is in another repository. Taking the union over the workflow-level block
    and every job-level block gives the upper bound the caller has to cover.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    blocks = [doc.get("permissions")]
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job, dict):
            blocks.append(job.get("permissions"))
    # A blanket string anywhere dominates the union.
    for b in blocks:
        if isinstance(b, str) and b != "read-all":
            return b
    union: dict[str, str] = {}
    for b in blocks:
        if not isinstance(b, dict):
            continue
        for scope, level in b.items():
            if _ACCESS_RANK.get(str(level), 0) > _ACCESS_RANK.get(str(union.get(scope, "none")), 0):
                union[scope] = level
    return union or None


def permission_gaps(
    granted: dict[str, str] | str | None,
    declared: dict[str, str] | str | None,
    declared_present: bool = True,
) -> list[str]:
    """Permissions the callee declares that the caller's effective set does not cover.

    `granted` is the caller's effective set: the job's block, else the workflow's. `declared_present`
    says whether either block actually existed, which distinguishes "granted nothing" from "granted
    the repository default" — see _DEFAULT_TOKEN.
    """
    if not declared or isinstance(declared, str):
        # A callee with no block of its own asks for nothing in particular, and a blanket string
        # (`read-all`/`write-all`) is never the narrower side of this comparison.
        return []
    if granted == "write-all":
        return []
    if isinstance(granted, dict):
        effective = granted
    elif granted == "read-all":
        effective = {k: "read" for k in declared}
    elif not declared_present:
        effective = _DEFAULT_TOKEN
    else:
        effective = {}
    gaps = []
    for scope, want in declared.items():
        if _ACCESS_RANK.get(str(want), 0) == 0:
            continue
        have = _ACCESS_RANK.get(str(effective.get(scope, "none")), 0)
        if have < _ACCESS_RANK.get(str(want), 0):
            gaps.append(f"{scope}: {want}")
    return gaps


def render_docs(doc: dict) -> str:
    domain = doc.get("domain", "workflow-centralization")
    lines = [
        DOCS_BEGIN,
        "",
        f"_Generated from `controls/{domain}.yaml` by "
        "`scripts/check-workflow-centralization.py --write-docs` — do not edit by hand._",
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
        print(f"workflow-centralization: wrote generated control table into {path}")
    else:
        print(f"workflow-centralization: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    current = _extract_block(path.read_text(encoding="utf-8"))
    if current is None:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    if current.strip() != render_docs(doc).strip():
        print(
            f"::error::{path}: control table is out of sync with controls catalog; run: "
            f"python3 scripts/check-workflow-centralization.py --write-docs {path}"
        )
        return 1
    print(f"workflow-centralization: {path} control table is in sync with the catalog")
    return 0


def published_reusable_workflows(repo: Path) -> dict[str, Path]:
    """Workflows in THIS repo that declare on.workflow_call — the authoritative central set."""
    found: dict[str, Path] = {}
    for wf in workflow_files(repo):
        try:
            if WORKFLOW_CALL_RE.search(wf.read_text(encoding="utf-8")):
                found[wf.name] = wf
        except OSError:
            continue
    if not found:
        raise SystemExit(
            f"::error::{repo / WORKFLOW_DIR}: no reusable workflows found. The central set is "
            "derived from this repo; without it the check would vacuously pass."
        )
    return found


def workflow_files(root: Path) -> list[Path]:
    """Every workflow, under either extension.

    Both are collected deliberately even though only `.yaml` is permitted: a checker that globbed
    `*.yaml` alone would not SEE a `.yml` file, so the extension rule would be unenforceable by the
    thing meant to enforce it, and a repo could opt out of every control here by renaming.
    """
    d = root / WORKFLOW_DIR
    return sorted([*d.glob("*.yaml"), *d.glob("*.yml")])


def consumer_workflows(root: Path) -> list[Path]:
    return workflow_files(root)


_RELEASED_EVENT = re.compile(r"^[a-z0-9][a-z0-9-]*-released$")


def _is_local_bump(filename: str, doc: dict) -> bool:
    """A workflow that propagates a dependency bump inside the repository that hosts it.

    Two independent signals, because either alone misses a real case. A listener is identified by
    its `repository_dispatch` type - that is the mechanism, and a rename does not change it. A
    bump driven only by `workflow_dispatch` has no such trigger, so the filename is checked as
    well; `bump-shared-modules.yaml`, the 315-line local implementation that existed in 70
    repositories, is caught by both.
    """
    if filename.startswith("bump-"):
        return True
    on = doc.get("on") or doc.get(True)  # bare `on:` parses as the boolean True in YAML 1.1
    if not isinstance(on, dict):
        return False
    dispatch = on.get("repository_dispatch")
    if not isinstance(dispatch, dict):
        return False
    types = dispatch.get("types")
    if isinstance(types, str):
        types = [types]
    return any(isinstance(t, str) and _RELEASED_EVENT.match(t) for t in (types or []))


def _is_reusable(text: str) -> bool:
    """True when the workflow is itself published for others to call.

    Such a file is exempt from the lane allowlist because GitHub resolves a reusable workflow only
    at `<owner>/<repo>/.github/workflows/<file>@<ref>` - it has to live where it is published from.
    A workflow that merely CALLS a reusable one is not exempt; that is an ordinary lane.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    on = doc.get("on") or doc.get(True)  # bare `on:` parses as the boolean True in YAML 1.1
    return isinstance(on, dict) and "workflow_call" in on


def check_repo(
    root: Path, published: dict[str, Path], controls: dict[str, dict], central: Path
) -> list[Finding]:
    findings: list[Finding] = []
    shadows: list[str] = []
    bad_refs: list[str] = []
    perm_gaps: list[str] = []
    local_bumps: list[str] = []
    extra_lanes: list[str] = []

    # The publishing repo cannot shadow its own publications: those files ARE the central
    # workflows, and they do not call themselves.
    is_central = root == central
    allowed_lanes = set(controls["WFC-006"].get("allowed_lanes") or ())

    for wf in consumer_workflows(root):
        try:
            text = wf.read_text(encoding="utf-8")
        except OSError:
            continue
        calls = CENTRAL_CALL_RE.findall(text)

        # WFC-001 — a file named like something we publish, that does not call it.
        if not is_central and wf.name in published and not any(n == wf.name for n, _ in calls):
            shadows.append(str(wf.relative_to(root)))

        # WFC-002 — every central call pins a major tag.
        for name, ref in calls:
            if not MAJOR_TAG_RE.match(ref):
                bad_refs.append(f"{wf.relative_to(root)} -> {name}@{ref}")

        # WFC-006 — only the permitted lanes, plus workflows this repo publishes as reusable.
        # The exemption is checked against the file's own `on:` rather than against a list of
        # repository names, so it cannot be claimed by a repo that merely asserts it is special.
        #
        # The extension is part of the rule. Both spellings are valid YAML and valid to GitHub, so
        # nothing fails when they are mixed - which is precisely why they drift, and why a search
        # for `ci.yaml` silently misses the repositories that spell it `ci.yml`.
        if wf.suffix != ".yaml":
            extra_lanes.append(f"{wf.relative_to(root)} (must be .yaml, not {wf.suffix})")
        elif wf.name not in allowed_lanes and not _is_reusable(text):
            extra_lanes.append(str(wf.relative_to(root)))

        # WFC-004 — a caller grants what its callee declares. Requires the parsed document: the
        # relationship is between a job's effective permissions and another FILE's, so it is not
        # expressible as a pattern over this file's text.
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            doc = None
        if not isinstance(doc, dict):
            continue

        # WFC-005 — a local bump. Matched on the trigger rather than the filename, because the
        # thing that makes a file a bump listener is that it subscribes to `<module>-released`;
        # renaming it changes nothing. The filename is checked too, since a bump driven only by
        # workflow_dispatch has no such trigger to match on.
        if not is_central and _is_local_bump(wf.name, doc):
            local_bumps.append(str(wf.relative_to(root)))
        wf_level = doc.get("permissions")
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if not isinstance(uses, str):
                continue
            callee: Path | None = None
            if uses.startswith("./"):
                callee = root / uses.split("@")[0].lstrip("./")
            else:
                m = CENTRAL_CALL_RE.search(f"uses: {uses}")
                if m:
                    callee = published.get(m.group(1))
            if callee is None or not callee.is_file():
                continue
            try:
                declared = _declared_permissions(callee.read_text(encoding="utf-8"))
            except OSError:
                continue
            # A job with no permissions: block inherits the workflow-level set; if neither exists
            # the run falls back to the repository default token, which is not the empty set.
            declared_present = "permissions" in job or "permissions" in doc
            granted = job.get("permissions", wf_level)
            gaps = permission_gaps(granted, declared, declared_present)
            if gaps:
                perm_gaps.append(
                    f"{wf.relative_to(root)}:{job_id} -> {callee.name} needs {', '.join(gaps)}"
                )

    c1 = controls["WFC-001"]
    findings.append(
        Finding(
            "WFC-001",
            c1["severity"],
            c1["title"],
            "fail" if shadows else "pass",
            (
                f"{len(shadows)} local implementation(s) of a published reusable workflow: "
                + ", ".join(shadows)
            )
            if shadows
            else (
                f"publishes the {len(published)} reusable workflow(s); shadowing not applicable"
                if is_central
                else f"no local shadows of the {len(published)} published reusable workflow(s)"
            ),
            c1["remediation"].strip(),
        )
    )

    c2 = controls["WFC-002"]
    findings.append(
        Finding(
            "WFC-002",
            c2["severity"],
            c2["title"],
            "fail" if bad_refs else "pass",
            f"{len(bad_refs)} call(s) not pinned to a major tag: " + ", ".join(bad_refs)
            if bad_refs
            else "every central reusable-workflow call pins a major tag",
            c2["remediation"].strip(),
        )
    )

    c4 = controls["WFC-004"]
    findings.append(
        Finding(
            "WFC-004",
            c4["severity"],
            c4["title"],
            "fail" if perm_gaps else "pass",
            f"{len(perm_gaps)} call(s) would be rejected at load time: " + "; ".join(perm_gaps)
            if perm_gaps
            else "every reusable-workflow call grants the permissions its callee declares",
            c4["remediation"].strip(),
        )
    )

    c5 = controls["WFC-005"]
    findings.append(
        Finding(
            "WFC-005",
            c5["severity"],
            c5["title"],
            "fail" if local_bumps else "pass",
            f"{len(local_bumps)} local bump workflow(s): " + ", ".join(local_bumps)
            if local_bumps
            else (
                "central release owns bump propagation; nothing to shadow here"
                if is_central
                else "no local bump implementation or release listener"
            ),
            c5["remediation"].strip(),
        )
    )

    c6 = controls["WFC-006"]
    findings.append(
        Finding(
            "WFC-006",
            c6["severity"],
            c6["title"],
            "fail" if extra_lanes else "pass",
            f"{len(extra_lanes)} workflow(s) outside the permitted lanes: " + ", ".join(extra_lanes)
            if extra_lanes
            else f"only the permitted lanes ({', '.join(sorted(allowed_lanes))}) and published "
            "reusable workflows",
            c6["remediation"].strip(),
        )
    )
    return findings


def report(
    roots: Iterable[Path],
    published: dict[str, Path],
    controls: dict[str, dict],
    central: Path,
    fail_on: str,
    fmt: str,
) -> int:
    threshold = SEVERITY_ORDER[fail_on]
    failed = 0
    payload: list[dict] = []

    for root in roots:
        if not (root / WORKFLOW_DIR).is_dir():
            if fmt == "text":
                print(f"[skip] {root}: no {WORKFLOW_DIR} directory")
            continue
        findings = check_repo(root, published, controls, central)
        payload.append({"repo": str(root), "findings": [f._asdict() for f in findings]})

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
        print(json.dumps({"fail_on": fail_on, "repos": payload}, indent=2))
    elif fmt == "text":
        verdict = "FAIL" if failed else "OK"
        print(
            f"\nworkflow-centralization: {verdict} - {len(payload)} repo(s) checked against "
            f"{len(published)} published reusable workflow(s) (fail-on={fail_on})."
        )
        print("Policy SSOT: ADR-0081, ADR-0051.")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", default=["."], help="repo root(s) to check (default: .)")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument(
        "--central-repo",
        default=str(SELF_REPO),
        help="repo publishing the reusable workflows (default: this one)",
    )
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

    central = Path(args.central_repo).resolve()
    published = published_reusable_workflows(central)
    roots = [Path(r).resolve() for r in (args.roots or ["."])]
    return report(roots, published, controls, central, args.fail_on, args.format)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
