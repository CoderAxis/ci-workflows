#!/usr/bin/env python3
"""Enforce controls/api-contract.yaml against a service repository.

Answers the question no existing checker asks: does this service's HTTP API actually
obey the platform's contract decisions, or does it merely have a spec file? The central
openapi-contract workflow already proves a spec exists, lints, and that the repo's own
contract tests pass. None of that stops a service from shipping a Swagger UI, inventing
its own response envelope, hand-rolling a copy of the shared conformance suite, or
committing four rival spec files - all of which the fleet had done.

    ./scripts/check-api-contract.py                  # check the repo in $PWD
    ./scripts/check-api-contract.py path/to/repo …   # check specific service roots
    ./scripts/check-api-contract.py --format json    # machine-readable report

RATCHET. Migrating 36 services is a program of work, not a PR, so a gate that fails on
the whole backlog on day one would simply be switched off. Each repo may commit an
`.api-contract-baseline.json` freezing its CURRENT violation count per control. The gate
then fails only when a count RISES. A repo with no baseline is held to zero, so a service
created tomorrow cannot introduce any of this, and a service with debt can only shrink it.
The baseline is a small, reviewable, CODEOWNERS-guardable diff - raising it is a visible
act, not an accident.

Exit 0 when every control is upheld at or above --fail-on, 1 on violations, 2 on a bad catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a policy failure
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "api-contract.yaml"
BASELINE_FILE = ".api-contract-baseline.json"

# The proto projection API-007 compares against. Generated, never hand-written:
#   go run ./platform/openapicontract/commonv1policy/cmd/emit-canonical-components
# in platform-shared-go, redirected here. Its `source.version` records the
# platform-contracts-go release it was projected from.
CANONICAL_COMPONENTS = SELF_REPO / "controls" / "common-v1-components.json"
_canonical_cache = None


def load_canonical_components():
    """Return (components, source_version), or (None, None) when unavailable.

    Callers must treat unavailable as a configuration error, not a skip - main() checks
    this up front and exits 2. The artifact ships in this repository beside this script,
    so its absence means a broken checkout (a sparse-checkout that omitted controls/, say),
    and a version gate that reports a pass because it could not find its reference is worse
    than no gate at all.
    """
    global _canonical_cache
    if _canonical_cache is None:
        try:
            doc = json.loads(CANONICAL_COMPONENTS.read_text(encoding="utf-8"))
            _canonical_cache = (doc.get("components") or {},
                                (doc.get("source") or {}).get("version", "unknown"))
        except (OSError, json.JSONDecodeError):
            _canonical_cache = (None, None)
    return _canonical_cache

SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"active", "deprecated", "superseded"}
VALID_SCOPE = {"service", "spec", "source"}
VALID_APPLIES = {"always", "http-api"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "applies_when",
                   "policy", "rationale", "remediation", "detector", "refs")
MAX_DETAILS = 25

DOCS_BEGIN = "<!-- BEGIN api-contract-controls (generated: scripts/check-api-contract.py --write-docs) -->"
DOCS_END = "<!-- END api-contract-controls -->"

# The canonical envelope components, owned by proto/common/v1 (CONTRACT_AUTHORITY_MATRIX row 2).
CANONICAL_SUCCESS = "common.v1.SuccessResponse"
CANONICAL_ERROR = "common.v1.ErrorResponse"

# Spec files a service may legitimately commit. Anything else under docs/ matching
# openapi*.json is a rival spec - the exact hygiene failure that left auth carrying a
# 307KB "premigration" spec advertising 58 paths against a live 49.
ALLOWED_SPEC_FILES = {
    "openapi.json",                      # the generated contract
    "openapi.base.json",                 # authored, non-derivable metadata only
    "openapi.operationids.lock.json",    # semver-governed operationId registry
    "openapi.operationids.schema.json",  # schema for the lock
}

# Runtime documentation surfaces, forbidden by ADR-0067.
RUNTIME_DOCS_MARKERS = (
    (re.compile(r"platform/swaggerpolicy"), "imports swaggerpolicy (runtime docs gating)"),
    (re.compile(r"swaggo/gin-swagger|ginSwagger"), "imports gin-swagger (Swagger UI handler)"),
    (re.compile(r"openapiroutes\.Register"), "registers openapiroutes spec endpoints"),
    (re.compile(r"^//go:build .*\bswagger\b", re.M), "carries a `swagger` build tag"),
    (re.compile(r"\"/swagger(/|\")"), "registers a /swagger route"),
    (re.compile(r"\"/api/docs(-json)?\""), "registers an /api/docs route"),
)

# A hand-rolled copy of the shared conformance suite: driving kin-openapi directly.
KIN_OPENAPI_MARKERS = re.compile(r"gorillamux\.NewRouter|openapi3filter\.ValidateResponse")
SHARED_CONFORMANCE_IMPORT = "openapicontract/conformance"


@dataclass
class Finding:
    ok: bool
    evidence: str
    details: list = field(default_factory=list)
    count: int = 0


def _capped(items: list) -> list:
    if len(items) <= MAX_DETAILS:
        return items
    return items[:MAX_DETAILS] + [f"... and {len(items) - MAX_DETAILS} more"]


# --- repository context: read the service once, shared by every detector -------------------

class ServiceRepo:
    def __init__(self, root: Path):
        self.root = root
        self.name = root.name
        self.spec_path = root / "docs" / "openapi.json"
        self.spec, self.spec_error = self._load_spec()
        self.go_sources = self._load_go_sources()
        self.baseline = self._load_baseline()

    def _load_spec(self):
        if not self.spec_path.is_file():
            return None, None
        try:
            return json.loads(self.spec_path.read_text(encoding="utf-8")), None
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(exc)

    def _load_go_sources(self) -> list:
        """Every non-vendored .go file, with its text, split into runtime and test."""
        out = []
        for path in sorted(self.root.rglob("*.go")):
            rel_parts = path.relative_to(self.root).parts
            # Dot-directories are tooling, not the service. In CI the central checker is
            # checked out into .api-contract-tools inside the repo being judged; scanning it
            # would let the guard fail on its own fixtures.
            if any(p.startswith(".") for p in rel_parts):
                continue
            if "vendor" in rel_parts or "node_modules" in rel_parts:
                continue
            try:
                out.append((path.relative_to(self.root), path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
        return out

    def runtime_sources(self):
        return [(rel, text) for rel, text in self.go_sources if not rel.name.endswith("_test.go")]

    def test_sources(self):
        return [(rel, text) for rel, text in self.go_sources if rel.name.endswith("_test.go")]

    def has_http_api(self) -> bool:
        return self.spec is not None or self.spec_error is not None

    def operations(self):
        """(path, method, operation) for every operation in the spec."""
        if not self.spec:
            return []
        out = []
        for p, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() in ("get", "put", "post", "delete", "patch", "head", "options") \
                        and isinstance(op, dict):
                    out.append((p, method.lower(), op))
        return out

    def _load_baseline(self) -> dict:
        path = self.root / BASELINE_FILE
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data.get("controls", {}) if isinstance(data, dict) else {}


# --- detectors: (ServiceRepo) -> Finding ---------------------------------------------------

def no_runtime_docs(repo: ServiceRepo) -> Finding:
    """API-001: the deployed runtime serves no documentation (ADR-0067)."""
    violations = []
    for rel, text in repo.runtime_sources():
        for pattern, why in RUNTIME_DOCS_MARKERS:
            if pattern.search(text):
                violations.append(f"{rel}: {why}")
    violations = sorted(set(violations))
    if violations:
        return Finding(False, f"{len(violations)} runtime documentation surface(s)",
                       _capped(violations), len(violations))
    return Finding(True, "no runtime documentation surface", count=0)


def single_committed_spec(repo: ServiceRepo) -> Finding:
    """API-002: exactly one committed REST spec, plus its declared generator inputs."""
    docs = repo.root / "docs"
    if not docs.is_dir():
        return Finding(True, "no docs/ directory", count=0)
    candidates = sorted({p.name for pattern in ("openapi*.json", "openapi*.yaml", "openapi*.yml",
                                                "swagger*.json", "swagger*.yaml", "swagger*.yml")
                         for p in docs.glob(pattern)})
    extras = [n for n in candidates if n not in ALLOWED_SPEC_FILES]
    if extras:
        return Finding(False, f"{len(extras)} rival spec file(s) committed",
                       [f"docs/{n}: not one of {sorted(ALLOWED_SPEC_FILES)}" for n in extras],
                       len(extras))
    return Finding(True, "one committed spec (plus allowed generator inputs)", count=0)


def shared_conformance_suite(repo: ServiceRepo) -> Finding:
    """API-003: conformance is imported from the shared engine, never re-implemented."""
    hand_rolled = []
    for rel, text in repo.test_sources():
        if KIN_OPENAPI_MARKERS.search(text) and SHARED_CONFORMANCE_IMPORT not in text:
            hand_rolled.append(f"{rel}: drives kin-openapi directly instead of importing "
                               f"{SHARED_CONFORMANCE_IMPORT}")
    hand_rolled = sorted(set(hand_rolled))
    if hand_rolled:
        return Finding(False, f"{len(hand_rolled)} hand-rolled conformance validator(s)",
                       _capped(hand_rolled), len(hand_rolled))
    return Finding(True, "conformance validation comes from the shared suite", count=0)


def canonical_envelope(repo: ServiceRepo) -> Finding:
    """API-004: every JSON response references the proto common.v1 envelope."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    offenders = []
    for path, method, op in repo.operations():
        for status, resp in (op.get("responses") or {}).items():
            if not isinstance(resp, dict):
                continue
            schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
            if not isinstance(schema, dict):
                continue  # bodiless (204) or non-JSON: nothing to bind
            wanted = CANONICAL_ERROR if status.startswith(("4", "5")) else CANONICAL_SUCCESS
            if wanted not in json.dumps(schema):
                offenders.append(f"{method.upper()} {path} -> {status}: does not reference {wanted}")
    if offenders:
        return Finding(False, f"{len(offenders)} response(s) not bound to the canonical envelope",
                       _capped(sorted(offenders)), len(offenders))
    return Finding(True, "every JSON response references the common.v1 envelope", count=0)


