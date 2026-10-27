#!/usr/bin/env python3
"""Where a repository lives in the microservices hub, in either of its two layouts.

The fleet scripts here read many repositories at once from one tree - the hub on a
workstation, or a workspace CI assembles. That tree has had two shapes:

  category-first (legacy)   services/<domain>/<repo>, gateways/<repo>, shared/<repo>,
                            contracts/<repo>, tools/<repo>, docs/<repo>, infra/<repo>,
                            ci-workflows
  owner-first               core/<category>/...     the product: the cores, core/ci-workflows
                            <client>/<category>/... one folder per client (inboxxhq/, coderaxis/)

The hub moves repos from the first to the second a few at a time, so both are live at once,
and a moved repo leaves a relative compat link at its old path only until the tools stop
needing it. A script therefore has to find each repo at its REAL path, and cannot lean on the
links: they are going away, and Path.rglob never descends into a symlinked directory anyway,
which is how a scan of `services/` comes to skip every repo that moved.

What a script reports does not change shape. Control files and reports name paths the
legacy way (`services/org/org-core/...`), because that is the one spelling that is stable
across both layouts and across CI, where every workspace is assembled category-first.
`Hub.logical` maps a real path back to that spelling, so an allowlist entry keeps matching
wherever the repo sits.

A client folder is a top-level directory that is not a category and directly holds one.
Dot- and underscore-prefixed directories (`.worktrees`, `_archive`, `_work`) are never
owners, and neither is a symlink or the top-level `ci-workflows` checkout, which holds a
`tools/` of its own. `frontend/` is still category-first and needs nothing here.
"""

from __future__ import annotations

import glob as _glob
import os
from pathlib import Path
from typing import Iterable, Iterator

# The categories a repository is grouped under, in both layouts.
CATEGORIES = ("services", "gateways", "shared", "contracts", "tools", "docs", "infra", "frontend")

# The owner folder that holds the product. Always first among the owners.
CORE = "core"

# Repositories that sit directly in an owner folder rather than in a category: `ci-workflows`
# at the hub root in the legacy layout, `core/ci-workflows` in the owner-first one.
OWNER_LEVEL_REPOS = ("ci-workflows",)


# Filesystem probes that answer False instead of raising. Before 3.13, Path.is_dir() and
# friends propagate PermissionError, and these run against directories nobody chose - every
# top-level entry of a hub, every ancestor of a docs checkout - where one unreadable entry must
# not take a checker down.
def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _holds_category(path: Path, categories: Iterable[str] = CATEGORIES) -> bool:
    return any(_is_dir(path / c) for c in categories)


def is_owner_folder(path: Path) -> bool:
    """`core/`, or a client folder: a real, visible, non-category directory holding a category."""
    name = path.name
    if name in CATEGORIES or name in OWNER_LEVEL_REPOS or name.startswith((".", "_")):
        return False
    if _is_symlink(path) or not _is_dir(path):
        return False
    return _holds_category(path)


def owner_folders(root: Path) -> list[Path]:
    """`root/core` first, then every client folder in name order. Empty for a legacy tree."""
    try:
        names = sorted(entry.name for entry in os.scandir(root))
    except OSError:
        return []
    owners = [root / name for name in names if is_owner_folder(root / name)]
    owners.sort(key=lambda p: (p.name != CORE, p.name))
    return owners


def is_hub(root: Path, categories: Iterable[str] = CATEGORIES) -> bool:
    """Does `root` hold many repositories, in either layout?

    Legacy: a category directory at the root - the test the scripts always made, with the
    caller's own category list. Owner-first: the same under `core/`. A client folder alone is
    deliberately not enough: that test is "some top-level directory holds a `services/` or a
    `tools/`", which an ordinary repository with `internal/services/` passes.
    """
    categories = tuple(categories)
    if _holds_category(root, categories):
        return True
    core = root / CORE
    return _is_dir(core) and not _is_symlink(core) and _holds_category(core, categories)


def detect_layout(root: Path, categories: Iterable[str] = CATEGORIES) -> str:
    """`monorepo` for a hub in either layout, `repo` for a single repository."""
    return "monorepo" if is_hub(root, categories) else "repo"


