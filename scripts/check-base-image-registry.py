#!/usr/bin/env python3
"""Fail when a Dockerfile pulls a base image from Docker Hub by its bare name.

WHY THIS IS A GATE AND NOT A STYLE RULE. CodeBuild instances share a NAT address, and
Docker Hub's anonymous pull quota is 100 requests per 6 hours per IP. A fleet-wide bump
triggers canonical builds across ~91 repositories at once and exhausts it, after which the
next build that needs a fresh pull dies with HTTP 429 before it writes a digest. The
failure surfaces as "canonical build FAILED" with no image name in the GitHub log, so it
reads as a defect in whatever commit happened to be building.

That has now happened twice. inboxxhq-web-console was moved to the AWS-managed mirror on
2026-08-14 after the Go 1.26.6 bump exhausted the quota; inboxxhq-web-app and
inboxxhq-web-www kept the bare names and lost two builds to the same 429 on 2026-09-11,
where it was first misread as a frontend-core pin having broken the image build.

public.ecr.aws/docker/library/ is AWS's mirror of Docker Hub's official images. From
CodeBuild it is AWS-to-AWS traffic authenticated by the instance role and is not rate
limited. GitHub Actions runners pull through GitHub's own authenticated Docker Hub account
with a far higher quota, which is why repository CI stays green while the deploy lane dies
-- the discrepancy is the reason a human reviewer does not catch this.

    python3 scripts/check-base-image-registry.py [--root DIR] [PATH ...]

Exit 0 when every FROM names a registry, 1 on violations, 2 on a bad invocation.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A registry host is a first path segment containing a dot or a colon, or the literal
# "localhost" -- the same rule Docker itself uses to tell a registry from a namespace.
# "node:20-alpine" has no host and resolves to Docker Hub; "public.ecr.aws/..." does.
HOSTED_RE = re.compile(r"^(localhost(:\d+)?|[^/]+[.:][^/]*)/")
FROM_RE = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)

MIRROR = "public.ecr.aws/docker/library/"


def image_token(rest: str) -> str:
    """The image reference on a FROM line, skipping --platform= and friends."""
    for tok in rest.split():
        if not tok.startswith("--"):
            return tok
    return ""


def stage_names(lines: list[str]) -> set[str]:
    names = set()
    for line in lines:
        m = FROM_RE.match(line)
        if not m:
            continue
        parts = m.group("rest").split()
        for i, tok in enumerate(parts):
            if tok.upper() == "AS" and i + 1 < len(parts):
                names.add(parts[i + 1].lower())
    return names


SKIP_DIRS = {"node_modules", "vendor"}


def skipped(path: Path, root: Path) -> bool:
    """True for a Dockerfile that is not part of the image this repository ships.

    DOT-DIRECTORIES ARE LOAD-BEARING HERE, not tidiness. frontend-image-ci.yaml checks
    ci-workflows out INTO the repository under test as `.coderaxis-ci`, so an rglob of the
    caller's tree also walks THIS checker's own tree -- including
    scripts/testdata/base-image-registry/dirty/, which exists to contain exactly what the
    guard rejects. Without this the gate fails every repository it is added to, blaming the
    caller for the checker's own fixture. The sibling guard check_no_raw_sql.py skips
    dot-directories for the same reason and says so.

    Excluding them cannot weaken the control: a Dockerfile under a dot-directory is local
    tooling, and the canonical build only ever builds the one this repository publishes.
    """
    rel = path.relative_to(root) if path.is_absolute() == root.is_absolute() else path
    return any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts[:-1])


def suggestion(img: str) -> str:
    """What to replace a bare Docker Hub reference with.

    public.ecr.aws/docker/library/ mirrors Docker Hub's OFFICIAL images only -- the
    single-segment ones like `node` or `nginx`. A namespaced image such as `grafana/k6`
    is a publisher's own repository and is NOT under docker/library/; naming a concrete
    replacement for it here would send someone to a tag that does not exist, so this asks
    for the publisher's own ECR Public repository instead of guessing one.
    """
    if "/" not in img.split("@")[0].split(":")[0]:
        return f"Use {MIRROR}{img}"
    return (f"Use that publisher's ECR Public repository (public.ecr.aws/<publisher>/...) "
            f"or mirror {img!r} into this account's ECR; {MIRROR} carries official "
            f"single-name images only and does not host it")


def check(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    stages = stage_names(lines)
    problems = []
    for n, line in enumerate(lines, 1):
        m = FROM_RE.match(line)
        if not m:
            continue
        img = image_token(m.group("rest"))
        if not img:
            continue
        # A later stage building on an earlier one pulls nothing.
        if img.lower() in stages or img == "scratch":
            continue
        # An ARG-driven reference is resolved at build time; DS-0003 in
        # check-dockerfile-standard.py is what reviews those defaults.
        if img.startswith("$"):
            continue
        if not HOSTED_RE.match(img):
            problems.append(
                f"{path}:{n}: base image {img!r} names no registry, so it is pulled from "
                f"Docker Hub anonymously and the deploy lane will fail with HTTP 429 once "
                f"the shared CodeBuild quota is spent. {suggestion(img)}"
            )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="directory to scan when no PATH is given")
    ap.add_argument("paths", nargs="*", metavar="PATH", help="Dockerfiles to check")
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        root = Path(args.root)
        if not root.is_dir():
            print(f"::error::--root {root} is not a directory", file=sys.stderr)
            return 2
        targets = sorted(p for p in root.rglob("Dockerfile*") if p.is_file() and not skipped(p, root))

    missing = [p for p in targets if not p.is_file()]
    if missing:
        for p in missing:
            print(f"::error::no such file: {p}", file=sys.stderr)
        return 2

    if not targets:
        print("no Dockerfile found; nothing to check")
        return 0

    problems = [msg for p in targets for msg in check(p)]
    for msg in problems:
        print(f"::error::{msg}")
    if problems:
        print(f"\n{len(problems)} base image(s) pulled from Docker Hub by bare name.")
        return 1

    print(f"every FROM in {len(targets)} Dockerfile(s) names a registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
