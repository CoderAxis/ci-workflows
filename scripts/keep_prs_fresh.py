#!/usr/bin/env python3
"""Update every open auto-merge PR that is BEHIND its base branch (keep-prs-fresh.yaml).

  REPO=owner/name BASE=main GH_TOKEN=<app token> python3 scripts/keep_prs_fresh.py

A PR is updated only when auto-merge is enabled on it and GitHub reports it BEHIND: a ruleset with
strict required checks will not merge it until its checks ran on the current base. A PR that
conflicts with the base (DIRTY) is named in a notice, since only a person can resolve it. GitHub
computes merge state lazily after the base moves, so an UNKNOWN state is asked again a few times
before it is reported and left to the next push.

The update is a merge of the base into the head branch (PUT .../update-branch), pinned to the head
commit read here, so a push that lands in between is never overwritten: GitHub answers 422 and the
PR is left for its author's new push to be judged on.

Exit 1 when the PRs could not be listed or an update failed for any reason but that race or a
conflict; exit 0 otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

FIELDS = "number,isDraft,headRefOid,mergeStateStatus,autoMergeRequest,isCrossRepository,maintainerCanModify"


def decide(pr: dict) -> tuple[str, str]:
    """(action, reason) for one open PR. Actions: update, conflict, unknown, skip."""
    if not pr.get("autoMergeRequest"):
        return "skip", "auto-merge is not enabled"
    if pr.get("isDraft"):
        return "skip", "draft"
    if pr.get("isCrossRepository") and not pr.get("maintainerCanModify"):
        return "skip", "its branch is in a fork that does not allow maintainer edits"
    state = pr.get("mergeStateStatus") or "UNKNOWN"
    if state == "BEHIND":
        return "update", "behind the base branch"
    if state == "DIRTY":
        return "conflict", "conflicts with the base branch; a person must resolve it"
    if state == "UNKNOWN":
        return "unknown", "GitHub has not worked out its merge state yet; the next push retries it"
    return "skip", f"merge state {state}"


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def list_prs(repo: str, base: str) -> list[dict]:
    p = gh("pr", "list", "--repo", repo, "--base", base, "--state", "open", "--limit", "200",
           "--json", FIELDS)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return json.loads(p.stdout or "[]")


def plan(repo: str, base: str, attempts: int, wait: float) -> list[tuple[dict, str, str]]:
    """Decide every PR, asking again while any auto-merge PR's state is still UNKNOWN."""
    for attempt in range(1, attempts + 1):
        decided = [(pr, *decide(pr)) for pr in list_prs(repo, base)]
        if attempt == attempts or not any(a == "unknown" for _, a, _ in decided):
            return decided
        time.sleep(wait * attempt)
    return []


def update(repo: str, pr: dict) -> tuple[str, str]:
    """('updated'|'raced'|'failed', detail)."""
    p = gh("api", "-X", "PUT", f"repos/{repo}/pulls/{pr['number']}/update-branch",
           "-f", f"expected_head_sha={pr['headRefOid']}")
    if p.returncode == 0:
        return "updated", f"merged the base into it at head {pr['headRefOid'][:12]}"
    err = (p.stderr.strip() or p.stdout.strip()).replace("\n", " ")
    # 422 is GitHub refusing on the facts, not failing: the head moved since it was read, or the
    # base no longer merges cleanly. Either way the PR now waits on a person or its next push.
    if "HTTP 422" in err:
        return "raced", err
    return "failed", err


def main() -> int:
    repo, base = os.environ["REPO"], os.environ.get("BASE") or "main"
    attempts = int(os.environ.get("KEEP_PRS_FRESH_ATTEMPTS", "6"))
    wait = float(os.environ.get("KEEP_PRS_FRESH_WAIT", "5"))
    try:
        decided = plan(repo, base, attempts, wait)
    except RuntimeError as e:
        print(f"::error title=Cannot list pull requests::{repo}: {e}")
        return 1

    rows, failed = [], 0
    for pr, action, reason in decided:
        n = pr["number"]
        if action == "update":
            result, detail = update(repo, pr)
            if result == "updated":
                print(f"#{n}: updated ({reason}); {detail}")
            elif result == "raced":
                print(f"::notice title=#{n} not updated::{detail}")
            else:
                failed += 1
                print(f"::error title=#{n} update failed::{detail}")
            rows.append((n, result, detail))
        elif action in ("conflict", "unknown"):
            print(f"::notice title=#{n} not updated::{reason}")
            rows.append((n, action, reason))
        else:
            print(f"#{n}: left alone ({reason})")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Auto-merge PRs brought up to date with `{base}`\n\n")
            if rows:
                f.write("| PR | result | detail |\n| -- | ------ | ------ |\n")
                f.writelines(f"| #{n} | {r} | {d} |\n" for n, r, d in rows)
            else:
                f.write("No auto-merge PR was behind.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
