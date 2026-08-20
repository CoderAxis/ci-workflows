#!/usr/bin/env python3
"""Self-test for the dependency-pin fast path.

Two halves, for the same reasons as test_docs_only_diff.py.

THE CLASSIFIER, against real git repositories. The part most likely to break is
not the filename check, it is what counts as a pin INSIDE go.mod: a `toolchain`
line, a `replace` directive and a `require` bump all arrive in the same file, and
only the last is a pin. Every case here builds real commits and asks the script.

THE WIRING, against service-ci.yaml itself. A classifier wired to nothing is the
failure this repository keeps rediscovering, so the structural assertions pin
which jobs the fast path may skip and, more importantly, which it may NOT: the
jobs whose subject IS the dependency delta.

The bias under test is one-directional, as it is for docs-only. A wrong `false`
wastes a pipeline; a wrong `true` merges a dependency change whose scanners never
ran. Every ambiguous case below therefore asserts `false`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CLASSIFIER = REPO / "scripts" / "pin-only-diff.py"
WORKFLOW = REPO / ".github" / "workflows" / "service-ci.yaml"

FAILURES: list[str] = []

GO_MOD = """module example.test

go 1.25

require (
\tgithub.com/coderaxis/platform-contracts-go v0.37.0
\tgithub.com/coderaxis/platform-shared-go v1.39.0
)
"""


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# ---------------------------------------------------------------------------
# A real git repository to diff
# ---------------------------------------------------------------------------

class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@example.test")
        self.git("config", "user.name", "CI Self Test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def write(self, rel: str, text: str = "x\n") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


def base_repo(root: Path) -> tuple[Repo, str]:
    r = Repo(root)
    r.write("README.md", "# service\n")
    r.write("go.mod", GO_MOD)
    r.write("go.sum", "github.com/coderaxis/platform-contracts-go v0.37.0 h1:aaa=\n")
    r.write("internal/handler.go", "package internal\n")
    return r, r.commit("initial")


def classify(repo: Repo, event_name: str, payload: dict) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        event_path = fh.name
    proc = subprocess.run(
        [sys.executable, str(CLASSIFIER),
         "--repo-root", str(repo.root),
         "--event-name", event_name,
         "--event-path", event_path],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    Path(event_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        return False, f"classifier exited {proc.returncode}\n{out}"
    return ("pin_only=true" in out), out


def push(before: str, after: str, **extra) -> dict:
    payload = {"before": before, "after": after,
               "created": False, "deleted": False, "forced": False}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

def test_a_bump_pr_is_pins_only(tmp: Path) -> None:
    """The shape the cascade actually opens: one require line and its go.sum row."""
    r, first = base_repo(tmp / "bump")
    r.write("go.mod", GO_MOD.replace("v0.37.0", "v0.38.0"))
    r.write("go.sum", "github.com/coderaxis/platform-contracts-go v0.38.0 h1:bbb=\n")
    after = r.commit("chore(deps): bump platform-contracts-go to v0.38.0")

    ok, out = classify(r, "push", push(first, after))
    expect(ok, f"a go.mod + go.sum version bump must be pin-only:\n{out}")


def test_a_toolchain_change_is_not_a_pin(tmp: Path) -> None:
    """The trap. `go 1.26` recompiles everything and can move gofmt and CodeQL,
    so it must not ride the fast path even though it lives in go.mod."""
    r, first = base_repo(tmp / "toolchain")
    r.write("go.mod", GO_MOD.replace("go 1.25", "go 1.26"))
    after = r.commit("chore: go 1.26")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"a `go` directive change must run everything:\n{out}")


def test_a_replace_directive_is_not_a_pin(tmp: Path) -> None:
    r, first = base_repo(tmp / "replace")
    r.write("go.mod", GO_MOD + "\nreplace github.com/coderaxis/platform-shared-go => ../local\n")
    after = r.commit("chore: point at a local checkout")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"a `replace` directive must run everything:\n{out}")


def test_a_pin_bump_alongside_source_is_not_pins_only(tmp: Path) -> None:
    r, first = base_repo(tmp / "mixed")
    r.write("go.mod", GO_MOD.replace("v0.37.0", "v0.38.0"))
    r.write("internal/handler.go", "package internal\n\nfunc New() {}\n")
    after = r.commit("feat: use the new RPCs")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"a bump carrying source changes must run everything:\n{out}")


def test_source_reverted_later_in_the_same_push_still_runs_everything(tmp: Path) -> None:
    """The union reading. The net tree diff is pins-only, but a commit in the
    range touched source, and CI tests the range."""
    r, first = base_repo(tmp / "revert")
    r.write("internal/handler.go", "package internal\n\nfunc Temp() {}\n")
    r.commit("wip")
    r.write("internal/handler.go", "package internal\n")
    r.write("go.mod", GO_MOD.replace("v0.37.0", "v0.38.0"))
    after = r.commit("chore(deps): bump, and undo the wip")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"source touched anywhere in the range must run everything:\n{out}")


def test_a_documentation_change_is_not_a_pin(tmp: Path) -> None:
    """The two fast paths are disjoint, and neither may claim the other's diff."""
    r, first = base_repo(tmp / "docs")
    r.write("README.md", "# service\n\nnotes\n")
    after = r.commit("docs: notes")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"a documentation diff is not a pin diff:\n{out}")


