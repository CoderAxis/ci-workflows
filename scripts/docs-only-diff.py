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

A dependency-pin change is code here, and deliberately so: `go.mod` is read as
input by build, vet, govulncheck and dependency review. It gets its own, much
narrower fast path in pin-only-diff.py, which skips a different and smaller set
of jobs for a different reason.

Exit codes:
  0  a verdict was reached (either verdict; `docs_only=` says which)
  2  the script could not do its job at all (bad arguments, unreadable repo)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path

# The range plumbing is shared with pin-only-diff.py. Both ask which commits an
# event describes and which paths they touched; only the verdict differs, and the
# subtle parts -- force pushes, shallow clones, merge bases, renames, reverted
# commits inside one push -- are each a way to answer "skippable" about a diff
# that is not. One copy, so one of them cannot be fixed in one place only.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diffrange import (  # noqa: E402
    changed_paths,
    excluded_roots,
    git,
    load_event,
    resolve_range,
)

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
# go:embed - a .md file compiled into the binary is not documentation
# ---------------------------------------------------------------------------

def embed_patterns(root: Path) -> list[str]:
    """Repo-relative glob patterns named by `//go:embed` directives.

    A markdown file embedded into a binary is program input: its content is
    compiled in, and tests assert on it. Changing one is a code change wearing a
    documentation extension, so any doc path a directive could match takes the
    full pipeline.
    """
    excluded = excluded_roots(root)
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
