#!/usr/bin/env python3
"""Precision/recall fixtures for check-frontend-sdk-contract.py.

A conformant app (lock + spec digest + published registry document + generator
that does not mention docs-json) must produce ZERO findings. A dirty tree must
still fire FE-SDK-0001..0004 — a silent pass on known-bad is the detector dying.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_frontend_sdk_contract",
    ROOT / "scripts" / "check-frontend-sdk-contract.py",
)
gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(gate)


SPEC_BODY = b'{"openapi":"3.0.3","info":{"title":"fixture","version":"1.0.0"}}'
SPEC_DIGEST = "sha256:" + hashlib.sha256(SPEC_BODY).hexdigest()


def write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)


def make_registry(base: Path, service: str = "org-bff", version: str = "v6.0.0") -> Path:
    published = base / service / f"{version}.json"
    write(published, SPEC_BODY)
    write(base / "index.json", '{"apiVersion":"contracts.inboxxhq.com/v1"}')
    return base


def make_app(
    base: Path,
    *,
    generate_script: bool = True,
    lock: bool = True,
    spec: bool = True,
    generated: bool = True,
    docs_json: bool = False,
    digest: str | None = None,
) -> Path:
    write(
        base / "package.json",
        json.dumps({"name": "fixture-app", "scripts": {"generate:api": "node scripts/generate-api-sdk.js"}}),
    )
    script = (
        "fetch('http://localhost:4000/api/docs-json/org-bff')\n"
        if docs_json
        else "import './generate-from-lock.js';\n"
    )
    if generate_script:
        write(base / "scripts" / "generate-api-sdk.js", script)
    if lock:
        write(
            base / "api-contract.lock.json",
            json.dumps(
                {
                    "service": "org-bff",
                    "version": "v6.0.0",
                    "spec_file": "openapi-org-bff.json",
                    "spec_digest": digest or SPEC_DIGEST,
                    "generated_dir": "src/app/core/api/generated/org-bff",
                }
            ),
        )
    if spec:
        write(base / "openapi-org-bff.json", SPEC_BODY)
    if generated:
        write(
            base / "src" / "app" / "core" / "api" / "generated" / "org-bff" / "index.ts",
            "export {};\n",
        )
    return base


class FrontendSdkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = gate.load_controls(gate.DEFAULT_CONTROLS)

    def ids(self, findings: list[dict]) -> set[str]:
        return {f["id"] for f in findings}

    def test_catalog_binds_every_active_control(self) -> None:
        ids = {c["id"] for c in self.catalog["controls"]}
        self.assertEqual(ids, {"FE-SDK-0001", "FE-SDK-0002", "FE-SDK-0003", "FE-SDK-0004"})
        for control in self.catalog["controls"]:
            self.assertEqual(control["status"], "active", control["id"])
            self.assertIn(control["detector"], gate.DETECTORS, control["id"])

    def test_skip_when_app_does_not_generate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "www"
            write(root / "package.json", json.dumps({"name": "marketing-site", "scripts": {"build": "next build"}}))
            findings = gate.evaluate(root, None, self.catalog)
            self.assertEqual(findings, [])

    def test_conformant_app_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = make_app(tmp_path / "app")
            registry = make_registry(tmp_path / "registry")
            findings = gate.evaluate(app, registry, self.catalog)
            self.assertEqual(findings, [], findings)

    def test_missing_lock_fires_0001(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp) / "app", lock=False)
            findings = gate.evaluate(app, Path(tmp) / "registry", self.catalog)
            self.assertIn("FE-SDK-0001", self.ids(findings))

    def test_stale_spec_fires_0002(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = make_app(tmp_path / "app")
            (app / "openapi-org-bff.json").write_bytes(b'{"openapi":"3.0.3","info":{"title":"stale"}}')
            registry = make_registry(tmp_path / "registry")
            findings = gate.evaluate(app, registry, self.catalog)
            self.assertIn("FE-SDK-0002", self.ids(findings))

    def test_lock_digest_not_in_registry_fires_0002(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = make_app(tmp_path / "app")
            registry = make_registry(tmp_path / "registry")
            (registry / "org-bff" / "v6.0.0.json").write_bytes(b'{"openapi":"3.0.3","info":{"title":"other"}}')
            findings = gate.evaluate(app, registry, self.catalog)
            self.assertIn("FE-SDK-0002", self.ids(findings))

    def test_docs_json_generator_fires_0003(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = make_app(tmp_path / "app", docs_json=True)
            registry = make_registry(tmp_path / "registry")
            findings = gate.evaluate(app, registry, self.catalog)
            self.assertIn("FE-SDK-0003", self.ids(findings))

    def test_missing_generated_dir_fires_0004(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = make_app(tmp_path / "app", generated=False)
            registry = make_registry(tmp_path / "registry")
            findings = gate.evaluate(app, registry, self.catalog)
            self.assertIn("FE-SDK-0004", self.ids(findings))


if __name__ == "__main__":
    unittest.main()
