#!/usr/bin/env python3
"""Self-test for check-base-image-digest.py and DS-0003's digest tolerance. Run from the
ci-workflows repo root:

    python3 scripts/test_base_image_digest.py

THE FIXTURES, through the command line in every mode: `pinned` must pass everywhere,
`baselined` fails only under enforce, `new` fails under ratchet and enforce. A checker that
stopped matching would pass everything, so both directions are asserted.

THE PARSING, against the module: image names with registry ports, tags and digests, ARG
defaults (including a bare in-stage re-declaration), and a digest ARG with no default.

DS-0003, against check-dockerfile-standard.py: the canonical template with a digest added,
literally or through the ARG default, still matches the version matrix, a wrong tag with a
digest still fails, and a malformed digest fails. Without this the pin the new check asks
for would fail the Dockerfile standard on every service that adopts it.

THE WIRING: the reusable workflows run the checker in warn mode and cannot fail on it, and
the baseline is a sorted list of tag-free image names.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = HERE / "testdata" / "base-image-digest"
FAILURES: list[str] = []


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def expect(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok - {name}")
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


digest = load("base_image_digest", "check-base-image-digest.py")

print("fixtures")
want = {("warn", "pinned"): 0, ("warn", "baselined"): 0, ("warn", "new"): 0,
        ("ratchet", "pinned"): 0, ("ratchet", "baselined"): 0, ("ratchet", "new"): 1,
        ("enforce", "pinned"): 0, ("enforce", "baselined"): 1, ("enforce", "new"): 1}
for (mode, fixture), code in want.items():
    r = subprocess.run([sys.executable, str(HERE / "check-base-image-digest.py"), "--mode", mode,
                        "--root", str(FIX / fixture)], capture_output=True, text=True)
    expect(f"{mode}/{fixture} exits {code}", r.returncode == code, f"exit {r.returncode}: {r.stdout}{r.stderr}")
r = subprocess.run([sys.executable, str(HERE / "check-base-image-digest.py"), "--root", str(FIX / "new")],
                   capture_output=True, text=True)
expect("warn mode names new debt as NEW", "NEW:" in r.stdout and "redis" in r.stdout, r.stdout)
expect("warn mode explains a digest ARG with no default", "RUNTIME_IMAGE_DIGEST has no default" in r.stdout, r.stdout)
r = subprocess.run([sys.executable, str(HERE / "check-base-image-digest.py"), "--baseline", "/nonexistent.json",
                    "--root", str(FIX / "pinned")], capture_output=True, text=True)
expect("an unreadable baseline is a bad invocation (exit 2)", r.returncode == 2, f"exit {r.returncode}")

print("parsing")
for ref, name in [("public.ecr.aws/docker/library/golang:1.26-alpine3.24", "public.ecr.aws/docker/library/golang"),
                  ("localhost:5000/team/app:1.0@sha256:" + "a" * 64, "localhost:5000/team/app"),
                  ("alpine", "alpine"), ("grafana/k6:latest", "grafana/k6")]:
    expect(f"image_name({ref[:40]}...)", digest.image_name(ref) == name, digest.image_name(ref))
args = digest.arg_defaults(["ARG GO_LANG_VERSION=1.25", "FROM x AS b", "ARG GO_LANG_VERSION", "ARG BARE"])
expect("a bare in-stage ARG keeps the global default", args.get("GO_LANG_VERSION") == "1.25", str(args))
expect("a bare ARG is recorded empty", args.get("BARE") == "", str(args))
with tempfile.TemporaryDirectory() as tmp:
    df = Path(tmp) / "Dockerfile"
    df.write_text("ARG D=sha256:" + "b" * 64 + "\nfrom --platform=$BUILDPLATFORM img:1@${D} as one\n"
                  "FROM img:2@sha256:" + "C" * 64 + "\nFROM one\n", encoding="utf-8")
    recs = digest.check(df)
    expect("lower-case FROM with --platform and an ARG digest counts as pinned",
           len(recs) == 2 and recs[0]["pinned"], str(recs))
    expect("an upper-case hex digest is not a valid pin", not recs[1]["pinned"], str(recs))

print("DS-0003 digest tolerance")
ds = load("dockerfile_standard_digest", "check-dockerfile-standard.py")
TEMPLATE = (ROOT / "templates" / "Dockerfile.service").read_text(encoding="utf-8")
GOOD = "@sha256:" + "d" * 64


def ds3(text: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Dockerfile"
        path.write_text(text, encoding="utf-8")
        return ds.version_matrix_pinned(ds.Dockerfile(path, Path(tmp), "x", {}))


f = ds3(TEMPLATE)
expect("the template as shipped passes", f.ok, f.evidence)
f = ds3(TEMPLATE.replace(f"ARG BUILDER_IMAGE_TAG={ds.EXPECTED_BUILDER_TAG}",
                         f"ARG BUILDER_IMAGE_TAG={ds.EXPECTED_BUILDER_TAG}{GOOD}"))
expect("a digest in the ARG default passes", f.ok, f.evidence)
f = ds3(TEMPLATE.replace("alpine:${RUNTIME_IMAGE_TAG}", f"alpine:{ds.EXPECTED_RUNTIME_TAG}{GOOD}"))
expect("a literal digest on the FROM passes", f.ok, f.evidence)
f = ds3(TEMPLATE.replace(f"ARG BUILDER_IMAGE_TAG={ds.EXPECTED_BUILDER_TAG}", f"ARG BUILDER_IMAGE_TAG=1.25-alpine3.20{GOOD}"))
expect("a wrong tag with a digest still fails", not f.ok and "1.25-alpine3.20" in f.evidence, f.evidence)
f = ds3(TEMPLATE.replace("alpine:${RUNTIME_IMAGE_TAG}", f"alpine:{ds.EXPECTED_RUNTIME_TAG}@sha256:short"))
expect("a malformed digest fails", not f.ok and "not a sha256 digest" in f.evidence, f.evidence)

print("wiring")
for wf, path in [("dockerfile-standard.yaml", ".dockerfile-standard-tools/scripts/check-base-image-digest.py"),
                 ("frontend-image-ci.yaml", ".coderaxis-ci/scripts/check-base-image-digest.py")]:
    text = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
    i = text.find(path)
    step = text[text.rfind("- name:", 0, i):i + 200] if i >= 0 else ""
    expect(f"{wf} runs the checker", i >= 0)
    expect(f"{wf} runs it in warn mode", "--mode warn" in step, step)
    expect(f"{wf} cannot fail the build on it", "continue-on-error: true" in step, step)
doc = json.loads((ROOT / "controls" / "base-image-digest-baseline.json").read_text(encoding="utf-8"))
imgs = doc["unpinned_images"]
expect("baseline is sorted and unique", imgs == sorted(set(imgs)), str(imgs))
expect("baseline entries carry no tag or digest", all(digest.image_name(i) == i for i in imgs), str(imgs))

if FAILURES:
    print("\nFAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nall base-image digest checks passed")
