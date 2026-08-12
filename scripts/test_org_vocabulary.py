#!/usr/bin/env python3
"""Self-test for the org-versus-tenant vocabulary gate.

What is pinned here, and why it matters:
  STAGE BEHAVIOUR  enforce exits 1 and emits ::error::; warn exits 0 and emits
                   ::warning::; observe exits 0 and emits neither, counting
                   only in the summary. This is the bug that shipped broken
                   (stage: warn still exited non-zero) and needs test cover so
                   it cannot regress silently.
  EXCEPTION PRECISION  a declared exception suppresses a finding; an undeclared
                       one does not. Tested against an enforce-stage control so
                       the distinction matters: the control's stage is now read
                       and honoured, not the hardcoded "enforce" default.
  HARNESS EXCLUSION  a ci-workflows checkout placed inside the caller tree is
                     excluded from scanning; a planted violation in the caller's
                     own source is still found. Tests this against a real repo
                     copy so a synthetic /tmp directory cannot paper over a
                     path that actually varies in CI.

A test that passes only because stage is warn - and therefore every finding is
advisory - provides no recall assurance. All stage-sensitive tests carry their
own minimal control file with the desired stage; they do not rely on the
production controls/org-vocabulary.yaml value.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check-org-vocabulary.py"

FAILURES: list[str] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def run_checker(root: Path, control: Path | None = None) -> tuple[int, str]:
    """Run the org vocabulary checker on the given root directory."""
    cmd = [sys.executable, str(CHECKER), "--repo-root", str(root)]
    if control is not None:
        cmd += ["--control", str(control)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _write_control(dest: Path, stage: str, exceptions: list | None = None) -> Path:
    """Write a minimal, self-consistent control YAML to dest (a file path).

    dest must be OUTSIDE the scan root, otherwise the control file itself
    (which contains "tenant" in its policy/remediation text) would be scanned
    and produce spurious findings.
    """
    doc = {
        "version": 1,
        "domain": "org-vocabulary-policy",
        "policy_ssot": ["docs/standard.md"],
        "exceptions": exceptions or [],
        "scan": {"skip_dirs": [], "skip_test_files": False},
        "controls": [{
            "id": "ORG-0001",
            "title": "Canonical vocabulary check",
            "owner": "platform-architecture",
            "scope": "source",
            "status": "active",
            "severity": "major",
            "stage": stage,
            "policy": "Canonical scope term is org.",
            "rationale": "Historical synonym is being normalized.",
            "remediation": "Replace the legacy term with org.",
            "detector": "usage_check",
            "refs": ["docs/standard.md"],
        }],
    }
    dest.write_text(yaml.dump(doc), encoding="utf-8")
    return dest


def _write_violation(root: Path, name: str = "service.py") -> Path:
    """Write a file with a clear legacy-term identifier violation."""
    f = root / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "def get_tenant_data(tenant_id: str):\n"
        "    return tenant_id\n",
        encoding="utf-8",
    )
    return f


class _Scratch:
    """Two-directory scratch: one for the repo to scan, one for control files.

    Using a single TemporaryDirectory and putting the control file at
    scratch.ctl rather than inside scratch.repo keeps the control file's
    'tenant' policy prose out of the scan, which would otherwise add spurious
    findings and confuse stage/exception tests.
    """
    def __init__(self, base: Path) -> None:
        self.repo = base / "repo"
        self.repo.mkdir()
        self.ctl_dir = base / "ctl"
        self.ctl_dir.mkdir()

    def control(self, stage: str, exceptions: list | None = None) -> Path:
        return _write_control(self.ctl_dir / f"{stage}.yaml", stage, exceptions)


# ---------------------------------------------------------------------------
# Stage behaviour
# ---------------------------------------------------------------------------

def test_enforce_stage_fails_ci() -> None:
    """stage: enforce -> exit 1, ::error:: annotation."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo)
        ctl = s.control("enforce")
        code, out = run_checker(s.repo, ctl)
        expect(code == 1,
               f"enforce stage must exit 1, got {code};\n{out}")
        expect("::error" in out,
               f"enforce stage must emit ::error::;\n{out}")
        expect("::warning" not in out,
               f"enforce stage must not emit ::warning::;\n{out}")
        expect("FAILED" in out,
               f"enforce stage must print FAILED;\n{out}")


