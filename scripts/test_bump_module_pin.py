#!/usr/bin/env python3
"""Tests for bump-module-pin: the supersede rule (bump-module-pin/supersede.py and the guard and
supersede steps that act on it) and the auto-merge step, run as the action runs them (bash, a
fake `gh`).

The steps are the layer that closes PRs and opens nothing, so they are executed, not only the
script: a script that decides right is no use if a step ignores its `newer` line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "bump-module-pin" / "action.yaml"
SUPERSEDE = ROOT / "bump-module-pin" / "supersede.py"
sys.path.insert(0, str(SUPERSEDE.parent))
import supersede as sp  # noqa: E402

FAILS = 0


def expect(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'}  {msg}")
    FAILS += 0 if cond else 1


def prs(*heads: tuple[int, str]) -> list[dict]:
    return [{"number": n, "headRefName": h} for n, h in heads]


print("supersede.py decide()")
# 2026-11-20: a cascade reconcile planned at v0.70.0 closed the open v0.70.1 bump.
out = sp.decide(prs((758, "chore/bump-ai-core-v0.70.1")), [("ai-core", "v0.70.0")],
                "chore/bump-ai-core-v0.70.0")
expect(out == ["newer 758 ai-core v0.70.1"], f"a newer open bump supersedes this one: {out}")
out = sp.decide(prs((757, "chore/bump-ai-core-v0.69.5")), [("ai-core", "v0.70.0")],
                "chore/bump-ai-core-v0.70.0")
expect(out == ["close 757 ai-core v0.69.5"], f"an older open bump is closed: {out}")
out = sp.decide(prs((9, "chore/bump-ai-core-v0.70.9")), [("ai-core", "v0.70.10")],
                "chore/bump-ai-core-v0.70.10")
expect(out == ["close 9 ai-core v0.70.9"], f"versions compare as numbers, not text: {out}")
out = sp.decide(prs((10, "chore/bump-ai-core-v0.70.10")), [("ai-core", "v0.70.9")],
                "chore/bump-ai-core-v0.70.9")
expect(out == ["newer 10 ai-core v0.70.10"], f"v0.70.10 is newer than v0.70.9: {out}")
out = sp.decide(prs((5, "chore/bump-ai-core-postgres-v0.40.0"), (6, "chore/bump-ai-core-v0.70.0")),
                [("ai-core", "v0.70.0")], "chore/bump-ai-core-v0.70.0")
expect(out == [], f"another module sharing the prefix and the bump's own branch are ignored: {out}")
out = sp.decide(prs((7, "chore/bump-order-core-postgres-v0.21.3"), (8, "chore/bump-order-core-postgres-v0.21.2")),
                [("order-core", "v0.33.2"), ("order-core-postgres", "v0.21.2")],
                "chore/bump-order-core-v0.33.2")
expect(out == ["newer 7 order-core-postgres v0.21.3", "close 8 order-core-postgres v0.21.2"],
       f"a companion is compared at its own version, an equal one is closed: {out}")
out = sp.decide(prs((11, "chore/bump-ai-core-vnext")), [("ai-core", "v0.70.0")],
                "chore/bump-ai-core-v0.70.0")
expect(out == ["unknown 11 chore/bump-ai-core-vnext"], f"an unordered head is left alone: {out}")
out = sp.decide(prs((12, "chore/bump-ai-core-v0.69.0")), [("ai-core", "main")],
                "chore/bump-ai-core-main")
expect(out == [], f"a bump whose own version does not parse compares nothing: {out}")


def step(step_id: str) -> dict:
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    return next(s for s in steps if s.get("id") == step_id)


def run_step(step_id: str, *, pin: str, version: str, open_prs: list[dict],
             companions: str = "", short: str = "ai-core",
             module: str = "github.com/coderaxis/ai-core", gh_body: str | None = None,
             extra: dict | None = None) -> tuple[str, list[str], str]:
    """Runs one action step's `run:` under bash; returns (last skip value, gh calls, stdout)."""
    s = step(step_id)
    env_keys = set(s.get("env", {}))
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "go.mod").write_text(f"module example.com/x\n\ngo 1.26\n\nrequire (\n\t{module} {pin}\n)\n")
        (dp / "prs.json").write_text(json.dumps(open_prs))
        (dp / "bin").mkdir()
        gh = dp / "bin" / "gh"
        gh.write_text('#!/usr/bin/env bash\necho "$*" >> "$GH_LOG"\n' + (gh_body or
                      'if [[ "$1 $2" == "pr list" ]]; then cat "$GH_PRS"; fi\n'))
        gh.chmod(0o755)
        provided = {
            "MODULE": module, "VERSION": version, "COMPANIONS": companions, "SHORT": short,
            "GH_TOKEN": "x", "TARGET": "acme/consumer",
            "NEW_BRANCH": f"chore/bump-{short}-{version}", "SUPERSEDE": str(SUPERSEDE),
            **(extra or {}),
        }
        missing = {k for k in env_keys if k not in provided}
        expect(not missing, f"{step_id}: the test provides every env the step declares {missing or ''}")
        env = {k: v for k, v in provided.items() if k in env_keys}
        env.update(PATH=f"{dp / 'bin'}:{os.environ['PATH']}", GITHUB_OUTPUT=str(dp / "out"),
                   GH_LOG=str(dp / "gh.log"), GH_PRS=str(dp / "prs.json"), HOME=d,
                   GITHUB_STEP_SUMMARY=str(dp / "summary.md"))
        (dp / "out").touch()
        r = subprocess.run(["bash", "-e", "-c", s["run"]], cwd=d, env=env, text=True,
                           capture_output=True)
        expect(r.returncode == 0, f"{step_id}: step exits 0 (rc={r.returncode} {r.stderr.strip()[:200]})")
        skips = [ln.split("=", 1)[1] for ln in (dp / "out").read_text().splitlines()
                 if ln.startswith("skip=")]
        calls = (dp / "gh.log").read_text().splitlines() if (dp / "gh.log").exists() else []
        summary = (dp / "summary.md").read_text() if (dp / "summary.md").exists() else ""
        return (skips[-1] if skips else ""), calls, r.stdout + "\nSUMMARY:" + summary


