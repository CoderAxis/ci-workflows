#!/usr/bin/env python3
"""Tests for keep-prs-fresh: the decision per PR (scripts/keep_prs_fresh.py decide()), the script
run as the workflow runs it against a fake `gh`, and the workflow's wiring.

The fake `gh` is the layer that matters here: the script is what calls update-branch, so it is run,
not only its decision function. A right decision is no use if the call goes to the wrong PR, drops
the expected head, or a GitHub refusal turns the run red.
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
SCRIPT = ROOT / "scripts" / "keep_prs_fresh.py"
WORKFLOW = ROOT / ".github" / "workflows" / "keep-prs-fresh.yaml"
sys.path.insert(0, str(SCRIPT.parent))
import keep_prs_fresh as kf  # noqa: E402

FAILS = 0


def expect(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'}  {msg}")
    FAILS += 0 if cond else 1


def pr(n: int, state: str, auto: bool = True, **kw) -> dict:
    return {"number": n, "isDraft": False, "headRefOid": f"{n:02d}" + "a" * 38, "mergeStateStatus": state,
            "autoMergeRequest": {"enabledAt": "2026-11-22T00:00:00Z"} if auto else None,
            "isCrossRepository": False, "maintainerCanModify": False, **kw}


print("keep_prs_fresh.py decide()")
expect(kf.decide(pr(1, "BEHIND"))[0] == "update", "an auto-merge PR that is behind is updated")
expect(kf.decide(pr(2, "BEHIND", auto=False))[0] == "skip", "a PR without auto-merge is left to its author")
expect(kf.decide(pr(3, "DIRTY"))[0] == "conflict", "a conflicting PR is named, not updated")
expect(kf.decide(pr(4, "UNKNOWN"))[0] == "unknown", "an unknown state is reported, not guessed")
expect(kf.decide(pr(5, None))[0] == "unknown", "a missing state reads as unknown")
for state in ("CLEAN", "BLOCKED", "UNSTABLE", "HAS_HOOKS"):
    expect(kf.decide(pr(6, state))[0] == "skip", f"{state} is not behind: nothing to do")
expect(kf.decide(pr(7, "BEHIND", isDraft=True))[0] == "skip", "a draft is left alone")
expect(kf.decide(pr(8, "BEHIND", isCrossRepository=True))[0] == "skip",
       "a fork branch that maintainers cannot edit is left alone")
expect(kf.decide(pr(9, "BEHIND", isCrossRepository=True, maintainerCanModify=True))[0] == "update",
       "a fork branch maintainers may edit is updated")

# The fake gh: `pr list` answers the next of the listing files in turn (the last one repeats), and
# `api -X PUT .../pulls/<n>/update-branch` fails when FAIL_<n> names an HTTP status.
FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [[ "$1 $2" == "pr list" ]]; then
  [[ -n "${LIST_FAILS:-}" ]] && { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
  i=$(cat "$GH_DIR/i" 2>/dev/null || echo 0); i=$((i+1)); echo "$i" > "$GH_DIR/i"
  f="$GH_DIR/list.$i.json"; [[ -f "$f" ]] || f="$(ls "$GH_DIR"/list.*.json | sort -V | tail -1)"
  cat "$f"; exit 0
fi
if [[ "$1 $2 $3" == "api -X PUT" ]]; then
  n="$(echo "$4" | sed -E 's#.*/pulls/([0-9]+)/update-branch#\1#')"
  v="FAIL_$n"
  if [[ -n "${!v:-}" ]]; then echo "gh: Unprocessable (HTTP ${!v})" >&2; exit 1; fi
  echo '{"message":"Updating pull request branch."}'; exit 0
fi
echo "unexpected gh call: $*" >&2; exit 3
"""


def run(listings: list[list[dict]], env_extra: dict | None = None) -> tuple[int, list[str], str, str]:
    """Runs the script against the fake gh; returns (exit code, gh calls, stdout, step summary)."""
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        for i, listing in enumerate(listings, 1):
            (dp / f"list.{i}.json").write_text(json.dumps(listing))
        (dp / "bin").mkdir()
        gh = dp / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        env = {"PATH": f"{dp / 'bin'}:{os.environ['PATH']}", "GH_LOG": str(dp / "gh.log"), "GH_DIR": d,
               "REPO": "acme/svc", "BASE": "main", "GH_TOKEN": "x", "KEEP_PRS_FRESH_WAIT": "0",
               "KEEP_PRS_FRESH_ATTEMPTS": "3", "GITHUB_STEP_SUMMARY": str(dp / "summary.md"),
               **(env_extra or {})}
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
        calls = (dp / "gh.log").read_text().splitlines() if (dp / "gh.log").exists() else []
        summary = (dp / "summary.md").read_text() if (dp / "summary.md").exists() else ""
        return r.returncode, calls, r.stdout + r.stderr, summary


def puts(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("api -X PUT")]