def test_warn_stage_exits_zero() -> None:
    """stage: warn -> exit 0, ::warning:: annotation, no ::error::."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo)
        ctl = s.control("warn")
        code, out = run_checker(s.repo, ctl)
        expect(code == 0,
               f"warn stage must exit 0 even with findings, got {code};\n{out}")
        expect("::warning" in out,
               f"warn stage must emit ::warning::;\n{out}")
        expect("::error" not in out,
               f"warn stage must not emit ::error::;\n{out}")
        expect("OK" in out,
               f"warn stage must print OK;\n{out}")


def test_observe_stage_exits_zero_no_annotations() -> None:
    """stage: observe -> exit 0, no ::error:: or ::warning::, count in summary."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo)
        ctl = s.control("observe")
        code, out = run_checker(s.repo, ctl)
        expect(code == 0,
               f"observe stage must exit 0, got {code};\n{out}")
        expect("::error" not in out,
               f"observe stage must not emit ::error::;\n{out}")
        expect("::warning" not in out,
               f"observe stage must not emit ::warning::;\n{out}")
        # Summary must still count the findings
        expect("observed" in out,
               f"observe stage must mention observed count in summary;\n{out}")


def test_stage_field_in_production_control_is_warn() -> None:
    """The production control is at stage: warn; violations must exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo)
        code, out = run_checker(s.repo)   # uses controls/org-vocabulary.yaml
        expect(code == 0,
               f"production stage: warn must exit 0, got {code};\n{out}")
        expect("::error" not in out,
               f"production warn stage must not emit ::error::;\n{out}")


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------

def test_declared_exception_suppresses_finding() -> None:
    """A path listed in exceptions must produce zero findings for that file."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo, "service.py")
        ctl = s.control("enforce", exceptions=[{
            "path": "service.py",
            "reason": "test exception",
        }])
        code, out = run_checker(s.repo, ctl)
        expect(code == 0,
               f"excepted file must not produce findings, got exit {code};\n{out}")
        expect("::error" not in out,
               f"excepted file must not emit ::error::;\n{out}")


def test_undeclared_usage_is_flagged() -> None:
    """A file not in exceptions must produce a finding at enforce stage."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        _write_violation(s.repo, "handler.go")
        ctl = s.control("enforce")
        code, out = run_checker(s.repo, ctl)
        expect(code == 1,
               f"undeclared usage must fail enforce, got {code};\n{out}")
        expect("handler.go" in out,
               f"finding must name the file;\n{out}")


def test_pattern_exception_is_matched_by_content() -> None:
    """An exception with a pattern key must only apply when the pattern matches the file."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        # File that contains the exempted phrase
        exempt = s.repo / "docs" / "arch.md"
        exempt.parent.mkdir()
        exempt.write_text("Multi-tenancy is the architectural pattern used here.\n",
                          encoding="utf-8")
        # File that contains the term but NOT the exempted phrase
        flagged = s.repo / "handler.go"
        flagged.write_text("func GetTenantData(tenantID string) {}\n",
                           encoding="utf-8")
        ctl = s.control("enforce", exceptions=[{
            "path": "docs/**/*.md",
            "pattern": "multi-tenant|multi-tenancy",
            "reason": "architectural concept prose",
        }])
        code, out = run_checker(s.repo, ctl)
        expect(code == 1,
               f"handler.go must still fail; exit was {code};\n{out}")
        expect("handler.go" in out,
               f"handler.go violation must be reported;\n{out}")
        expect("arch.md" not in out,
               f"docs/arch.md must be silenced by pattern exception;\n{out}")


# ---------------------------------------------------------------------------
# Harness exclusion
# ---------------------------------------------------------------------------