def default_root(script: str | Path) -> Path:
    """The hub a script in `ci-workflows/scripts/` runs against when it is not told.

    `<hub>/ci-workflows` in the legacy layout, `<hub>/core/ci-workflows` in the owner-first
    one. The script's path is resolved first, so running it through the legacy compat link
    lands in the same place as running it from its real path.
    """
    repo = Path(script).resolve().parents[1]
    parent = repo.parent
    return parent.parent if parent.name == CORE else parent


def _real(path: Path) -> str:
    return os.path.realpath(path)


class Hub:
    """One tree, read in both layouts at once.

    The owner folders are read once, on first use, so a caller that never needs them (every
    path given explicitly) never scans the root.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._owners: list[Path] | None = None
        self._owner_names: frozenset[str] = frozenset()

    @property
    def owners(self) -> list[Path]:
        if self._owners is None:
            self._owners = owner_folders(self.root)
            self._owner_names = frozenset(owner.name for owner in self._owners)
        return self._owners

    def spellings(self, rel: str | Path) -> list[Path]:
        """A legacy relative path at its legacy spelling, then under core/ and each client."""
        return [self.root / rel] + [owner / rel for owner in self.owners]

    def resolve(self, rel: str | Path) -> Path:
        """Where a legacy relative path lives now: the first spelling that exists.

        Falls back to the legacy spelling, so a message about a missing file names the path a
        reader expects and is identical to the one the scripts printed before.
        """
        for path in self.spellings(rel):
            if _exists(path):
                return path
        return self.root / rel

    def glob(self, pattern: str, *, include_hidden: bool = True) -> list[Path]:
        """Every match of a legacy glob, in whichever layout it lives, once.

        Each owner folder is globbed as if it were the root, matches are de-duplicated by real
        path (a compat link and its target are one repository), and the result is ordered by
        logical path - which, for a legacy-only tree, is exactly `sorted(root.glob(pattern))`.
        The spelling kept is the first one found, legacy before core before clients.

        `include_hidden=False` keeps the `glob.glob` convention of not matching a dot-prefixed
        name with a wildcard, for callers that were written against it.
        """
        seen: set[str] = set()
        hits: list[Path] = []
        for base in [self.root, *self.owners]:
            if include_hidden:
                found = sorted(base.glob(pattern))
            else:
                found = sorted(Path(p) for p in _glob.glob(os.path.join(_glob.escape(str(base)),
                                                                        pattern)))
            for path in found:
                real = _real(path)
                if real in seen:
                    continue
                seen.add(real)
                hits.append(path)
        hits.sort(key=lambda p: tuple(self.logical(p).split("/")))
        return hits

    def rglob(self, categories: Iterable[str], pattern: str) -> Iterator[Path]:
        """`rglob(pattern)` over each category in every layout, each file once.

        Category by category, in the order given; within one, the legacy directory first, then
        core/, then each client - so a legacy-only tree is walked in exactly the order the
        scripts walked it before. Symlinked directories are not descended into, as with any
        rglob; a moved repo is reached at its real path instead of through its compat link.
        """
        seen_bases: set[str] = set()
        seen: set[str] = set()
        for category in categories:
            for base in self.spellings(category):
                if not _is_dir(base):
                    continue
                real_base = _real(base)
                if real_base in seen_bases:
                    continue
                seen_bases.add(real_base)
                for path in base.rglob(pattern):
                    real = _real(path)
                    if real in seen:
                        continue
                    seen.add(real)
                    yield path

    def logical(self, path: Path | str) -> str:
        """The legacy-style path of `path` relative to the root.

        `core/services/org/org-core/x.go` -> `services/org/org-core/x.go`,
        `inboxxhq/gateways/inboxxhq-org-bff` -> `gateways/inboxxhq-org-bff`,
        `core/ci-workflows/controls/log-schema.yaml` -> `ci-workflows/controls/log-schema.yaml`.
        A legacy path is returned unchanged. Lexical, so a path found through a compat link
        keeps the spelling it was found under. Raises ValueError for a path outside the root.
        """
        parts = Path(path).relative_to(self.root).parts
        if len(parts) > 1 and self.owners and parts[0] in self._owner_names:
            parts = parts[1:]
        return "/".join(parts)
