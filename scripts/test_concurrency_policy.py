#!/usr/bin/env python3
"""Self-test: no workflow here can cancel a run on main.

A run on main is the verdict a release reads: module-release.yaml refuses a commit whose CI did not
pass, and this repository's release.yaml moves v1 only once CI passed for that exact commit. With
`cancel-in-progress: true`, the next merge cancelled the run still going on main, so that commit had
no verdict, never released, and needed someone to notice and re-run it (ai-core #614, 2026-11-22).

So every `concurrency` block, at workflow or job level, either never cancels, or cancels through an
expression this test evaluates: false for every way a run starts on main (push, dispatch, schedule),
and true for a pull request, which must still give way to its own newer push.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# The workflows whose pull request runs must keep cancelling: they are the long ones, and a stale
# run of a pushed-over commit only holds a runner.
PR_RUNS_CANCEL = {"service-ci.yaml", "ci.yaml"}

ON_MAIN = [
    {"github.ref": "refs/heads/main", "github.event_name": "push"},
    {"github.ref": "refs/heads/main", "github.event_name": "workflow_dispatch"},
    {"github.ref": "refs/heads/main", "github.event_name": "schedule"},
]
PULL_REQUEST = {"github.ref": "refs/pull/7/merge", "github.event_name": "pull_request"}

# `${{ <context> == '<literal>' }}` or `!=`. Deliberately the only shape accepted: anything richer
# would need a real evaluator, and a cancel rule nobody can evaluate is one nobody can trust.
EXPR = re.compile(r"^\$\{\{\s*(github\.ref|github\.event_name)\s*(==|!=)\s*'([^']*)'\s*\}\}$")

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def evaluate(value: object, ctx: dict[str, str]) -> bool:
    """What cancel-in-progress resolves to for a run started in `ctx`."""
    if isinstance(value, bool):
        return value
    m = EXPR.match(str(value).strip())
    if not m:
        raise ValueError(f"cancel-in-progress {value!r} is not `${{{{ <github.ref|github.event_name> "
                         f"==|!= '<literal>' }}}}`, so this test cannot prove it spares main")
    left, op, lit = m.groups()
    return (ctx[left] == lit) if op == "==" else (ctx[left] != lit)


def blocks(path: Path) -> list[tuple[str, object]]:
    """(where, cancel-in-progress) for every concurrency block in a workflow."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    found = []
    for where, conc in [("workflow", doc.get("concurrency"))] + [
            (f"job {k}", (j or {}).get("concurrency")) for k, j in (doc.get("jobs") or {}).items()]:
        if conc is None:
            continue
        # A bare string is a group with cancel-in-progress left at its default, false.
        cancel = conc.get("cancel-in-progress", False) if isinstance(conc, dict) else False
        found.append((where, cancel))
    return found


def test_no_concurrency_block_cancels_a_run_on_main() -> None:
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for where, cancel in blocks(path):
            try:
                hits = [c["github.event_name"] for c in ON_MAIN if evaluate(cancel, c)]
            except ValueError as e:
                expect(False, f"{path.name} {where}: {e}")
                continue
            expect(not hits, f"{path.name} {where}: cancel-in-progress {cancel!r} cancels main's "
                             f"run on {hits}; a cancelled main run has no verdict, so its commit "
                             f"never releases. Use `${{{{ github.ref != 'refs/heads/main' }}}}`")


def test_pull_request_runs_still_cancel() -> None:
    for name in sorted(PR_RUNS_CANCEL):
        path = WORKFLOWS / name
        found = [c for w, c in blocks(path) if w == "workflow"]
        expect(len(found) == 1, f"{name}: expected one workflow-level concurrency block")
        if len(found) == 1:
            try:
                ok = evaluate(found[0], PULL_REQUEST)
            except ValueError as e:
                FAILURES.append(f"{name}: {e}")
                continue
            expect(ok, f"{name}: a pull request's run no longer gives way to its newer push")


def test_the_evaluator() -> None:
    main_push, pr = ON_MAIN[0], PULL_REQUEST
    expect(evaluate(True, main_push) is True, "a literal true cancels")
    expect(evaluate("${{ github.ref != 'refs/heads/main' }}", main_push) is False, "spares main")
    expect(evaluate("${{ github.ref != 'refs/heads/main' }}", pr) is True, "cancels a PR")
    expect(evaluate("${{ github.event_name == 'pull_request' }}", ON_MAIN[2]) is False,
           "event_name form spares a scheduled run")
    try:
        evaluate("${{ !contains(github.ref, 'main') }}", main_push)
        expect(False, "an expression it cannot read must be refused, not guessed")
    except ValueError:
        pass


def main() -> int:
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            ran += 1
            fn()
    if FAILURES:
        print(f"concurrency policy self-test: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"concurrency policy self-test: OK ({ran} test(s)) - no workflow cancels a run on main, "
          f"and pull request runs still give way to a newer push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