def test_harness_checkout_is_excluded_from_caller_scan() -> None:
    """ci-workflows, when checked out as .org-tools inside a caller repo, must
    not be scanned. The checker identifies its own checkout via REPO_ROOT
    (Path(__file__).parent.parent), so the exclusion works regardless of the
    checkout directory name.

    This test builds a real-copy scenario rather than a synthetic /tmp repo:
    it copies the actual ci-workflows tree into a temp caller directory and
    plants a violation in the caller's own source, then verifies (a) ci-workflows
    files are not reported and (b) the caller violation is still found.
    """
    with tempfile.TemporaryDirectory() as tmp:
        caller = Path(tmp) / "caller-repo"
        caller.mkdir()

        # Plant a violation in the caller's own source
        (caller / "handler.go").write_text(
            "func HandleTenantRequest(tenantID string) {}\n",
            encoding="utf-8",
        )

        # Copy the real ci-workflows tree into the caller as .org-tools/
        harness = caller / ".org-tools"
        shutil.copytree(str(REPO), str(harness), ignore=shutil.ignore_patterns(".git"))

        # The harness checker is now at .org-tools/scripts/check-org-vocabulary.py.
        # When that script resolves REPO_ROOT it gets caller/.org-tools, which is
        # inside caller - so excluded_paths() returns [caller/.org-tools].
        harness_checker = harness / "scripts" / "check-org-vocabulary.py"
        harness_control = harness / "controls" / "org-vocabulary.yaml"

        proc = subprocess.run(
            [sys.executable, str(harness_checker),
             "--repo-root", str(caller),
             "--control", str(harness_control)],
            capture_output=True, text=True,
        )
        out = proc.stdout + proc.stderr

        # The harness checkout must be announced as skipped
        expect("[skip]" in out,
               f"must announce skipping harness checkout;\n{out}")

        # The caller's handler.go violation must be found (warn stage → advisory)
        expect("handler.go" in out,
               f"caller violation must be reported;\n{out}")

        # None of our own files (controls/org-vocabulary.yaml contains "tenant"
        # extensively in its policy text) should be reported
        expect("org-vocabulary.yaml" not in out.split("[skip]")[0] or
               "org-vocabulary.yaml" in out.split("[skip]")[0].split("::")[0],
               "org-vocabulary.yaml must not appear in findings (harness excluded);\n" + out)
        # Simpler: no finding should name a path under .org-tools/
        org_tools_findings = [line for line in out.splitlines()
                              if ".org-tools" in line and "::" in line]
        expect(not org_tools_findings,
               f"no finding should name .org-tools paths;\n" +
               "\n".join(org_tools_findings))


# ---------------------------------------------------------------------------
# Operability
# ---------------------------------------------------------------------------

def test_empty_repository_exits_zero() -> None:
    """A repo with no scannable files must exit 0 cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        (s.repo / "README.md").write_text("# Empty repo\n", encoding="utf-8")
        ctl = s.control("enforce")
        code, out = run_checker(s.repo, ctl)
        expect(code == 0,
               f"clean repo must exit 0, got {code};\n{out}")
        expect("::error" not in out,
               f"clean repo must not emit ::error::;\n{out}")


def test_binary_files_are_not_scanned() -> None:
    """Files with binary-like extensions must be skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        (s.repo / "app.exe").write_bytes(b"\x00\x01\x02tenant\x03\x04")
        (s.repo / "config.yml").write_text("org_id: 12345\n", encoding="utf-8")
        ctl = s.control("enforce")
        code, out = run_checker(s.repo, ctl)
        expect(code == 0,
               f"binary-only repo must exit 0, got {code};\n{out}")


def test_catalog_file_is_valid_and_self_consistent() -> None:
    """The production catalog must pass its own validation."""
    catalog_path = REPO / "controls" / "org-vocabulary.yaml"
    expect(catalog_path.exists(), "catalog file must exist")
    try:
        doc = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        expect(False, f"catalog must be valid YAML: {exc}")
        return

    expect("controls" in doc, "catalog must have controls section")
    expect("exceptions" in doc, "catalog must have exceptions section")
    expect("version" in doc, "catalog must have version")

    controls = doc.get("controls", [])
    expect(len(controls) > 0, "must have at least one control")
    required = ["id", "title", "owner", "scope", "status", "severity",
                "stage", "policy", "rationale", "remediation", "detector", "refs"]
    for c in controls:
        for f in required:
            expect(f in c, f"control {c.get('id')} missing field: {f}")
        expect(c.get("stage") in ("enforce", "warn", "observe"),
               f"control {c.get('id')} has invalid stage: {c.get('stage')!r}")

    for i, exc in enumerate(doc.get("exceptions", [])):
        expect("path" in exc, f"exception {i} must have path")
        expect("reason" in exc, f"exception {i} must have reason")


def test_case_insensitive_detection() -> None:
    """tenant, Tenant, TENANT, tenantID - all variants must be detected."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _Scratch(Path(tmp))
        (s.repo / "test.go").write_text(
            "var TENANT_ID = \"x\"\n"
            "type TenantService struct{}\n"
            "func getTenant() string { return \"\" }\n",
            encoding="utf-8",
        )
        ctl = s.control("enforce")
        code, out = run_checker(s.repo, ctl)
        expect(code == 1,
               f"all case variants must be detected at enforce stage, got {code};\n{out}")
        expect(out.count("::error") >= 3,
               f"at least 3 findings expected (one per line); got {out.count('::error')};\n{out}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"org-vocabulary self-test: FAILED ({len(FAILURES)} assertion(s))")
        for f in FAILURES:
            print(f"::error::{f}")
        return 1

    print(
        f"org-vocabulary self-test: OK ({len(tests)} test(s)) - "
        f"all three stage behaviours verified (enforce/warn/observe), "
        f"exception precision verified at enforce stage, "
        f"harness exclusion verified against a real ci-workflows copy."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
