#!/usr/bin/env python3
"""Decide whether a push or pull request changed DOCUMENTATION AND NOTHING ELSE.

Why this exists. 111 of the 120 caller repositories carry no `paths:` filter on
their ci.yaml, so a one-word fix to a README runs build, vet, unit tests,
integration tests, CodeQL, gitleaks, dependency review, schema compatibility,
the artifact build and the GitOps pin. That bill is large enough that people
stop pushing documentation, which is the worst possible outcome for a fleet
whose docs are themselves gated.

Why not `paths-ignore:`. A workflow skipped by a path filter reports NO status
at all, and a required status check then waits forever for a check that will
never arrive - the pull request is unmergeable and there is nothing to click.
So the workflow always runs, this script decides once whether the diff is
documentation, and the expensive JOBS carry `if:` conditions on the answer. A
job skipped by `if:` reports a real `skipped` conclusion, which branch
protection accepts and which `contains(needs.*.result, 'failure')` does not
mistake for a failure.

FAIL CLOSED, ALWAYS. Every path through this script that cannot prove the diff
is documentation answers `false`, which runs the entire pipeline. An unreadable
event payload, an absent before-SHA, a force push, a commit the clone does not
contain, a shallow clone missing the base, an unknown event - all of them run
everything. The cost of a wrong `false` is a wasted pipeline; the cost of a
wrong `true` is unreviewed code on main.

WHAT COUNTS AS DOCUMENTATION is deliberately narrow: `*.md` outside `.github/`
and outside any `testdata/` directory, a handful of extensionless legal and
changelog files, and images under `docs/`. Everything else - every YAML and
JSON file, `go.mod`, `go.sum`, every lockfile, `sqlc.yaml`, SQL and migrations,
Dockerfiles, anything under `.github/` or any other dot-directory - is code as
far as this gate is concerned, because each of those is read as input by some
gate. One non-documentation path in the diff runs the whole pipeline.

Exit codes:
  0  a verdict was reached (either verdict; `docs_only=` says which)
  2  the script could not do its job at all (bad arguments, unreadable repo)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ZERO_SHA = "0" * 40

# Documentation, and nothing that any gate reads as input.
DOC_SUFFIXES = {".md", ".markdown"}
# Images, but only where documentation keeps them. An .svg in a frontend repo
# is a source asset that ships to a browser, not a diagram in a design note.
DOC_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
DOC_IMAGE_ROOTS = ("docs/",)
# Extensionless files that are documentation by convention.
DOC_EXACT_NAMES = {
    "LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CONTRIBUTORS",
    "CHANGELOG", "CONTRIBUTING", "CODE_OF_CONDUCT",
}
# Directory names that make a path code no matter what it is called. `testdata`
# is the Go convention for fixtures a test reads, so a .md in one is a test
# input; `.github` and every other dot-directory holds configuration that gates
# and the workflow engine read.
DENY_DIR_NAMES = {"testdata"}

GO_EMBED_RE = re.compile(r"^\s*//go:embed\s+(.+?)\s*$")


def cannot_run(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"::error::{message}")
    sys.exit(2)


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
        # would only ever be over-conservative, but it would also mark a
        # documentation-only PR as code for as long as someone else's Go change
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
    means a multi-commit push whose head happens to look documentation-shaped
    still runs the full pipeline, and it means a merge commit (which contributes
    to the tree diff but lists no files of its own in `git log`) cannot smuggle
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
# go:embed - a .md file compiled into the binary is not documentation
# ---------------------------------------------------------------------------

def _excluded_roots(root: Path) -> list[Path]:
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


def embed_patterns(root: Path) -> list[str]:
    """Repo-relative glob patterns named by `//go:embed` directives.

    A markdown file embedded into a binary is program input: its content is
    compiled in, and tests assert on it. Changing one is a code change wearing a
    documentation extension, so any doc path a directive could match takes the
    full pipeline.
    """
    excluded = _excluded_roots(root)
    patterns: list[str] = []
    for go_file in root.rglob("*.go"):
        if any(part.startswith(".") or part in ("vendor", "node_modules")
               for part in go_file.relative_to(root).parts[:-1]):
            continue
        if any(go_file == ex or ex in go_file.parents for ex in excluded):
            continue
        try:
            text = go_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "go:embed" not in text:
            continue
        pkg_dir = go_file.parent.relative_to(root)
        for line in text.splitlines():
            m = GO_EMBED_RE.match(line)
            if not m:
                continue
            try:
                tokens = shlex.split(m.group(1))
            except ValueError:
                tokens = m.group(1).split()
            for token in tokens:
                # `all:` and `-` prefixes are go:embed's own qualifiers.
                token = token.split(":", 1)[-1].lstrip("-")
                if not token:
                    continue
                rel = os.path.normpath(os.path.join(str(pkg_dir), token))
                patterns.append(rel.replace(os.sep, "/").lstrip("./"))
    return patterns


def is_embedded(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat in (".", ""):
            return True
        if path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("/") + "/*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_documentation(path: str, embeds: list[str]) -> bool:
    p = path.strip()
    if not p or p.startswith("/") or ".." in p.split("/"):
        return False

    parts = p.split("/")
    for part in parts[:-1]:
        # `.github/` and every other dot-directory holds configuration, not
        # documentation; `testdata/` holds inputs a test reads.
        if part.startswith(".") or part in DENY_DIR_NAMES:
            return False
    name = parts[-1]
    if name.startswith("."):
        return False

    suffix = Path(name).suffix.lower()

    if suffix in DOC_SUFFIXES:
        return not is_embedded(p, embeds)
    if suffix in DOC_IMAGE_SUFFIXES and p.startswith(DOC_IMAGE_ROOTS):
        return not is_embedded(p, embeds)
    if not suffix and name in DOC_EXACT_NAMES:
        return not is_embedded(p, embeds)
    return False


def classify(paths: set[str], embeds: list[str]) -> tuple[bool, list[str]]:
    """(docs_only, the paths that are not documentation)."""
    if not paths:
        # A range with no files is not evidence of a documentation change; it is
        # evidence of something this script did not model.
        return False, []
    code = sorted(p for p in paths if not is_documentation(p, embeds))
    return (not code), code


# ---------------------------------------------------------------------------
# Entry point
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


def emit(github_output: str | None, docs_only: bool, reason: str) -> None:
    value = "true" if docs_only else "false"
    line = f"docs_only={value}"
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line)
    print(f"reason: {reason}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".", help="the repository under test")
    ap.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""),
                    help="the triggering event")
    ap.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"),
                    help="the event payload JSON")
    ap.add_argument("--head-sha", default=os.environ.get("GITHUB_SHA", ""),
                    help="fallback head SHA when the payload has none")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                    help="where to write the docs_only output")
    args = ap.parse_args()

    repo = Path(args.repo_root).resolve()
    if not repo.is_dir():
        cannot_run(f"--repo-root {repo} is not a directory")
    if git(repo, "rev-parse", "--git-dir")[0] != 0:
        # No git, no range, no verdict: run everything.
        emit(args.github_output, False, f"{repo} is not a git repository")
        return 0
    if not args.event_name:
        emit(args.github_output, False, "no event name; running everything")
        return 0

    event = load_event(args.event_path)
    base, head, reason = resolve_range(repo, args.event_name, event, args.head_sha)
    if base is None or head is None:
        emit(args.github_output, False, reason)
        return 0

    paths = changed_paths(repo, base, head)
    if paths is None:
        emit(args.github_output, False, f"{reason}: git could not read the diff")
        return 0

    embeds = embed_patterns(repo)
    docs_only, code = classify(paths, embeds)

    if docs_only:
        detail = (f"{reason}: {len(paths)} path(s) changed, all documentation:\n  "
                  + "\n  ".join(sorted(paths)))
    else:
        shown = code[:20]
        detail = (f"{reason}: {len(code)} of {len(paths)} changed path(s) are not "
                  f"documentation:\n  " + "\n  ".join(shown)
                  + ("\n  ..." if len(code) > len(shown) else ""))
    emit(args.github_output, docs_only, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