def test_a_testdata_go_mod_is_not_this_modules_pins(tmp: Path) -> None:
    r, first = base_repo(tmp / "fixture")
    r.write("scripts/testdata/fixture/go.mod", "module fixture\n\ngo 1.25\n")
    after = r.commit("test: add a fixture module")

    ok, out = classify(r, "push", push(first, after))
    expect(not ok, f"a go.mod under testdata/ is a fixture, not this module:\n{out}")


def test_an_undeterminable_range_runs_everything(tmp: Path) -> None:
    r, _ = base_repo(tmp / "forced")
    r.write("go.mod", GO_MOD.replace("v0.37.0", "v0.38.0"))
    after = r.commit("chore(deps): bump")

    ok, out = classify(r, "push", push("0" * 40, after, created=True))
    expect(not ok, f"a first push carries no range and must run everything:\n{out}")

    ok, out = classify(r, "push", push(after, after, forced=True))
    expect(not ok, f"a force push is a rewrite and must run everything:\n{out}")

    ok, out = classify(r, "workflow_dispatch", {})
    expect(not ok, f"workflow_dispatch must run everything:\n{out}")


def test_go_sum_alone_is_pins_only(tmp: Path) -> None:
    """`go mod tidy` rewriting only go.sum is derived state and nothing else."""
    r, first = base_repo(tmp / "sum")
    r.write("go.sum", "github.com/coderaxis/platform-contracts-go v0.37.0 h1:ccc=\n")
    after = r.commit("chore: tidy")

    ok, out = classify(r, "push", push(first, after))
    expect(ok, f"a go.sum-only diff must be pin-only:\n{out}")


# ---------------------------------------------------------------------------
# The wiring in service-ci.yaml
# ---------------------------------------------------------------------------

# Jobs whose every input is a first-party file that a pin bump does not touch.
# Under a pin-only diff these are not being re-checked, they are being
# re-confirmed against bytes identical to the run that already passed.
#
# codeql is the one worth arguing about, and it belongs here only because the two
# jobs whose actual subject is the dependency delta -- vulnerabilities and
# dependency-review -- are both in PIN_KEPT below. CodeQL's own input is
# first-party source, which is unchanged.
#
# sqlc belongs here for a specific, checked reason: service-ci.yaml installs
# `sqlc@v1.30.0` itself rather than taking it from the repository's go.mod, so a
# pin bump cannot change its generated output.
PIN_SKIPPED = {
    "codeql", "events", "seeding", "schema", "dockerfile", "structure",
    "uuid-policy", "org-vocabulary", "sqlc", "contract-classification",
    "gateway-baseline", "source-invariants", "standards", "docs", "contracts",
}

