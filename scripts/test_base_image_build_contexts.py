#!/usr/bin/env python3
"""Self-test for base-image-build-contexts.py. Run from the ci-workflows repo root:

    python3 scripts/test_base_image_build_contexts.py

Three halves.

THE FIXTURES, through the command line exactly as frontend-image-ci.yaml calls it. Each fixture
directory under scripts/testdata/base-image-build-contexts/ holds a Dockerfile and the complete
expected output beside it, so a shape that stops producing its line and a shape that starts
producing one it should not both fail. `frontend` is the shape of the images the workflow builds;
`shapes` is every way a Dockerfile can name, hide or merely mention an image. `buildable` is the
one ci.yaml then builds for real with the script's output.

THE EDGES, against the module. What BuildKit does with an unset ARG, an ARG only the builder can
set, a heredoc, a quoted `<<` that is not one, a different escape character, and a reference
under docker/library that is not an official image.

THE WIRING, against frontend-image-ci.yaml itself. A correct script whose output reaches one of
the two builds is the failure this lane would actually have: the image build passes and the unit
test build dies on the same 429. So every `docker buildx build` in the workflow must consume the
redirect, the redirect must be confined to GitHub-hosted runners, and the script must be given
the same --build-arg names as the builds.

The bias under test is toward leaving an image alone. A missed redirect builds exactly as the
lane did before; a wrong one names an image that does not exist, or one the Dockerfile never
asked for.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "base-image-build-contexts.py"
FIXTURES = REPO / "scripts" / "testdata" / "base-image-build-contexts"
WORKFLOW = REPO / ".github" / "workflows" / "frontend-image-ci.yaml"

spec = importlib.util.spec_from_file_location("base_image_build_contexts", SCRIPT)
bic = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bic  # dataclasses resolve their module by name
spec.loader.exec_module(bic)  # type: ignore[union-attr]

FAILURES: list[str] = []


def expect(condition: bool, name: str, detail: str = "") -> None:
    if condition:
        print(f"  ok - {name}")
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def contexts(text: str, **build_args: str) -> "bic.Result":
    return bic.build_contexts(text, build_args)


ECR = "public.ecr.aws/docker/library/"
HUB = "docker-image://docker.io/library/"

# ---------------------------------------------------------------------------
# Fixtures, through the CLI
# ---------------------------------------------------------------------------

print("fixtures:")
FIXTURE_ARGS = {
    # What frontend-image-ci.yaml passes.
    "frontend": ["--build-arg", "TARGETARCH=amd64", "--build-arg", "CORE_REF=main"],
    # An override must win over the ARG default, as it does in the build.
    "shapes": ["--build-arg", "ALPINE_TAG=3.23"],
    # What ci.yaml passes when it builds this fixture for real.
    "buildable": ["--build-arg", "TARGETARCH=amd64"],
}
for name, extra in FIXTURE_ARGS.items():
    proc = run(*extra, str(FIXTURES / name / "Dockerfile"))
    want = (FIXTURES / name / "expected.txt").read_text(encoding="utf-8")
    expect(proc.returncode == 0, f"{name}: exits 0", proc.stderr)
    expect(proc.stdout == want, f"{name}: output is exactly expected.txt",
           f"\n--- got\n{proc.stdout}--- want\n{want}")
    expect(proc.stderr == "", f"{name}: nothing to warn about", proc.stderr)

with tempfile.TemporaryDirectory() as tmp:
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp, capture_output=True, text=True)
    expect(proc.returncode == 2 and "cannot read Dockerfile" in proc.stderr,
           "no Dockerfile in the working directory is a bad invocation (exit 2)", proc.stderr)
    (Path(tmp) / "Dockerfile").write_text(f"FROM {ECR}node:22-alpine\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp, capture_output=True, text=True)
    expect(proc.stdout == f"{ECR}node:22-alpine={HUB}node:22-alpine\n",
           "the default path is ./Dockerfile", proc.stdout + proc.stderr)

proc = run("--build-arg", "NOT A PAIR", str(FIXTURES / "frontend" / "Dockerfile"))
expect(proc.returncode == 2, "a malformed --build-arg is a bad invocation (exit 2)", proc.stderr)

# ---------------------------------------------------------------------------
# Edges, against the module
# ---------------------------------------------------------------------------

print("edges:")

r = contexts(f"ARG TAG=3.24\nFROM {ECR}alpine:${{TAG}}\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24"], "an ARG default resolves the tag", str(r.lines()))

r = contexts(f"FROM {ECR}node${{SUFFIX}}\n")
expect(r.lines() == [f"{ECR}node={HUB}node:latest"] and not r.warnings,
       "an undeclared ARG expands to nothing, as BuildKit expands it", f"{r.lines()} {r.warnings}")

r = contexts(f"ARG VARIANT\nFROM {ECR}node:22${{VARIANT}}\n")
expect(r.lines() == [f"{ECR}node:22={HUB}node:22"] and not r.warnings,
       "an ARG declared with no value and no --build-arg is unset", f"{r.lines()} {r.warnings}")

r = contexts(f"ARG TARGETARCH\nFROM {ECR}alpine:3.24-${{TARGETARCH}}\n")
expect(r.lines() == [] and len(r.warnings) == 1,
       "declaring a platform ARG with no value keeps the builder's value, which is unknown here",
       f"{r.lines()} {r.warnings}")

r = contexts(f"FROM {ECR}golang:1.26-${{BUILDARCH}}\n")
expect(r.lines() == [] and any("--build-arg BUILDARCH" in w for w in r.warnings),
       "an ARG only the builder sets is left alone, with a warning that names the fix",
       f"{r.lines()} {r.warnings}")

r = contexts(f"FROM {ECR}golang:1.26-${{BUILDARCH}}\n", BUILDARCH="amd64")
expect(r.lines() == [f"{ECR}golang:1.26-amd64={HUB}golang:1.26-amd64"],
       "... and resolved once --build-arg supplies it", str(r.lines()))

r = contexts(f"ARG IMG={ECR}golang:1.26-${{BUILDARCH}}\nFROM ${{IMG}}\n")
expect(r.lines() == [] and len(r.warnings) == 1,
       "an ARG whose default needs a builder value is unknown, not empty",
       f"{r.lines()} {r.warnings}")

r = contexts(f"ARG BASE={ECR}node:22-alpine\nFROM ${{BASE}} AS base\nFROM base\n")
expect(r.lines() == [f"{ECR}node:22-alpine={HUB}node:22-alpine"],
       "an ARG-named image resolves, and the stage built on it is not an image", str(r.lines()))

r = contexts(f"ARG STAGE=builder\nFROM {ECR}node:22-alpine AS builder\nFROM ${{STAGE}}\n")
expect(r.lines() == [f"{ECR}node:22-alpine={HUB}node:22-alpine"],
       "an ARG that resolves to a stage name is a stage, not an image", str(r.lines()))

r = contexts(f"FROM {ECR}node:22-alpine\nFROM {ECR}node:22-alpine AS second\n")
expect(len(r.lines()) == 1, "a base image used twice is redirected once", str(r.lines()))

r = contexts(f"FROM {ECR}grafana/k6:latest\n")
expect(r.lines() == [] and len(r.warnings) == 1,
       "a namespaced path under docker/library is not an official image: left alone, with a warning",
       f"{r.lines()} {r.warnings}")

r = contexts(f"FROM public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine\nFROM node:22-alpine\n")
expect(r.lines() == [] and not r.warnings,
       "another ECR Public publisher and a bare name are not this script's business")

r = contexts(f"FROM {ECR}alpine:3.24\nRUN <<-EOT\n\tFROM {ECR}mysql:8\n\tEOT\nFROM {ECR}debian:13\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24", f"{ECR}debian:13={HUB}debian:13"],
       "a <<- heredoc body with a tab-indented terminator is skipped, and parsing resumes after it",
       str(r.lines()))

r = contexts(f"FROM {ECR}alpine:3.24\nRUN echo 'a <<WORD b'\nFROM {ECR}debian:13\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24", f"{ECR}debian:13={HUB}debian:13"],
       "a `<<WORD` with no terminator does not swallow the rest of the file", str(r.lines()))

r = contexts(f"# escape=`\nFROM `\n  {ECR}alpine:3.24 `\n  AS base\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24"],
       "the escape directive changes the continuation character", str(r.lines()))

r = contexts(f"FROM \\\n# a comment inside the continuation\n  {ECR}alpine:3.24\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24"],
       "a comment inside a continuation is dropped, as the parser drops it", str(r.lines()))

r = contexts(f"from {ECR}alpine:3.24 as base\nfrom base\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24"], "instructions are case-insensitive", str(r.lines()))

r = contexts(f"ARG T=22\nFROM {ECR}node:${{T:+${{T}}-alpine}}\nFROM {ECR}nginx:${{MISSING-1.27}}\n")
expect(r.lines() == [f"{ECR}nginx:1.27={HUB}nginx:1.27", f"{ECR}node:22-alpine={HUB}node:22-alpine"],
       "the ${VAR:+x} and ${VAR-x} forms expand, nested", str(r.lines()))

r = contexts(f"FROM {ECR}alpine:3.24\nCOPY --from=0 / /\nRUN --mount=type=cache,target=/c true\n")
expect(r.lines() == [f"{ECR}alpine:3.24={HUB}alpine:3.24"],
       "a stage index and a mount without from= pull nothing", str(r.lines()))

# ---------------------------------------------------------------------------
# Wiring, against frontend-image-ci.yaml
# ---------------------------------------------------------------------------

print("wiring:")
workflow_text = WORKFLOW.read_text(encoding="utf-8")
steps = yaml.safe_load(workflow_text)["jobs"]["build"]["steps"]

producers = [s for s in steps if "base-image-build-contexts.py" in s.get("run", "")]
expect(len(producers) == 1, "exactly one step computes the redirect", str(len(producers)))
producer = producers[0] if producers else {}
step_id = producer.get("id", "")
expect(bool(step_id), "the redirect step has an id the builds can read")
expect(re.fullmatch(r"\s*runner\.environment\s*==\s*'github-hosted'\s*", str(producer.get("if", ""))) is not None,
       "the redirect runs on GitHub-hosted runners only", str(producer.get("if")))

builds = [s for s in steps if "docker buildx build" in s.get("run", "")]
expect(len(builds) == workflow_text.count("docker buildx build") == 2,
       "both buildx invocations (image, unit tests) are steps this test can see",
       f"{len(builds)} step(s), {workflow_text.count('docker buildx build')} invocation(s)")
expect(any("--target unit-tests" in s["run"] for s in builds),
       "the unit-test build is among them")

producer_args = set(re.findall(r"--build-arg\s+([A-Za-z_][A-Za-z0-9_]*)=", producer.get("run", "")))
for s in builds:
    name = s.get("name", "?")
    env = s.get("env", {}) or {}
    reads_output = any(f"steps.{step_id}.outputs.contexts" in str(v) for v in env.values())
    expect(bool(step_id) and reads_output, f"{name!r} reads the redirect step's output")
    body = s["run"]
    invocation = body[body.index("docker buildx build"):]
    expect('"${build_contexts[@]}"' in invocation and "--build-context" in body,
           f"{name!r} passes the redirect to docker buildx build")
    build_args = set(re.findall(r"--build-arg\s+([A-Za-z_][A-Za-z0-9_]*)=", invocation))
    expect(build_args <= producer_args,
           f"{name!r} gives the script every --build-arg it gives the build",
           f"build {sorted(build_args)} vs script {sorted(producer_args)}")


def main() -> int:
    if FAILURES:
        print(f"base-image build contexts self-test: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("base-image build contexts self-test: OK - every fixture matches its expected output "
          "exactly, the BuildKit edge cases hold, and both buildx invocations in "
          "frontend-image-ci.yaml take the redirect on GitHub-hosted runners only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