print("keep_prs_fresh.py against a fake gh")
rc, calls, out, summary = run([[pr(10, "BEHIND"), pr(11, "BEHIND", auto=False), pr(12, "DIRTY"),
                                pr(13, "CLEAN"), pr(14, "BLOCKED")]])
expect(rc == 0, f"exit 0 when every PR was handled (rc={rc})")
expect(puts(calls) == [f"api -X PUT repos/acme/svc/pulls/10/update-branch -f expected_head_sha={pr(10, '')['headRefOid']}"],
       f"only the behind auto-merge PR is updated, pinned to the head it was read at: {puts(calls)}")
expect("::notice title=#12 not updated::" in out, "the conflicting PR is named in a notice")
expect("| #10 | updated |" in summary and "| #12 | conflict |" in summary, "the run summary lists both")
lists = [c for c in calls if c.startswith("pr list")]
expect(len(lists) == 1 and "--base main" in lists[0] and "--state open" in lists[0],
       f"one listing of the open PRs into the base branch: {lists}")

rc, calls, out, _ = run([[pr(20, "UNKNOWN")], [pr(20, "BEHIND")]])
expect(rc == 0 and len(puts(calls)) == 1 and len([c for c in calls if c.startswith("pr list")]) == 2,
       "an UNKNOWN state is asked again, and updated once GitHub says BEHIND")

rc, calls, out, _ = run([[pr(21, "UNKNOWN")]])
expect(rc == 0 and not puts(calls) and len([c for c in calls if c.startswith("pr list")]) == 3
       and "::notice title=#21 not updated::" in out,
       "a state that stays UNKNOWN is asked the set number of times, then noticed, not failed")

rc, calls, out, _ = run([[pr(22, "UNKNOWN", auto=False), pr(23, "CLEAN")]])
expect(len([c for c in calls if c.startswith("pr list")]) == 1,
       "an UNKNOWN PR without auto-merge does not hold the run up")

rc, calls, out, _ = run([[pr(30, "BEHIND"), pr(31, "BEHIND")]], {"FAIL_30": "422"})
expect(rc == 0 and len(puts(calls)) == 2 and "::notice title=#30 not updated::" in out,
       "a 422 (head moved, or no longer merges cleanly) is a notice, and the next PR is still updated")

rc, calls, out, _ = run([[pr(32, "BEHIND"), pr(33, "BEHIND")]], {"FAIL_32": "403"})
expect(rc == 1 and len(puts(calls)) == 2 and "::error title=#32 update failed::" in out,
       "any other refusal fails the run, after trying the rest")

rc, calls, out, _ = run([[]], {"LIST_FAILS": "1"})
expect(rc == 1 and "::error title=Cannot list pull requests::" in out, "a listing failure fails the run")

rc, calls, out, summary = run([[]])
expect(rc == 0 and not puts(calls) and "No auto-merge PR was behind." in summary, "no open PRs: nothing to do")

print("keep-prs-fresh.yaml wiring")
wf = yaml.safe_load(WORKFLOW.read_text())
on = wf.get("on", wf.get(True))
expect(set(on) == {"workflow_call"}, f"reusable only, so the release bot admits it by its own name: {set(on)}")
expect((wf.get("concurrency") or {}).get("cancel-in-progress") is False, "runs queue, never cancel")
job = wf["jobs"]["update"]
expect("github.event_name == 'push'" in job.get("if", "") and "default_branch" in job.get("if", ""),
       "runs only on a push to the default branch")
expect((job.get("permissions") or {}).get("id-token") == "write", "the job can prove its identity to mint")
expect(job.get("continue-on-error") is True,
       "a failed update never fails the caller's ci.yaml run, which a release requires to succeed")
steps = job["steps"]
mint = next(s for s in steps if str(s.get("uses", "")).startswith("coderaxis/ci-workflows/ci-app-token@"))
expect(mint["with"].get("scope") == "publish",
       "mints the release bot: a GITHUB_TOKEN push starts no CI, so the PR would stay BLOCKED")
expect(mint["with"].get("repositories") == "${{ github.event.repository.name }}"
       and mint["with"].get("owner") == "${{ github.repository_owner }}",
       "the token reaches the calling repository and nothing else")
upd = next(s for s in steps if "keep_prs_fresh.py" in str(s.get("run", "")))
expect(upd["env"].get("GH_TOKEN") == f"${{{{ steps.{mint['id']}.outputs.token }}}}",
       f"the update uses the minted token, never github.token: {upd['env'].get('GH_TOKEN')}")
checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
expect(checkout["with"].get("repository") == "coderaxis/ci-workflows" and checkout["with"].get("ref") == "v1"
       and upd["run"].split()[-1] == f"{checkout['with']['path']}/scripts/keep_prs_fresh.py",
       "the script runs from the central checkout at v1")

print(f"\n{'FAILED' if FAILS else 'passed'}: {FAILS} failure(s)")
sys.exit(1 if FAILS else 0)
