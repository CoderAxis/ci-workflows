#!/usr/bin/env python3
"""UUID version-policy gate: UUIDv7 everywhere, deterministic v3/v5 only where a
documented reason exists.

The policy is a sentence in ADR-0071 and outbox-ddl-standard section 2.1 that
nothing verified. This is the verifier. It is split in two on purpose:

  controls/uuid-policy.yaml  the policy: which sinks have a documented required
                             version, which reasons are sanctioned grounds for a
                             deterministic exception, and which stage each
                             control is at.
  tools/uuidscan             the facts: a syntax-only go/ast scan that reports
                             constructor sites, sink assignments, function return
                             kinds and declaration markers, and decides nothing.

This script is the judge. Keeping parsing out of it is not tidiness: the policy's
own regression fixture defeats a text scan, because the FIXED voice-gateway
emitter quotes both `uuid.NewSHA1` and `uuid.Must(uuid.NewV7())` in a doc comment
explaining the bug it removed. A grep gate reports the fix as the defect. go/ast
puts comments and code in different places, so a declaration marker is read from
prose and a constructor only ever from syntax.

Exit codes follow the house convention:
  0  clean, or the repository has no Go source to scan
  1  at least one finding from a control at stage `enforce`
  2  the checker could not do its job (missing toolchain, unparseable catalog,
     scanner failure, or a Go repository that produced an empty scan)

Exit 2 exists because a checker that silently cannot run is worse than no
checker: it reports a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTROL = REPO_ROOT / "controls" / "uuid-policy.yaml"
DEFAULT_SCANNER = REPO_ROOT / "tools" / "uuidscan"

REQUIRED_CONTROL_FIELDS = (
    "id", "title", "owner", "scope", "status", "severity", "stage",
    "policy", "rationale", "remediation", "detector", "refs",
)
VALID_SEVERITY = {"critical", "major", "minor"}
VALID_STAGE = {"enforce", "warn", "observe"}
VALID_STATUS = {"active", "deprecated", "superseded"}

DETERMINISTIC_KINDS = {"deterministic-v5", "deterministic-v3"}
FRESH_KINDS = {"fresh-v7", "random-v4"}
KIND_VERSION = {"deterministic-v5": "v5", "deterministic-v3": "v3",
                "fresh-v7": "v7", "random-v4": "v4"}


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
        cannot_run(f"uuid-policy control catalog not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        cannot_run(f"cannot read the uuid-policy catalog at {path}: {exc}")
    if not isinstance(doc, dict):
        cannot_run(f"{path}: expected a mapping at the top level")

    errors: list[str] = []
    seen: set[str] = set()
    controls = doc.get("controls")
    if not isinstance(controls, list) or not controls:
        cannot_run(f"{path}: invalid catalog (expected a non-empty 'controls:' list)")
    for i, c in enumerate(controls):
        cid = (c or {}).get("id", f"#{i}")
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

    for key in ("constructors", "sinks", "reasons"):
        if not isinstance(doc.get(key), list) or not doc[key]:
            errors.append(f"missing or empty '{key}:' list")

    # Every sink must name a control that exists, or a violation would be
    # reported under an id nobody can look up.
    for sink in doc.get("sinks") or []:
        if sink.get("control") not in seen:
            errors.append(f"sink {sink.get('field')!r} names unknown control {sink.get('control')!r}")

    if errors:
        for e in errors:
            print(f"::error::uuid-policy catalog invalid: {e}")
        sys.exit(2)
    return doc


def controls_by_id(doc: dict) -> dict:
    return {c["id"]: c for c in doc["controls"]}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def repo_has_go(root: Path) -> bool:
    if (root / "go.mod").exists():
        return True
    for p in root.rglob("*.go"):
        parts = set(p.parts)
        if ".git" in parts or "vendor" in parts:
            continue
        return True
    return False


def run_scanner(root: Path, doc: dict, scanner_dir: Path, scanner_bin: str | None) -> dict:
    scan = doc.get("scan") or {}
    cfg = {
        "constructors": doc["constructors"],
        "unwrappers": doc.get("unwrappers") or [],
        "sinks": [{"field": s["field"], "types": s.get("types") or []} for s in doc["sinks"]],
        "marker_pattern": scan.get("marker_pattern", ""),
        "determinism_name_pattern": scan.get("determinism_name_pattern", ""),
        "skip_dirs": scan.get("skip_dirs") or [],
        "skip_test_files": bool(scan.get("skip_test_files", True)),
    }
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "uuidscan-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        if scanner_bin:
            argv = [scanner_bin]
        else:
            go = shutil.which("go")
            if not go:
                cannot_run(
                    "no Go toolchain on PATH and no --scanner-bin given; the scan is a go/ast "
                    "pass and cannot be substituted with a text search")
            argv = [go, "run", "."]

        env = dict(os.environ)
        # The scanner is stdlib-only, so it must never consult a module proxy or
        # a workspace file belonging to the repo under test.
        env["GOWORK"] = "off"
        env["GOFLAGS"] = "-mod=mod"
        env["GOPROXY"] = env.get("GOPROXY", "off")

        proc = subprocess.run(
            argv + ["-root", str(root), "-config", str(cfg_path)],
            cwd=str(scanner_dir) if not scanner_bin else None,
            capture_output=True, text=True, env=env,
        )
    if proc.returncode != 0:
        cannot_run(f"uuidscan failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        cannot_run(f"uuidscan produced no parseable report: {exc}")


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def type_matches(lit_type: str, accepted: list) -> bool:
    """A composite literal's spelled type is compared by last path component, so
    `outbox.Event`, `Event` and an aliased `ob.Event` all match. Without type
    information this is the strongest available constraint; it is what keeps the
    field-name match from firing on an unrelated struct."""
    if not accepted:
        return True
    got = lit_type.split(".")[-1].lstrip("*&")
    return any(got == a.split(".")[-1] for a in accepted)


def rule_sink_contract(report: dict, doc: dict, ctl: dict) -> list[Finding]:
    by_field = {s["field"]: s for s in doc["sinks"]}
    out: list[Finding] = []
    for sink in report.get("sinks") or []:
        rule = by_field.get(sink["field"])
        if rule is None:
            continue
        kind = sink.get("value_kind") or ""
        if kind not in set(rule.get("rejects") or []):
            continue
        if sink["site"] == "composite-literal" and not type_matches(sink.get("lit_type", ""), rule.get("types") or []):
            continue
        c = ctl[rule["control"]]
        if c["status"] != "active":
            continue
        via = f" through {sink['via']}()" if sink.get("via") else ""
        out.append(Finding(
            control=c["id"], severity=c["severity"], stage=c["stage"],
            file=sink["file"], line=sink["line"],
            message=(
                f"{sink['field']} (outbox column {rule['column']}) is assigned "
                f"`{sink['value_expr']}`, a {KIND_VERSION[kind]} value{via}, but "
                f"{rule['authority']} requires {KIND_VERSION[rule['requires']]}"
            ),
            remediation=c["remediation"], refs=c["refs"],
        ))
    return out


def rule_unused_parameter_determinism(report: dict, ctl: dict) -> list[Finding]:
    c = ctl["UUID-0003"]
    if c["status"] != "active":
        return []
    out: list[Finding] = []
    for fn in report.get("funcs") or []:
        if not fn.get("all_params_unused") or fn.get("params", 0) == 0:
            continue
        if fn.get("kind") not in FRESH_KINDS:
            continue
        # A method with unused parameters is usually satisfying an interface, so
        # it is reported one stage down rather than as a hard failure.
        stage = c["stage"] if not fn.get("recv") else "warn"
        recv = f"({fn['recv']}) " if fn.get("recv") else ""
        out.append(Finding(
            control=c["id"], severity=c["severity"], stage=stage,
            file=fn["file"], line=fn["line"],
            message=(
                f"{recv}{fn['name']} declares {fn['params']} parameter(s), references none of "
                f"them, and returns a freshly generated {KIND_VERSION[fn['kind']]} UUID. Every "
                f"call returns a different value, so no caller gets the derivation the signature "
                f"promises"
            ),
            remediation=c["remediation"], refs=c["refs"],
        ))
    return out


def rule_determinism_claim(report: dict, ctl: dict, already: set) -> list[Finding]:
    c = ctl["UUID-0004"]
    if c["status"] != "active":
        return []
    out: list[Finding] = []
    for fn in report.get("funcs") or []:
        if not fn.get("name_claims_determinism"):
            continue
        # "mixed" means at least one return path IS deterministic, which is the
        # legitimate shape (derive normally, fall back to a fresh id on empty
        # input). Only a function with no deterministic path at all is a lie.
        if fn.get("kind") not in FRESH_KINDS:
            continue
        if (fn["file"], fn["line"]) in already:
            continue  # UUID-0003 already reported this exact function
        out.append(Finding(
            control=c["id"], severity=c["severity"], stage=c["stage"],
            file=fn["file"], line=fn["line"],
            message=(
                f"{fn['name']} promises a derived value but every return path yields a fresh "
                f"{KIND_VERSION[fn['kind']]} UUID ({', '.join(fn.get('return_kinds') or [])}); "
                f"no return path derives anything from the inputs"
            ),
            remediation=c["remediation"], refs=c["refs"],
        ))
    return out


def deterministic_sink_functions(report: dict, doc: dict) -> set:
    """Function names whose value reaches a sink that documents a deterministic
    requirement. A constructor inside such a function is self-declaring: the
    reason is already written down in the ADR that governs the sink, so demanding
    a per-site marker there would be a suppression with no reader."""
    wanted = {s["field"] for s in doc["sinks"] if s.get("requires") in DETERMINISTIC_KINDS}
    names: set = set()
    lines: set = set()
    for sink in report.get("sinks") or []:
        if sink["field"] not in wanted:
            continue
        if sink.get("via"):
            names.add(sink["via"])
        lines.add((sink["file"], sink["line"]))
    return names, lines  # type: ignore[return-value]


def rule_undeclared_deterministic(report: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["UUID-0005"]
    if c["status"] != "active":
        return []
    reasons = {r["token"]: r for r in doc["reasons"]}
    self_declaring_funcs, sink_lines = deterministic_sink_functions(report, doc)
    markers = report.get("markers") or []
    out: list[Finding] = []
    for ctor in report.get("constructors") or []:
        if ctor["kind"] not in DETERMINISTIC_KINDS:
            continue
        if ctor.get("in_func") and ctor["in_func"] in self_declaring_funcs:
            continue
        if (ctor["file"], ctor["line"]) in sink_lines:
            continue
        idx = ctor.get("marker_idx", -1)
        if ctor.get("declared") and 0 <= idx < len(markers):
            m = markers[idx]
            want_version = KIND_VERSION[ctor["kind"]]
            # An unsanctioned reason token is not checked here: rule_orphan_marker
            # owns that, because it must hold for every marker including the ones
            # beside a self-declaring sink, and one owner means one diagnostic.
            if m.get("reason") not in reasons:
                pass
            elif m.get("version") != want_version:
                out.append(Finding(
                    control="UUID-0006", severity=ctl["UUID-0006"]["severity"],
                    stage=ctl["UUID-0006"]["stage"], file=m["file"], line=m["line"],
                    message=(
                        f"declaration says {m.get('version')} but the adjacent constructor mints "
                        f"{want_version} (`{ctor['expr']}`)"
                    ),
                    remediation=ctl["UUID-0006"]["remediation"], refs=ctl["UUID-0006"]["refs"],
                ))
            elif reasons[m["reason"]].get("adr") != m.get("adr"):
                out.append(Finding(
                    control="UUID-0006", severity=ctl["UUID-0006"]["severity"],
                    stage=ctl["UUID-0006"]["stage"], file=m["file"], line=m["line"],
                    message=(
                        f"declaration cites {m.get('adr')} for reason={m['reason']}, but the "
                        f"sanctioned authority for that reason is "
                        f"{reasons[m['reason']].get('adr')}"
                    ),
                    remediation=ctl["UUID-0006"]["remediation"], refs=ctl["UUID-0006"]["refs"],
                ))
            continue
        where = f" in {ctor['in_func']}()" if ctor.get("in_func") else ""
        out.append(Finding(
            control=c["id"], severity=c["severity"], stage=c["stage"],
            file=ctor["file"], line=ctor["line"],
            message=(
                f"`{ctor['expr']}`{where} mints a deterministic "
                f"{KIND_VERSION[ctor['kind']]} UUID with no documented reason, and its value does "
                f"not reach a sink whose deterministic requirement is already written down"
            ),
            remediation=c["remediation"], refs=c["refs"],
        ))
    return out


def rule_orphan_marker(report: dict, doc: dict, ctl: dict) -> list[Finding]:
    c = ctl["UUID-0006"]
    if c["status"] != "active":
        return []
    reasons = {r["token"] for r in doc["reasons"]}
    out: list[Finding] = []
    for m in report.get("markers") or []:
        if m.get("covers", 0) == 0:
            out.append(Finding(
                control=c["id"], severity=c["severity"], stage=c["stage"],
                file=m["file"], line=m["line"],
                message=(
                    f"declaration `{m['raw']}` sits beside no deterministic UUID constructor; the "
                    f"code it excused is gone or has moved, so the excuse is stale"
                ),
                remediation=c["remediation"], refs=c["refs"],
            ))
        elif m.get("reason") not in reasons:
            out.append(Finding(
                control=c["id"], severity=c["severity"], stage=c["stage"],
                file=m["file"], line=m["line"],
                message=(
                    f"declaration cites reason={m.get('reason')!r}, which is not in the sanctioned "
                    f"vocabulary {sorted(reasons)}"
                ),
                remediation=c["remediation"], refs=c["refs"],
            ))
    return out


def evaluate(report: dict, doc: dict) -> list[Finding]:
    ctl = controls_by_id(doc)
    findings = rule_sink_contract(report, doc, ctl)
    u3 = rule_unused_parameter_determinism(report, ctl)
    findings += u3
    findings += rule_determinism_claim(report, ctl, {(f.file, f.line) for f in u3})
    findings += rule_undeclared_deterministic(report, doc, ctl)
    findings += rule_orphan_marker(report, doc, ctl)
    # One finding per control per site: a reviewer needs the site once, not once
    # per rule that happened to reach it.
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

def emit_text(report: dict, findings: list[Finding]) -> int:
    enforced = [f for f in findings if f.stage == "enforce"]
    warned = [f for f in findings if f.stage == "warn"]

    for f in enforced:
        print(f"::error file={f.file},line={f.line}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")
    for f in warned:
        print(f"::warning file={f.file},line={f.line}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")

    for err in report.get("parse_errors") or []:
        print(f"::warning::uuidscan could not parse {err}")

    scanned = (f"{report.get('files_scanned', 0)} Go file(s) in "
               f"{report.get('packages_scanned', 0)} package(s)")
    ctors = len([c for c in report.get('constructors') or [] if c['kind'] in DETERMINISTIC_KINDS])
    if enforced:
        print(f"uuid-version-policy: FAILED - {len(enforced)} enforced violation(s), "
              f"{len(warned)} advisory, {scanned} scanned, {ctors} deterministic constructor "
              f"site(s) inventoried.")
        return 1
    print(f"uuid-version-policy: OK - no enforced violations, {len(warned)} advisory, "
          f"{scanned} scanned, {ctors} deterministic constructor site(s) inventoried.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".", help="repository to check")
    ap.add_argument("--control", default=str(DEFAULT_CONTROL), help="uuid-policy control catalog")
    ap.add_argument("--scanner", default=str(DEFAULT_SCANNER), help="tools/uuidscan source dir")
    ap.add_argument("--scanner-bin", default=os.environ.get("UUIDSCAN_BIN") or None,
                    help="prebuilt uuidscan binary (skips 'go run')")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--report", help="write the raw scanner report plus findings here")
    args = ap.parse_args()

    root = Path(args.repo_root).resolve()
    if not root.is_dir():
        cannot_run(f"--repo-root {root} is not a directory")
    doc = load_catalog(Path(args.control).resolve())

    if not repo_has_go(root):
        print("uuid-version-policy: no Go source in this repository; skipping.")
        return 0

    report = run_scanner(root, doc, Path(args.scanner).resolve(), args.scanner_bin)
    if report.get("files_scanned", 0) == 0:
        cannot_run(
            f"{root} looks like a Go repository but uuidscan parsed 0 files; refusing to report a "
            f"pass from an empty scan")

    findings = evaluate(report, doc)

    if args.report:
        Path(args.report).write_text(json.dumps({
            "root": str(root),
            "scan": {k: report.get(k) for k in ("files_scanned", "packages_scanned", "module")},
            "findings": [f.__dict__ for f in findings],
        }, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps({"findings": [f.__dict__ for f in findings],
                          "scan": {k: report.get(k) for k in
                                   ("files_scanned", "packages_scanned", "module")}}, indent=2))
        return 1 if any(f.stage == "enforce" for f in findings) else 0

    return emit_text(report, findings)


if __name__ == "__main__":
    raise SystemExit(main())