print("action.yaml guard step")
skip, calls, _ = run_step("guard", pin="v0.69.5", version="v0.70.0",
                          open_prs=prs((758, "chore/bump-ai-core-v0.70.1")))
expect(skip == "true", "guard skips a bump an open newer bump already passes (the 11-20 race)")
expect(not any(c.startswith("pr close") for c in calls), "guard closes nothing")
skip, _, _ = run_step("guard", pin="v0.69.5", version="v0.70.1",
                      open_prs=prs((759, "chore/bump-ai-core-v0.70.0")))
expect(skip == "false", "guard proceeds when the open bump is older")
skip, calls, _ = run_step("guard", pin="v0.70.1", version="v0.70.0", open_prs=[])
expect(skip == "true", "guard still refuses to downgrade the pin on main")
skip, _, _ = run_step("guard", pin="v0.33.1", version="v0.33.2", short="order-core",
                      module="github.com/coderaxis/order-core",
                      companions="github.com/coderaxis/order-core-postgres@v0.21.2",
                      open_prs=prs((7, "chore/bump-order-core-postgres-v0.21.3")))
expect(skip == "true", "guard skips when an open bump moves a companion further")

print("action.yaml supersede step")
skip, calls, _ = run_step("supersede", pin="v0.69.5", version="v0.70.1",
                          open_prs=prs((759, "chore/bump-ai-core-v0.70.0"),
                                       (761, "chore/bump-ai-core-v0.70.1"),
                                       (5, "chore/bump-ai-core-postgres-v0.40.0")))