def operation_ids_governed(repo: ServiceRepo) -> Finding:
    """API-005: every operation has a unique operationId, and the id set is locked."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    missing, seen, duplicates = [], set(), []
    for path, method, op in repo.operations():
        oid = op.get("operationId")
        if not oid:
            missing.append(f"{method.upper()} {path}: no operationId")
            continue
        if oid in seen:
            duplicates.append(f"{method.upper()} {path}: duplicate operationId {oid!r}")
        seen.add(oid)
    problems = sorted(missing) + sorted(duplicates)
    lock = repo.root / "docs" / "openapi.operationids.lock.json"
    if not lock.is_file() and repo.operations():
        problems.append("docs/openapi.operationids.lock.json is absent: operationIds are a public "
                        "API surface and must be semver-governed")
    if problems:
        return Finding(False, f"{len(missing)} missing, {len(duplicates)} duplicate, "
                              f"lock {'present' if lock.is_file() else 'absent'}",
                       _capped(problems), len(problems))
    return Finding(True, f"{len(seen)} operationIds present, unique, and locked", count=0)


def canonical_components_current(repo: ServiceRepo) -> Finding:
    """API-007: the spec's common.v1 components match the proto projection exactly.

    API-004 proves a response POINTS AT the envelope. This proves the envelope it points
    at is the CURRENT one. The distinction matters because each service projects the
    components from its own platform-contracts-go pin, so two services can both pass every
    in-repo gate while publishing structurally different common.v1.Meta - each internally
    consistent, which is all a per-repo drift check can ever establish.
    """
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    reference, ref_version = load_canonical_components()
    if reference is None:
        return Finding(True, "no reference artifact available; skipped", count=0)
    schemas = ((repo.spec or {}).get("components") or {}).get("schemas") or {}
    published = {k: v for k, v in schemas.items() if k.startswith("common.v1.")}
    if not published:
        # Pre-migration service on a bespoke envelope. API-004 already owns that failure;
        # reporting it twice would double-count the same debt in two baselines.
        return Finding(True, "no common.v1 components published; API-004 governs adoption", count=0)
    problems = []
    for name in sorted(set(reference) | set(published)):
        want, got = reference.get(name), published.get(name)
        if want is None:
            continue  # a service may publish extra components of its own
        if got is None:
            problems.append(f"{name}: missing from the spec but present in {ref_version}")
        elif json.dumps(got, sort_keys=True) != json.dumps(want, sort_keys=True):
            problems.append(f"{name}: differs from the {ref_version} proto projection")
    if problems:
        return Finding(False, f"{len(problems)} component(s) stale against {ref_version}",
                       _capped(problems), len(problems))
    return Finding(True, f"common.v1 components match the {ref_version} proto projection", count=0)


def no_swaggo_annotation_source(repo: ServiceRepo) -> Finding:
    """API-006: no swaggo annotation source; the engine generates from Go types."""
    offenders = [str(rel) for rel, _ in repo.go_sources if rel.name == "swagger_main.go"]
    if offenders:
        return Finding(False, f"{len(offenders)} swaggo annotation source file(s)",
                       [f"{o}: superseded by the reflection engine (ADR-0048) and the "
                        "no-runtime-docs decision (ADR-0067)" for o in offenders], len(offenders))
    return Finding(True, "no swaggo annotation source", count=0)


DETECTORS = {
    "no_runtime_docs": no_runtime_docs,
    "single_committed_spec": single_committed_spec,
    "shared_conformance_suite": shared_conformance_suite,
    "canonical_envelope": canonical_envelope,
    "canonical_components_current": canonical_components_current,
    "operation_ids_governed": operation_ids_governed,
    "no_swaggo_annotation_source": no_swaggo_annotation_source,
}


# --- catalog + evaluation ------------------------------------------------------------------

def load_controls(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"::error::cannot read control catalog {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: invalid control catalog (expected a 'controls:' list)")
    errors, seen = [], set()
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
        if c.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {c.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for e in errors:
            print(f"::error::api-contract catalog invalid: {e}")
        raise SystemExit(2)
    return doc


def evaluate(repo: ServiceRepo, controls: list) -> list:
    results = []
    for c in controls:
        rec = {"control": c["id"], "title": c["title"], "severity": c["severity"],
               "scope": c["scope"], "applies_when": c["applies_when"], "owner": c["owner"],
               "status": c["status"], "result": None, "evidence": "", "details": [],
               "count": 0, "baseline": repo.baseline.get(c["id"], 0), "remediation": ""}
        if c["status"] != "active":
            rec.update(result="skipped", evidence=f"lifecycle status={c['status']} (not evaluated)")
        elif c["applies_when"] == "http-api" and not repo.has_http_api():
            rec.update(result="skipped", evidence="no docs/openapi.json; not an HTTP API service")
        else:
            f = DETECTORS[c["detector"]](repo)
            rec.update(count=f.count, evidence=f.evidence, details=f.details)
            # Ratchet: a frozen count is tolerated, a rise is not, a fall is celebrated.
            if f.count > rec["baseline"]:
                rec.update(result="fail",
                           remediation=" ".join(str(c["remediation"]).split()))
            elif f.count < rec["baseline"]:
                rec.update(result="improved")
            elif f.count > 0:
                rec.update(result="frozen")
            else:
                rec.update(result="pass")
        results.append(rec)
    return results


def is_enforced(rec: dict, threshold: int) -> bool:
    return rec["result"] == "fail" and SEVERITY_ORDER[rec["severity"]] >= threshold


MARK = {"pass": "ok", "fail": "XX", "frozen": "==", "improved": "->", "skipped": "--"}


def render_text(repo: ServiceRepo, results: list, threshold: int) -> None:
    print(f"::group::api-contract: {repo.name}")
    for r in results:
        line = f"[{MARK[r['result']]}] {r['control']} [{r['severity']}] {r['title']}: {r['evidence']}"
        if r["result"] in ("frozen", "improved"):
            line += f" (baseline {r['baseline']}, now {r['count']})"
        print("  " + line)
    print("::endgroup::")
    for r in results:
        if r["result"] == "fail":
            level = "error" if is_enforced(r, threshold) else "warning"
            rose = f" - rose from baseline {r['baseline']} to {r['count']}" if r["baseline"] else ""
            print(f"::{level}::[{r['control']}][{r['severity']}] {r['title']}{rose}: {r['evidence']}")
            for d in r["details"]:
                print(f"    - {d}")
            if r["remediation"]:
                print(f"    remediation: {r['remediation']}")
        elif r["result"] == "improved":
            print(f"::notice::[{r['control']}] improved from {r['baseline']} to {r['count']} - "
                  f"run --write-baseline to lock the gain in")


def build_report(repo: ServiceRepo, results: list, policy_ssot: list, fail_on: str, threshold: int) -> dict:
    return {
        "service": repo.name,
        "root": str(repo.root),
        "policy_ssot": policy_ssot,
        "fail_on": fail_on,
        "http_api": repo.has_http_api(),
        "baseline_present": bool(repo.baseline),
        "controls_total": len(results),
        "passed": sum(1 for r in results if r["result"] == "pass"),
        "frozen": sum(1 for r in results if r["result"] == "frozen"),
        "improved": sum(1 for r in results if r["result"] == "improved"),
        "failed": sum(1 for r in results if r["result"] == "fail"),
        "skipped": sum(1 for r in results if r["result"] == "skipped"),
        "enforced_failures": sum(1 for r in results if is_enforced(r, threshold)),
        "open_violations": {r["control"]: r["count"] for r in results if r["count"]},
        "ok": not any(is_enforced(r, threshold) for r in results),
        "results": results,
    }


def write_baseline(repo: ServiceRepo, results: list) -> int:
    counts = {r["control"]: r["count"] for r in results if r["count"]}
    path = repo.root / BASELINE_FILE
    # A compliant service carries no debt artifact. Removing the file rather than writing an
    # empty one keeps "no baseline means held to zero" the visible default, and makes the
    # last baseline deletion in a repo the moment its migration is provably finished.
    if not counts:
        if path.is_file():
            path.unlink()
            print(f"api-contract: {repo.name} is clean - removed {BASELINE_FILE}")
        else:
            print(f"api-contract: {repo.name} is clean - no baseline needed")
        return 0
    payload = {
        "_comment": "Frozen api-contract debt for this service. The gate fails if any count "
                    "RISES; lower it by fixing violations, then re-run --write-baseline. "
                    "Raising a number here is a reviewable, deliberate act.",
        "controls": counts,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"api-contract: wrote {path} ({sum(counts.values())} violation(s) frozen "
          f"across {len(counts)} control(s))")
    return 0


def render_markdown(doc: dict) -> str:
    rows = ["| Control | Policy | Severity | Scope | Applies | Owner | Status |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        rows.append(f"| {c['id']} | {policy} | {c['severity']} | {c['scope']} | "
                    f"{c['applies_when']} | {c['owner']} | {c['status']} |")
    return "\n".join(rows)


def _docs_block(doc: dict) -> str:
    return (f"{DOCS_BEGIN}\n\n_Generated from `controls/api-contract.yaml` by "
            f"`scripts/check-api-contract.py --write-docs` — do not edit by hand._\n\n"
            f"{render_markdown(doc)}\n\n{DOCS_END}")


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        raise SystemExit(f"::error::{path}: missing {DOCS_BEGIN} / {DOCS_END} markers")
    head, _, rest = text.partition(DOCS_BEGIN)
    _, _, tail = rest.partition(DOCS_END)
    path.write_text(head + _docs_block(doc) + tail, encoding="utf-8")
    print(f"api-contract: wrote generated control table into {path}")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        raise SystemExit(f"::error::{path}: missing {DOCS_BEGIN} / {DOCS_END} markers")
    head, _, rest = text.partition(DOCS_BEGIN)
    _, _, tail = rest.partition(DOCS_END)
    if head + _docs_block(doc) + tail != text:
        print(f"::error::{path}: control table drifted from controls/api-contract.yaml "
              "- run scripts/check-api-contract.py --write-docs")
        return 1
    print(f"api-contract: {path} control table is in sync with the catalog")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Service API-contract guard (executable form of "
                                             "ADR-0048 + ADR-0067 + RFC-0001).")
    ap.add_argument("roots", nargs="*", default=["."], help="service repository roots to check")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--write-baseline", action="store_true",
                    help="freeze current violation counts into .api-contract-baseline.json")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv)

    doc = load_controls(Path(args.controls))
    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_markdown(doc))
        return 0

    threshold = SEVERITY_ORDER[args.fail_on]
    controls = doc["controls"]

    # Refuse to run a control whose reference artifact is missing rather than let it report a
    # pass it never actually checked.
    if any(c.get("detector") == "canonical_components_current" for c in controls):
        if load_canonical_components()[0] is None:
            print(f"::error::API-007 is enabled but {CANONICAL_COMPONENTS} is missing or "
                  "unreadable. Check out this repository's controls/ directory, or regenerate "
                  "the artifact from platform-shared-go with `go run "
                  "./platform/openapicontract/commonv1policy/cmd/emit-canonical-components`.")
            return 2
    reports, failed = [], False
    for raw in (args.roots or ["."]):
        root = Path(raw).resolve()
        if not root.is_dir():
            print(f"::error::not a directory: {root}")
            return 2
        repo = ServiceRepo(root)
        results = evaluate(repo, controls)
        if args.write_baseline:
            write_baseline(repo, results)
            continue
        report = build_report(repo, results, doc.get("policy_ssot", []), args.fail_on, threshold)
        reports.append(report)
        if args.format == "text":
            render_text(repo, results, threshold)
        failed = failed or not report["ok"]

    if args.write_baseline:
        return 0
    payload = reports[0] if len(reports) == 1 else {"repos": reports,
                                                    "ok": all(r["ok"] for r in reports)}
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
