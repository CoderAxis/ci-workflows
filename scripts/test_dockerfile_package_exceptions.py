#!/usr/bin/env python3
"""Self-test for DS-0018's runtime package exceptions. Run from the ci-workflows repo root:

    python3 scripts/test_dockerfile_package_exceptions.py

An exception (ADR-0073 Amendment 1) allows named runtime packages for ONE named repository.
Its whole value is in what it still refuses, so each direction is exercised: the listed
repository passes with its listed packages, and the same Dockerfile fails under any other
repository, with an unlisted package, or with the packages in the builder stage.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dockerfile_standard", HERE / "check-dockerfile-standard.py")
ds = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ds  # dataclasses resolve their module by name
spec.loader.exec_module(ds)  # type: ignore[union-attr]

FAILURES: list[str] = []

RUNTIME = """FROM --platform=$BUILDPLATFORM golang:1.26-alpine3.24 AS builder
RUN apk add --no-cache {builder}
FROM alpine:3.24
RUN apk add --no-cache {runtime}; addgroup -g 10001 -S appuser
USER 10001:10001
"""

CATALOG = {"inboxxhq-product-service": {"packages": ["tesseract-ocr", "tesseract-ocr-data-eng"], "adr": "ADR-0073"}}


def finding(repository: str, *, runtime: str, builder: str = "ca-certificates git tzdata") -> "ds.Finding":
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Dockerfile"
        path.write_text(RUNTIME.format(builder=builder, runtime=runtime), encoding="utf-8")
        exceptions = ds.load_package_exceptions({"runtime_package_exceptions": CATALOG}, path)
        df = ds.Dockerfile(path, Path(tmp), repository, exceptions)
        return ds.package_allowlist(df)


def expect(name: str, f: "ds.Finding", ok: bool, evidence: str) -> None:
    if f.ok != ok or evidence not in f.evidence:
        FAILURES.append(f"{name}: ok={f.ok} evidence={f.evidence!r}")
    else:
        print(f"  ok - {name}")


OCR = "ca-certificates tzdata wget tesseract-ocr tesseract-ocr-data-eng"

expect("the listed repository passes with its listed packages",
       finding("inboxxhq-product-service", runtime=OCR), True,
       "allowed for inboxxhq-product-service by its runtime_package_exceptions entry")
expect("an ordinary runtime install passes and names no exception",
       finding("inboxxhq-order-service", runtime="ca-certificates tzdata wget"), True,
       "within the approved allow-list")
expect("another repository with the same packages fails",
       finding("inboxxhq-order-service", runtime=OCR), False, "'tesseract-ocr'")
expect("no repository named at all fails",
       finding("", runtime=OCR), False, "'tesseract-ocr'")
expect("the listed repository with an unlisted package fails",
       finding("inboxxhq-product-service", runtime=OCR + " imagemagick"), False, "['imagemagick']")
expect("the listed packages in the builder stage fail",
       finding("inboxxhq-product-service", runtime="ca-certificates tzdata wget",
               builder="ca-certificates git tzdata tesseract-ocr"), False, "(stage 0)")

for bad, why in ((["x"], "a list"), ({"r": {"packages": []}}, "empty packages"),
                 ({"r": {"packages": ["p"]}}, "no adr"), ({"o/r": {"packages": ["p"], "adr": "ADR-1"}}, "owner in key")):
    try:
        ds.load_package_exceptions({"runtime_package_exceptions": bad}, Path("catalog.yaml"))
        FAILURES.append(f"an invalid entry ({why}) was accepted")
    except SystemExit:
        print(f"  ok - an invalid entry ({why}) is refused")

# The shipped catalog parses, and lists only what ADR-0073 Amendment 1 recorded.
doc = ds.load_controls(HERE.parent / "controls" / "dockerfile-standard.yaml")
shipped = ds.load_package_exceptions(doc, Path("controls/dockerfile-standard.yaml"))
if shipped != {"inboxxhq-product-service": {"tesseract-ocr", "tesseract-ocr-data-eng"}}:
    FAILURES.append(f"the shipped catalog lists {shipped}; change it with the version matrix")
else:
    print("  ok - the shipped catalog lists the recorded exception only")

if FAILURES:
    for f in FAILURES:
        print(f"::error::{f}")
    sys.exit(1)
print("DS-0018 package exceptions: all cases hold")
