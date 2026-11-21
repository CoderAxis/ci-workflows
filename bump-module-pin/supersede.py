#!/usr/bin/env python3
"""Decide which open bump PRs a bump supersedes, and whether an open one supersedes the bump.

Reads `gh pr list --json number,headRefName` (a JSON array) on stdin. Each `--move short=vX.Y.Z`
is one module this change moves: the primary first, then every companion. A bump PR this action
opened is named `chore/bump-<short>-<version>`, so its head says which module it moves and to
which version.

Prints one line per finding:

    close <number> <short> <version>    an open PR for that module at the same or an older version
    newer <number> <short> <version>    an open PR for that module at a NEWER version
    unknown <number> <head>             a head that names the module but no plain vX.Y.Z version

The caller closes the `close` PRs only when there is no `newer` line, and when there is one it
opens nothing: the newer PR already carries the module past this version.

Why the comparison: the old step closed every other bump PR of the module without comparing
versions. Bumps arrive out of order (a release fan-out and the cascade reconciler run at the
same time, and the reconciler plans with the versions that were latest when it started). On
2026-11-20 a reconcile planned at ai-core v0.70.0 closed the open v0.70.1 bump of the same
consumer, opened and auto-merged v0.70.0, and left the next bump (v0.70.2) in conflict. The guard
step's downgrade check could not see it: it compares with the pin on main, and main was older
than both.

A head whose version does not parse is left alone (reported as `unknown`): this action never
opens one, so it was made by hand, and closing a branch whose version cannot be ordered is a
guess. When this bump's own version does not parse, nothing is compared at all.

Standard library only: it runs inside the consumer's job on any runner.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse(version: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(version)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def decide(prs: list[dict], moves: list[tuple[str, str]], new_branch: str) -> list[str]:
    out: list[str] = []
    for short, version in moves:
        mine = parse(version)
        if mine is None:
            continue
        # `-v` ends the prefix, so `ai-core` never matches `ai-core-postgres`'s branches.
        prefix = f"chore/bump-{short}-"
        for pr in sorted(prs, key=lambda p: p["number"]):
            head = pr.get("headRefName", "")
            if head == new_branch or not head.startswith(prefix + "v"):
                continue
            theirs = parse(head[len(prefix):])
            if theirs is None:
                out.append(f"unknown {pr['number']} {head}")
            elif theirs > mine:
                out.append(f"newer {pr['number']} {short} {head[len(prefix):]}")
            else:
                out.append(f"close {pr['number']} {short} {head[len(prefix):]}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--new-branch", required=True, help="the branch this bump opens")
    ap.add_argument("--move", action="append", default=[], metavar="SHORT=VERSION",
                    help="a module this change moves, primary first; repeatable")
    args = ap.parse_args(argv)
    moves = []
    for m in args.move:
        short, sep, version = m.partition("=")
        if not sep or not short or not version:
            ap.error(f"--move wants SHORT=VERSION, got {m!r}")
        moves.append((short, version))
    if not moves:
        ap.error("at least one --move is required")
    prs = json.load(sys.stdin)
    if not isinstance(prs, list):
        ap.error("stdin must be the JSON array `gh pr list --json number,headRefName` prints")
    for line in decide(prs, moves, args.new_branch):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