# Jobs a dependency genuinely can break, and the cheap infrastructure everything
# reads. Gating any of these would make the fast path a hole rather than a saving.
PIN_KEPT = {
    "resolve",             # identity; everything reads it
    "supported-language",  # the guard against a silent green
    "lanes",               # the caller is not running its own pipeline
    "docs-only",           # the other classifier
    "pin-only",            # this one
    "build-test",          # does it still compile and pass its tests
    "integration",         # a library change is a behaviour change
    "e2e",                 # likewise, end to end
    "vulnerabilities",     # a bump is EXACTLY when a CVE arrives
    "dependency-review",   # its entire subject is the dependency delta
    "openapi-generator",   # the generator library comes from platform-shared-go
    "contract",            # the spec's common.v1 projection moves with contracts-go
    "artifact",            # the image and the GitOps pin still have to be built
    "notify",              # always()
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _needs(job: dict) -> list[str]:
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def test_every_job_is_classified_for_the_pin_path() -> None:
    jobs = set(_workflow()["jobs"])
    unaccounted = jobs - PIN_SKIPPED - PIN_KEPT
    expect(not unaccounted,
           f"service-ci.yaml has job(s) this test does not classify: "
           f"{sorted(unaccounted)}. Add each to PIN_SKIPPED or PIN_KEPT.")
    missing = (PIN_SKIPPED | PIN_KEPT) - jobs
    expect(not missing,
           f"this test names job(s) service-ci.yaml no longer has: {sorted(missing)}")


def test_skipped_jobs_depend_on_and_test_the_flag() -> None:
    jobs = _workflow()["jobs"]
    for name in sorted(PIN_SKIPPED & set(jobs)):
        job = jobs[name]
        expect("pin-only" in _needs(job),
               f"{name} must list pin-only in needs, or its `if:` reads an "
               f"empty string and the job runs regardless")
        cond = " ".join(str(job.get("if", "")).split())
        expect("needs.pin-only.outputs.pin_only != 'true'" in cond,
               f"{name} must skip when the diff is only dependency pins; if: {cond!r}")


def test_the_jobs_a_dependency_can_break_are_never_skipped() -> None:
    """The half that makes this safe rather than merely cheap."""
    jobs = _workflow()["jobs"]
    for name in sorted(PIN_KEPT & set(jobs)):
        cond = " ".join(str(jobs[name].get("if", "")).split())
        expect("pin_only" not in cond,
               f"{name} can return a different answer after a dependency change "
               f"and must not be skipped by the pin fast path; if: {cond!r}")


def test_the_supply_chain_gates_are_kept() -> None:
    """Named explicitly, because these are the ones whose absence would make the
    whole fast path indefensible."""
    for name in ("vulnerabilities", "dependency-review", "build-test"):
        expect(name in PIN_KEPT,
               f"{name} must run on a pin-only diff: a dependency bump is "
               f"precisely the diff it exists to examine")


def test_the_artifact_still_builds_for_a_bump() -> None:
    """A bump merged to main has to produce an image; only docs-only may skip it."""
    artifact = _workflow()["jobs"]["artifact"]
    cond = " ".join(str(artifact.get("if", "")).split())
    expect("pin_only" not in cond,
           "artifact must not be gated on pin_only: a merged bump needs a new "
           f"image and GitOps pin; if: {cond!r}")


def test_the_classifier_job_publishes_the_flag() -> None:
    jobs = _workflow()["jobs"]
    expect("pin-only" in jobs, "service-ci.yaml must define a pin-only job")
    job = jobs.get("pin-only") or {}
    outputs = job.get("outputs") or {}
    expect("pin_only" in outputs,
           "the pin-only job must publish a pin_only output")
    body = yaml.dump(job)
    expect("fetch-depth: 0" in body,
           "the pin-only job must check out full history: the before-SHA of a "
           "push is exactly the object a shallow clone lacks")
    expect("pin-only-diff.py" in body,
           "the pin-only job must run the central classifier")


def test_the_two_fast_paths_compose() -> None:
    """A job skipped by both must test both, or one flag masks the other."""
    jobs = _workflow()["jobs"]
    for name in sorted(PIN_SKIPPED & set(jobs)):
        cond = " ".join(str(jobs[name].get("if", "")).split())
        if "docs_only" in cond:
            expect("&&" in cond,
                   f"{name} is gated by both fast paths and must combine them "
                   f"with &&; if: {cond!r}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import shutil
    import tempfile as tf

    tmp = Path(tf.mkdtemp(prefix="pin-only-selftest-"))
    ran = 0
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            ran += 1
            if fn.__code__.co_argcount == 1:
                fn(tmp)
            else:
                fn()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILURES:
        print(f"pin-only fast path self-test: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"pin-only fast path self-test: OK ({ran} test(s)) - a cascade bump PR "
          "is recognised; toolchain, replace, mixed-source, reverted-source, "
          "documentation and testdata diffs all run everything; and the wiring in "
          "service-ci.yaml is asserted job by job, including that the supply-chain "
          "gates are never skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
