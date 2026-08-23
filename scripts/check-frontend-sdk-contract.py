#!/usr/bin/env python3
"""Enforce controls/frontend-sdk-contract.yaml against a frontend app repository.

This is the consumer half of the API-contract gate. Producer CI already proves a
service published a spec into inboxxhq-api-contracts. A browser app that generates
an HTTP SDK must pin one of those published versions and commit that exact document.
Fetching /api/docs-json at generate time is not a contract source (ADR-0067).

    python3 scripts/check-frontend-sdk-contract.py --root path/to/app \
        --registry path/to/inboxxhq-api-contracts

Exit 0 when every applicable control holds, 1 on violations, 2 on a bad invocation.
Apps that do not generate an HTTP SDK (no generate:api script, no lockfile) skip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "frontend-sdk-contract.yaml"
LOCK_NAME = "api-contract.lock.json"
DOCS_JSON_RE = re.compile(r"docs-json", re.IGNORECASE)
GENERATE_SCRIPT = Path("scripts") / "generate-api-sdk.js"


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_controls(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "controls" not in data:
        raise SystemExit(f"::error::{path} is not a control catalog")
    return data


def package_generates_api(root: Path) -> bool:
    pkg = root / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return False
    scripts = data.get("scripts") or {}
    return "generate:api" in scripts


def load_lock(root: Path) -> tuple[dict | None, str | None]:
    path = root / LOCK_NAME
    if not path.is_file():
        return None, None
    try:
        lock = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"api-contract.lock.json is not valid JSON: {exc}"
    if not isinstance(lock, dict):
        return None, "api-contract.lock.json must be an object"
    return lock, None


def check_lockfile_present(root: Path, lock: dict | None, lock_err: str | None) -> str | None:
    if lock_err:
        return lock_err
    if lock is None:
        return (
            "package.json defines generate:api but api-contract.lock.json is missing. "
            "Pin a published inboxxhq-api-contracts version."
        )
    required = ("service", "version", "spec_file", "spec_digest", "generated_dir")
    missing = [k for k in required if not isinstance(lock.get(k), str) or not lock[k]]
    if missing:
        return f"api-contract.lock.json missing string fields: {', '.join(missing)}"
    if not str(lock["spec_digest"]).startswith("sha256:"):
        return "spec_digest must be sha256:<hex>"
    return None


def check_spec_matches_published_pin(
    root: Path, lock: dict | None, registry: Path | None
) -> str | None:
    if not lock:
        return None
    spec = root / lock["spec_file"]
    if not spec.is_file():
        return f"pinned spec_file does not exist: {lock['spec_file']}"
    spec_digest = sha256_file(spec)
    if spec_digest != lock["spec_digest"]:
        return (
            f"{lock['spec_file']} digest {spec_digest} does not match lock "
            f"{lock['spec_digest']}. Copy the published document named in the lock."
        )
    if registry is None:
        return (
            "registry path is required to prove the pin. Pass --registry "
            "(CI checks out InboxxHQ-CoderAxis/inboxxhq-api-contracts)."
        )
    published = registry / lock["service"] / f"{lock['version']}.json"
    if not published.is_file():
        return f"published spec not in registry: {published}"
    published_digest = sha256_file(published)
    if published_digest != lock["spec_digest"]:
        return (
            f"lock spec_digest {lock['spec_digest']} does not match registry "
            f"{published} ({published_digest}). Bump the lock to a real published version."
        )
    return None


def check_no_runtime_docs_json_generator(root: Path, lock: dict | None) -> str | None:
    script = root / GENERATE_SCRIPT
    if not script.is_file():
        return f"{GENERATE_SCRIPT} is missing"
    text = script.read_text(errors="replace")
    if DOCS_JSON_RE.search(text):
        return (
            f"{GENERATE_SCRIPT} still references /api/docs-json. Generation must "
            "read api-contract.lock.json and the published registry."
        )
    return None


def check_generated_dir_present(root: Path, lock: dict | None) -> str | None:
    if not lock:
        return None
    generated = root / lock["generated_dir"]
    if not generated.is_dir():
        return f"generated_dir does not exist: {lock['generated_dir']}"
    if not any(generated.rglob("*.ts")):
        return f"generated_dir has no .ts files: {lock['generated_dir']}"
    return None


DETECTORS = {
    "lockfile_present": check_lockfile_present,
    "spec_matches_published_pin": check_spec_matches_published_pin,
    "no_runtime_docs_json_generator": check_no_runtime_docs_json_generator,
    "generated_dir_present": check_generated_dir_present,
}


def evaluate(root: Path, registry: Path | None, catalog: dict) -> list[dict]:
    findings: list[dict] = []
    generates = package_generates_api(root)
    lock, lock_err = load_lock(root)
    if not generates and lock is None:
        return findings

    for control in catalog["controls"]:
        if control.get("status") != "active":
            continue
        detector_name = control["detector"]
        fn = DETECTORS.get(detector_name)
        if fn is None:
            findings.append(
                {
                    "id": control["id"],
                    "severity": "critical",
                    "message": f"unknown detector {detector_name}",
                }
            )
            continue
        if detector_name == "spec_matches_published_pin":
            message = fn(root, lock, registry)
        elif detector_name == "lockfile_present":
            message = fn(root, lock, lock_err)
        else:
            message = fn(root, lock)
        if message:
            findings.append(
                {
                    "id": control["id"],
                    "severity": control.get("severity", "major"),
                    "title": control.get("title", ""),
                    "message": message,
                }
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="frontend app repository root")
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="checkout of inboxxhq-api-contracts (required for FE-SDK-0002)",
    )
    parser.add_argument(
        "--controls",
        type=Path,
        default=DEFAULT_CONTROLS,
        help="control catalog YAML",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    registry = args.registry.resolve() if args.registry else None
    catalog = load_controls(args.controls)

    if not package_generates_api(root) and not (root / LOCK_NAME).is_file():
        print("::notice::no generate:api script and no api-contract.lock.json; skipping frontend SDK contract")
        return 0

    findings = evaluate(root, registry, catalog)
    if not findings:
        print("frontend SDK contract: all applicable controls hold")
        return 0
    for finding in findings:
        print(
            f"::error::[{finding['id']}] {finding.get('title', '').strip()}: {finding['message']}"
        )
    print(f"frontend SDK contract: {len(findings)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
