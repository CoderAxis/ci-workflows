#!/usr/bin/env python3
"""Self-test for `CI passed`, the one check a calling repository requires on main.

Two halves.

THE WIRING, against service-ci.yaml. The job is only worth requiring if it waits on everything
the image build waits on: a gate `artifact` reads but `ci-passed` does not would let a PR merge
green and then build no image, which is the exact failure this job exists to stop. So the needs
lists are held together here rather than by a comment. It must also run when something it needs
failed (a plain job is skipped then, and GitHub counts a skip as a pass), and it must be a plain
job rather than a reusable-workflow call, because callers require it by its two-part context
`<caller job> / CI passed`.

THE VERDICT, by running the job's own script. Skipped is a pass (that is how the docs-only and
pin-only fast paths look); failure and cancelled are not (a job killed by its own timeout reports
cancelled).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "service-ci.yaml"
GATE = "ci-passed"
GATE_NAME = "CI passed"

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict) -> set[str]:
    n = job.get("needs") or []
    return {n} if isinstance(n, str) else set(n)


def test_the_gate_exists_under_the_name_callers_require() -> None:
    jobs = _jobs()
    expect(GATE in jobs, f"service-ci.yaml has no `{GATE}` job; every caller's ruleset requires it")
    if GATE in jobs:
        expect(jobs[GATE].get("name") == GATE_NAME,
               f"`{GATE}` must be named {GATE_NAME!r}: rulesets require `ci / {GATE_NAME}` by name, "
               f"and a rename makes every caller's main unmergeable")
        expect("uses" not in jobs[GATE],
               f"`{GATE}` must be a plain job; a reusable-workflow call reports a three-part context")


def test_the_gate_waits_on_everything_the_image_waits_on() -> None:
    jobs = _jobs()
    if GATE not in jobs:
        return
    gate, artifact = _needs(jobs[GATE]), _needs(jobs["artifact"])
    missing = sorted(artifact - gate)
    expect(not missing, f"`{GATE}` does not wait on {missing}, which `artifact` does: a PR could "
                        f"merge green and then build no image")
    expect("pin-only" in gate, f"`{GATE}` must wait on pin-only: a crashed classifier skips the "
                               f"gates that read it without failing any of them")
    unknown = sorted(gate - set(jobs))
    expect(not unknown, f"`{GATE}` needs job(s) service-ci.yaml does not have: {unknown}")
    expect("artifact" not in gate and "notify" not in gate,
           f"`{GATE}` must not wait on artifact or notify: they run on main, after the merge")


def test_the_gate_runs_when_something_it_needs_failed() -> None:
    jobs = _jobs()
    if GATE not in jobs:
        return
    cond = " ".join(str(jobs[GATE].get("if", "")).split())
    expect("!cancelled()" in cond,
           f"`{GATE}` must run with !cancelled(); without it a failed need skips the gate, and a "
           f"skipped required check counts as passed. if: {cond!r}")
    expect("always()" not in cond,
           f"`{GATE}` must not use always(): a superseded run would paint its stale commit red")


def _verdict_script() -> str:
    run = _jobs()[GATE]["steps"][0]["run"]
    m = re.search(r"<<'PY'\n(.*?)\nPY", run, re.S)
    if not m:
        raise AssertionError(f"`{GATE}` step no longer carries its python verdict in a PY heredoc")
    return m.group(1)


def _run(needs: dict) -> int:
    env = dict(os.environ, NEEDS=json.dumps({k: {"result": v, "outputs": {}} for k, v in needs.items()}))
    return subprocess.run([sys.executable, "-c", _verdict_script()], env=env,
                          capture_output=True, text=True).returncode


def test_the_verdict() -> None:
    if GATE not in _jobs():
        return
    cases = [
        ({"build-test": "success", "codeql": "success"}, 0, "all green"),
        ({"build-test": "success", "codeql": "skipped"}, 0, "a fast path skipped a gate"),
        ({"docs-only": "success", "build-test": "skipped"}, 0, "a documentation-only PR"),
        ({"build-test": "failure", "docker-tests": "skipped"}, 1, "a red build skipping its dependants"),
        ({"build-test": "success", "e2e": "cancelled"}, 1, "a gate killed by its own timeout"),
        ({"resolve": "failure", "build-test": "skipped"}, 1, "identity failed and nothing ran"),
    ]
    for needs, want, what in cases:
        got = _run(needs)
        expect(got == want, f"verdict for {what}: exit {got}, want {want}")


def main() -> int:
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            ran += 1
            try:
                fn()
            except AssertionError as e:
                FAILURES.append(f"{name}: {e}")
    if FAILURES:
        print(f"CI passed gate self-test: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"CI passed gate self-test: OK ({ran} test(s)) - `{GATE}` waits on every gate the image "
          f"build waits on, runs when one of them failed, and passes skips but not failures or "
          f"cancellations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
