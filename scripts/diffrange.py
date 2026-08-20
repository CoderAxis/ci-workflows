#!/usr/bin/env python3
"""What a push or pull request changed, resolved once for every classifier.

This is the plumbing under `docs-only-diff.py` and `pin-only-diff.py`. Both ask
the same two questions -- which commits does this event describe, and which paths
did they touch -- and then disagree only about what the answer means.

It is shared rather than copied because the subtlety is all in here. A force push
is a history rewrite and not a change set; the before-SHA of a push is exactly the
object a shallow clone lacks; a long-lived pull request must diff against the merge
base rather than the moving base tip; rename detection hides the source path of a
`git mv`; and a later commit reverting an earlier one in the same push makes the
net tree diff an incomplete reading. Every one of those is a way to answer "yes,
skippable" about a diff that is not, and a second copy of this logic is a second
place for one of them to be got wrong.

FAIL CLOSED. Every path that cannot produce a trustworthy range returns None, and
every caller is expected to treat None as "run everything". The asymmetry is the
point: a wrong "cannot tell" costs one pipeline, a wrong "skippable" costs an
unchecked merge to main.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ZERO_SHA = "0" * 40


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def commit_exists(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0


def ensure_commit(repo: Path, sha: str) -> bool:
    """Make `sha` readable, fetching it if the clone is shallow.

    A shallow clone is the normal shape for `actions/checkout` defaults, and the
    base commit of a range is exactly the object it does not have. One deepening
    fetch is worth trying; if it fails we run everything rather than guess.
    """
    if commit_exists(repo, sha):
        return True
    for extra in (["--deepen=50"], ["origin", sha]):
        git(repo, "fetch", "--no-tags", "--quiet", *extra)
        if commit_exists(repo, sha):
            return True
    return False


def is_ancestor(repo: Path, base: str, head: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", base, head)[0] == 0


# ---------------------------------------------------------------------------
# Range resolution - one answer per trigger, and no answer is a valid answer
# ---------------------------------------------------------------------------

def resolve_range(repo: Path, event_name: str, event: dict,
                  head_sha: str) -> tuple[str | None, str | None, str]:
    """Return (base, head, reason). base is None when no range can be trusted."""

    if event_name == "push":
        before = str(event.get("before") or "")
        after = str(event.get("after") or "") or head_sha
        if event.get("deleted"):
            return None, None, "branch deletion carries no diff"
        if event.get("created") or not before or set(before) == {"0"} or before == ZERO_SHA:
            # First push of a branch. GitHub sends an all-zero before-SHA and
            # there is no predecessor tree to compare against.
            return None, None, "first push of this ref: no before-SHA to diff against"
        if event.get("forced"):
            # The pushed history is not a descendant of what was there, so
            # `before..after` describes a rewrite rather than a change set.
            return None, None, "force push: the range is a history rewrite, not a change set"
        if not after:
            return None, None, "push event carries no head SHA"
        if not ensure_commit(repo, before):
            return None, None, f"before-SHA {before[:12]} is not in this clone"
        if not ensure_commit(repo, after):
            return None, None, f"head SHA {after[:12]} is not in this clone"
        if not is_ancestor(repo, before, after):
            # Belt and braces for a force push the payload did not flag.
            return None, None, f"{before[:12]} is not an ancestor of {after[:12]}"
        return before, after, f"push range {before[:12]}..{after[:12]}"

    if event_name in ("pull_request", "pull_request_target"):
        pr = event.get("pull_request") or {}
        base = str(((pr.get("base") or {}).get("sha")) or "")
        head = str(((pr.get("head") or {}).get("sha")) or "")
        if not base or not head:
            return None, None, "pull_request payload carries no base/head SHA"
        if not ensure_commit(repo, base) or not ensure_commit(repo, head):
            return None, None, "pull request base or head is not in this clone"
        # The merge base, not base.sha: the base branch moves under a long-lived
        # pull request, and diffing against its tip would attribute other
        # people's commits to this one - in the direction that ADDS files, so it
        # would only ever be over-conservative, but it would also mark an
        # otherwise skippable PR as code for as long as someone else's Go change
        # sat unmerged ahead of it.
        rc, out = git(repo, "merge-base", base, head)
        if rc != 0 or not out.strip():
            return None, None, "no merge base between the pull request base and head"
        mb = out.strip()
        return mb, head, f"pull request range {mb[:12]}..{head[:12]}"

    # workflow_dispatch, schedule, repository_dispatch, workflow_run, release,
    # and anything this pipeline has not met yet. None of them describes a set
    # of commits, and workflow_dispatch in particular is how a central pipeline
    # change is picked up in a service repo - the one run that must be complete.
    return None, None, f"{event_name} describes no commit range; running everything"


# ---------------------------------------------------------------------------
# Changed paths
# ---------------------------------------------------------------------------

def changed_paths(repo: Path, base: str, head: str) -> set[str] | None:
    """Every path the range touched, by two independent readings.

    `git diff base head` is the NET tree difference, which is what the head
    commit will actually be tested as. `git log --name-only base..head` is the
    UNION of what each commit in the range touched, which additionally catches
    a code change that a later commit in the same push reverted. Taking both
    means a multi-commit push whose head happens to look skippable still runs
    the full pipeline, and it means a merge commit (which contributes to the
    tree diff but lists no files of its own in `git log`) cannot smuggle
    anything past the union reading.

    `--no-renames` is load-bearing. With rename detection on - the default -
    `git diff --name-only` prints only the DESTINATION path of a rename, so
    `git mv internal/handler.go docs/handler.md` would present as a single
    documentation path while deleting a source file. Reported as delete + add,
    both paths are seen and the diff is code.
    """
    out: set[str] = set()

    rc, raw = git(repo, "diff", "--no-renames", "--name-only", "-z", base, head)
    if rc != 0:
        return None
    out.update(p for p in raw.split("\0") if p)

    rc, raw = git(repo, "log", "--no-renames", "--format=", "--name-only", "-z",
                  f"{base}..{head}")
    if rc != 0:
        return None
    out.update(p for p in raw.split("\0") if p)

    return out


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------

def load_event(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"::warning::cannot read the event payload at {path}: {exc}; "
              "running the full pipeline")
        return {}
    return doc if isinstance(doc, dict) else {}


def excluded_roots(root: Path) -> list[Path]:
    """Directories under `root` that are this checker's own checkout.

    The reusable workflow checks ci-workflows out INSIDE the caller's tree -
    `actions/checkout` cannot place a repository anywhere else - so a walk of
    the caller's repository also walks ours. Computed from where this file
    resolves to rather than from a directory name, because the name is the
    workflow's choice and differs per job. Same instrument, same reason, as
    check-ci-identity.py and check-org-vocabulary.py.
    """
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        return []
    return [REPO_ROOT] if root in REPO_ROOT.parents else []
