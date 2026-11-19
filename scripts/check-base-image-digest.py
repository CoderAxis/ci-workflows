#!/usr/bin/env python3
"""Report base images that are pinned by tag only, not by @sha256 digest.

WHY. A tag is a pointer the publisher can move. `golang:1.26-alpine3.24` today and the same
tag next week can be different bytes, so the build is not reproducible and a compromised or
re-pushed tag reaches every image at its next build without a single line changing here.
A digest names the bytes. core-docs security/supply-chain.md lists digest pinning as a
Target that nothing enforces; the fleet pins by tag (DS-0003, the version matrix).

WHY A RATCHET, AND WHY WARN FIRST. Almost every Dockerfile in the fleet is tag-pinned today,
so a hard gate would fail every build at once. The baseline lists the base IMAGES (registry
and repository, no tag) that are still allowed unpinned. An image not on it -- a new base
image entering the fleet -- is "new debt". Modes:

  warn     (default, what the reusable workflows run)  every unpinned FROM is a warning,
           new debt is marked as such; exit 0.
  ratchet  baselined images warn, new debt fails (exit 1). An image leaves the baseline
           once the fleet pins it, and from then on cannot come back unpinned.
  enforce  every unpinned FROM fails (exit 1).

The baseline names images, not repositories, on purpose: it stays a short neutral list that
needs no edit when a repository is created or renamed, and it closes the door that matters
most first -- a new, unreviewed base image arriving unpinned.

    python3 scripts/check-base-image-digest.py [--mode warn|ratchet|enforce]
        [--baseline FILE] [--root DIR] [PATH ...]

Exit 0 when nothing fails in the chosen mode, 1 when something does, 2 on a bad invocation.
Stage references (`FROM builder`), `scratch`, and Dockerfiles under dot-directories (a
vendored checkout of this repository) are skipped, exactly as check-base-image-registry.py
skips them; this script reuses that script's parsing so the two cannot drift.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("base_image_registry", HERE / "check-base-image-registry.py")
registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(registry)

DEFAULT_BASELINE = HERE.parent / "controls" / "base-image-digest-baseline.json"
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
ARG_RE = re.compile(r"^\s*ARG\s+(?P<decls>.+?)\s*$", re.IGNORECASE)
ARG_REF_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
MODES = ("warn", "ratchet", "enforce")


def arg_defaults(lines: list[str]) -> dict[str, str]:
    """ARG name -> declared default anywhere in the file; a bare `ARG X` maps to "".

    A bare re-declaration inside a stage (`ARG GO_LANG_VERSION` after the global
    `ARG GO_LANG_VERSION=1.25`) inherits the global default in BuildKit, so it must not
    erase the default recorded earlier.
    """
    out: dict[str, str] = {}
    for line in lines:
        m = ARG_RE.match(line)
        if not m:
            continue
        for decl in m.group("decls").split():
            name, _, default = decl.partition("=")
            name, default = name.strip(), default.strip().strip('"').strip("'")
            if default or name not in out:
                out[name] = default
    return out


def resolve(ref: str, args: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute ARG defaults. Returns the reference and the ARGs that had no default.

    Only the default is visible to a static check, and it is what a plain `docker build`
    uses. An ARG with no default leaves its `${X}` in place, so a digest supplied only
    through --build-arg reads as unpinned: the file alone does not prove the pin.
    """
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        val = args.get(name, "")
        if not val:
            missing.append(name)
            return m.group(0)
        return val

    return ARG_REF_RE.sub(sub, ref), missing


def image_name(ref: str) -> str:
    """Registry and repository of a reference, without tag or digest."""
    name = ref.split("@", 1)[0]
    head, sep, last = name.rpartition("/")
    last = last.split(":", 1)[0]
    return f"{head}{sep}{last}"


def check(path: Path) -> list[dict]:
    """One record per external FROM: line, raw and resolved reference, image, pinned."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    stages = registry.stage_names(lines)
    args = arg_defaults(lines)
    out = []
    for n, line in enumerate(lines, 1):
        m = registry.FROM_RE.match(line)
        if not m:
            continue
        raw = registry.image_token(m.group("rest"))
        if not raw or raw.lower() in stages or raw == "scratch":
            continue
        resolved, missing = resolve(raw, args)
        out.append({
            "path": str(path), "line": n, "raw": raw, "resolved": resolved,
            "image": image_name(resolved), "missing_args": missing,
            "pinned": bool(DIGEST_RE.search(resolved)) and not missing,
        })
    return out


def load_baseline(path: Path) -> set[str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    images = doc.get("unpinned_images")
    if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
        raise ValueError(f"{path}: 'unpinned_images' must be a list of strings")
    return set(images)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="warn")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="JSON with 'unpinned_images'")
    ap.add_argument("--root", default=".", help="directory to scan when no PATH is given")
    ap.add_argument("paths", nargs="*", metavar="PATH", help="Dockerfiles to check")
    args = ap.parse_args(argv[1:])

    try:
        baseline = load_baseline(Path(args.baseline))
    except (OSError, ValueError) as exc:
        print(f"::error::base-image digest: cannot read baseline: {exc}")
        return 2

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        root = Path(args.root)
        if not root.is_dir():
            print(f"::error::--root {root} is not a directory")
            return 2
        targets = sorted(p for p in root.rglob("Dockerfile*") if p.is_file() and not registry.skipped(p, root))
    missing = [p for p in targets if not p.is_file()]
    if missing:
        for p in missing:
            print(f"::error::no such file: {p}")
        return 2
    if not targets:
        print("no Dockerfile found; nothing to check")
        return 0

    records = [r for p in targets for r in check(p)]
    unpinned = [r for r in records if not r["pinned"]]
    failed = 0
    for r in unpinned:
        new = r["image"] not in baseline
        why = (f"ARG {', '.join(r['missing_args'])} has no default, so the file does not prove a digest"
               if r["missing_args"] else "pinned by tag only")
        msg = (f"{r['path']}:{r['line']}: base image {r['resolved']!r} is {why}. "
               f"Pin it as <image>:<tag>@sha256:<digest> (the tag stays for readers; the digest is what is pulled)."
               + (" NEW: this image is not in the unpinned-image baseline." if new else ""))
        fail = args.mode == "enforce" or (args.mode == "ratchet" and new)
        print(f"::{'error' if fail else 'warning'}::{msg}")
        failed += fail

    pinned = len(records) - len(unpinned)
    new_debt = sum(1 for r in unpinned if r["image"] not in baseline)
    print(f"base-image digest ({args.mode}): {pinned}/{len(records)} FROM line(s) pinned by digest "
          f"in {len(targets)} Dockerfile(s); {len(unpinned) - new_debt} baselined, {new_debt} new")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
