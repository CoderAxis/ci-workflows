#!/usr/bin/env python3
"""Decide whether a push or pull request changed DEPENDENCY PINS AND NOTHING ELSE.

Why this exists. The central release cascade (ADR-0082) fans a version out to
every consumer of a released module: `platform-contracts-go` has 53 of them and
`platform-shared-go` has 90. Each fan-out arrives as an automated pull request
whose entire content is two lines of `go.mod` and the matching `go.sum` rows, and
because service CI runs on both `pull_request` and `push: main`, each one costs
two full pipelines. One `platform-shared-go` release therefore bills something
like 180 runs of a 28-job pipeline to answer a question about a version string.

Most of those jobs cannot possibly return a different answer. `structure` reads
the repository layout, `uuid-policy` and `org-vocabulary` scan Go source,
`dockerfile` reads the Dockerfile, `source-invariants` scans the checkout: under
a pin-only diff every one of those inputs is byte-identical to the run that
already passed on the base commit. They are not being re-checked, they are being
re-confirmed.

WHAT STILL RUNS is everything whose answer a dependency genuinely can change,
and that list is longer than it first looks:

  build-test          does it still compile and pass its tests - the whole point
  integration, e2e    a library change is a behaviour change
  vulnerabilities     a bump is EXACTLY when a CVE arrives; govulncheck reads go.mod
  dependency-review   its entire subject is the dependency delta
  openapi-generator   the generator library comes FROM platform-shared-go, so a
                      bump can legitimately change the generated spec
  contracts, contract a contracts-go bump moves the common.v1 projection (API-0007)
  artifact            the image and the GitOps pin still have to be built

So this is not "skip CI for bumps". It is "stop re-running the source scanners
against source that did not change", and the two jobs whose actual subject IS the
dependency delta - vulnerabilities and dependency-review - are both in the must-run
set. That is what makes skipping CodeQL defensible: CodeQL's input is first-party
source, which is identical, while the supply-chain question it might be thought to
cover is answered by two jobs that still run in full.

WHAT COUNTS AS A PIN is deliberately narrower than "the file is called go.mod",
because several things hide in that file that are not pins at all:

  go 1.26.6           a toolchain change recompiles everything and can move
  toolchain go1.26.6  gofmt output and CodeQL results, so it is not pin-only
  replace ...         redirects a module to a fork or a local path
  exclude, retract    change resolution rather than record a version
  module ...          renames the module itself

A diff touching any of those runs the full pipeline. Only `require` lines - the
version pins themselves - and `go.sum`, which is derived, qualify.

FAIL CLOSED, ALWAYS, on the same asymmetry as docs-only-diff.py: every path that
cannot prove the diff is pins answers `false` and runs everything. A wrong `false`
costs one pipeline; a wrong `true` merges code whose scanners never ran.

Exit codes:
  0  a verdict was reached (either verdict; `pin_only=` says which)
  2  the script could not do its job at all (bad arguments, unreadable repo)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Shared with docs-only-diff.py; see diffrange.py for why it is shared and not
# copied.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diffrange import (  # noqa: E402
    changed_paths,
    git,
    load_event,
    resolve_range,
)

# The only two filenames that can appear in a pin-only diff. Matched on basename
# so a nested module's go.mod counts too.
PIN_FILES = {"go.mod", "go.sum"}

# Directives that live in go.mod and are not version pins. A change to any of
# them is a change to how the module builds, not to which version it builds
# against, and the skipped scanners can legitimately answer differently after it.
NON_PIN_DIRECTIVES = ("go", "toolchain", "replace", "exclude", "retract", "module")


def cannot_run(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"::error::{message}")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Path-level classification
# ---------------------------------------------------------------------------

def is_pin_file(path: str) -> bool:
    p = path.strip()
    if not p or p.startswith("/") or ".." in p.split("/"):
        return False
    parts = p.split("/")
    # A go.mod under .github/ or in testdata/ is a fixture some gate reads, not
    # this module's pins. Same reasoning as docs-only-diff.py's deny list.
    for part in parts[:-1]:
        if part.startswith(".") or part in ("testdata", "vendor", "node_modules"):
            return False
    return parts[-1] in PIN_FILES


# ---------------------------------------------------------------------------
# Content-level classification
# ---------------------------------------------------------------------------

def significant_lines(repo: Path, base: str, head: str, path: str) -> list[str] | None:
    """The added and removed lines of `path` across the range, sans markers.

    `-U0` because context lines are not changes and would otherwise have to be
    filtered by position. Returns None when git cannot produce the diff, which
    the caller treats as "cannot tell".
    """
    rc, raw = git(repo, "diff", "--no-renames", "-U0", base, head, "--", path)
    if rc != 0:
        return None
    out: list[str] = []
    for line in raw.splitlines():
        if line.startswith(("+++", "---", "@@", "diff ", "index ", "new file",
                            "deleted file", "similarity ", "rename ")):
            continue
        if line.startswith(("+", "-")):
            body = line[1:].strip()
            if body:
                out.append(body)
    return out


def is_pin_line(line: str) -> bool:
    """True when this go.mod line records a version rather than a build rule."""
    text = line.strip()
    if not text or text.startswith("//"):
        return False
    # Structural lines of a require block carry no meaning on their own.
    if text in ("require (", ")", "require ("):
        return True
    first = text.split()[0].rstrip("(")
    if first in NON_PIN_DIRECTIVES:
        return False
    if first == "require":
        # `require example.com/m v1.2.3` on one line.
        return True
    # Inside a require block: `example.com/m v1.2.3 // indirect`. A module path
    # has a dot in its first element; anything else is a directive this script
    # does not recognise, and an unrecognised directive is not a pin.
    return "." in first or "/" in first


def go_mod_is_pins_only(repo: Path, base: str, head: str,
                        path: str) -> tuple[bool, str]:
    lines = significant_lines(repo, base, head, path)
    if lines is None:
        return False, f"git could not diff {path}"
    if not lines:
        # A go.mod in the path list whose diff is empty means the range added and
        # reverted it, or something this script did not model. Not evidence.
        return False, f"{path} is in the diff but has no changed lines"
    offenders = [ln for ln in lines if not is_pin_line(ln)]
    if offenders:
        shown = "; ".join(offenders[:4])
        return False, f"{path} changes more than pins: {shown}"
    return True, ""


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def classify(repo: Path, base: str, head: str,
             paths: set[str]) -> tuple[bool, str]:
    if not paths:
        # A range with no files is not evidence of a pin bump; it is evidence of
        # something this script did not model.
        return False, "the range touched no files"

    others = sorted(p for p in paths if not is_pin_file(p))
    if others:
        shown = others[:20]
        return False, (f"{len(others)} of {len(paths)} changed path(s) are not "
                       "dependency pins:\n  " + "\n  ".join(shown)
                       + ("\n  ..." if len(others) > len(shown) else ""))

    # Every path is a go.mod or go.sum. go.sum is derived, so only go.mod needs
    # reading, and it needs reading closely: a toolchain or replace change wears
    # the same filename as a pin bump.
    for path in sorted(p for p in paths if Path(p).name == "go.mod"):
        ok, why = go_mod_is_pins_only(repo, base, head, path)
        if not ok:
            return False, why

    return True, (f"{len(paths)} path(s) changed, all dependency pins:\n  "
                  + "\n  ".join(sorted(paths)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def emit(github_output: str | None, pin_only: bool, reason: str) -> None:
    value = "true" if pin_only else "false"
    line = f"pin_only={value}"
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
                    help="where to write the pin_only output")
    args = ap.parse_args()

    repo = Path(args.repo_root).resolve()
    if not repo.is_dir():
        cannot_run(f"--repo-root {repo} is not a directory")
    if git(repo, "rev-parse", "--git-dir")[0] != 0:
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

    pin_only, detail = classify(repo, base, head, paths)
    emit(args.github_output, pin_only, f"{reason}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