closed = [c.split()[2] for c in calls if c.startswith("pr close")]
expect(skip == "false" and closed == ["759"], f"only the older bump is closed: {closed}")
skip, calls, out = run_step("supersede", pin="v0.69.5", version="v0.70.0",
                            open_prs=prs((757, "chore/bump-ai-core-v0.69.9"),
                                         (758, "chore/bump-ai-core-v0.70.1")))
closed = [c.split()[2] for c in calls if c.startswith("pr close")]
expect(skip == "true" and closed == [], f"a newer bump opened mid-run: nothing closed, nothing opened: {closed}")
skip, calls, _ = run_step("supersede", pin="v0.69.5", version="v0.70.0", open_prs=[])
expect(skip == "false" and not any(c.startswith("pr close") for c in calls), "no open bumps: proceeds")

print("action.yaml wiring")
steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
gated = {s.get("id") or s.get("name"): s.get("if", "") for s in steps}
for sid in ("paths", "cpr"):
    expect("steps.supersede.outputs.skip != 'true'" in gated.get(sid, ""),
           f"step {sid} opens nothing when the supersede step skipped")
am = next(s for s in steps if s.get("name", "").startswith("Auto-merge"))
expect("steps.supersede.outputs.skip != 'true'" in am.get("if", ""), "auto-merge honours the supersede skip")
outv = yaml.safe_load(ACTION.read_text())["outputs"]["skipped"]["value"]
expect("steps.supersede.outputs.skip" in outv and "steps.guard.outputs.skip" in outv,
       "the skipped output reports either skip")

print("action.yaml auto-merge step")
AM_ID = next(s.get("id") for s in yaml.safe_load(ACTION.read_text())["runs"]["steps"]
             if s.get("name", "").startswith("Auto-merge"))
AM_ENV = {"PR": "42", "MOVES": "github.com/coderaxis/ai-core v0.70.1 v0.70.2"}
# GitHub's answer when the repository setting is off (enablePullRequestAutoMerge).
OFF = ('if [[ "$1 $2" == "pr merge" ]]; then echo "GraphQL: Auto merge is not allowed for this '
       'repository (enablePullRequestAutoMerge)" >&2; exit 1; fi\n'
       'if [[ "$1" == "api" ]]; then echo false; fi\n')
_, calls, out = run_step(AM_ID, pin="v0.70.1", version="v0.70.2", open_prs=[], gh_body=OFF, extra=AM_ENV)
warn = [ln for ln in out.splitlines() if ln.startswith("::warning")]
expect(any('"Allow auto-merge" OFF' in w and "allow_auto_merge=true" in w for w in warn),
       f"setting off: a warning names the setting and the fix: {warn}")
expect('"Allow auto-merge" OFF' in out.split("SUMMARY:", 1)[1], "setting off: the run summary says so too")
_, calls, out = run_step(AM_ID, pin="v0.70.1", version="v0.70.2", open_prs=[], extra=AM_ENV,
                         gh_body='if [[ "$1 $2" == "pr merge" ]]; then exit 0; fi\n')
expect(not any(ln.startswith("::warning") for ln in out.splitlines()) and
       any(c.startswith("pr merge 42") and "--auto" in c for c in calls),
       "auto-merge enabled: no warning")
_, calls, out = run_step(AM_ID, pin="v0.70.1", version="v0.70.2", open_prs=[], extra=AM_ENV,
                         gh_body='if [[ "$1 $2" == "pr merge" ]]; then echo "HTTP 502" >&2; exit 1; fi\n'
                                 'if [[ "$1" == "api" ]]; then echo true; fi\n')
warn = [ln for ln in out.splitlines() if ln.startswith("::warning")]
expect(any("HTTP 502" in w and "allow_auto_merge=true)" in w for w in warn),
       f"another failure: the warning carries GitHub's error, not the setting: {warn}")

print(f"\n{'FAILED' if FAILS else 'passed'}: {FAILS} failure(s)")
sys.exit(1 if FAILS else 0)
