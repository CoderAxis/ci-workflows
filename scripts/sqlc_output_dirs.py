#!/usr/bin/env python3
"""Print the directories sqlc is configured to write, one per line. Run from a repo root.

The drift check needs to diff generated output against the tree, and the only
reliable statement of where that output lives is the config sqlc itself reads.
Hard-coding the directory names is what made the previous check vacuous: it
compared `sqlc` and `rootsqlc`, no repository in the fleet uses either, and git
diff tolerates a pathspec matching nothing — so the gate passed without reading
a single generated file.

Both config shapes are handled. sqlc v2 nests the output under gen.go.out; v1
puts it directly on the package.
"""
from __future__ import annotations

import os
import sys

import yaml


def output_dirs(cfg: dict) -> list[str]:
    seen: list[str] = []
    for pkg in cfg.get("sql") or []:
        if not isinstance(pkg, dict):
            continue
        gen_go = ((pkg.get("gen") or {}).get("go")) or {}
        out = gen_go.get("out") or pkg.get("out")
        if out and out not in seen:
            seen.append(out)
    return seen


def main() -> int:
    path = "sqlc.yaml" if os.path.exists("sqlc.yaml") else "sqlc.yml"
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for out in output_dirs(cfg):
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
