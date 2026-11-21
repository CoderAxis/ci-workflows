#!/usr/bin/env python3
"""Tests for scripts/cascade_reconcile.py against a stubbed GitHub API (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cascade_reconcile as cr  # noqa: E402

FAILS = 0


def expect(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'}  {msg}")
    FAILS += 0 if cond else 1


class StubApi:
    def __init__(self, files: dict[str, str], tags: dict[str, list[str]], heads: dict[str, str]):
        self.files, self.tags, self.heads = files, tags, heads

    def get(self, owner, path, raw=False):
        parts = path.split("/")
        repo = parts[3]
        if "/git/matching-refs/tags/" in path:
            return [{"ref": f"refs/tags/{t}"} for t in self.tags.get(repo, [])]
        if path.endswith("/commits/HEAD"):
            return {"sha": self.heads[repo]} if repo in self.heads else None
        if "/contents/" in path:
            return self.files.get(f"{repo}/{path.split('/contents/', 1)[1]}")
        return None


def node(name, org, typ, module=None, service=""):
    n = {"name": name, "type": typ, "service": service,
         "repository": {"organization": org, "name": name}}
    if module:
        n["module_path"] = module
    return n


GRAPH = {
    "nodes": {
        "contracts-go": node("contracts-go", "coderaxis", "shared-library", "github.com/coderaxis/contracts-go"),
        "org-core": node("org-core", "coderaxis", "core", "github.com/coderaxis/org-core", "org"),
        "org-core-postgres": node("org-core-postgres", "coderaxis", "schema", "github.com/coderaxis/org-core-postgres", "org"),
        "org-service": node("org-service", "InboxxHQ-CoderAxis", "deployable", service="org"),
        "org-bff": node("org-bff", "InboxxHQ-CoderAxis", "deployable", service="org-bff"),
        "cyclic": node("cyclic", "InboxxHQ-CoderAxis", "deployable"),
        "frontend-core": node("frontend-core", "coderaxis", "shared-library"),
        "web-app": node("web-app", "InboxxHQ-CoderAxis", "deployable"),
        "web-console": node("web-console", "InboxxHQ-CoderAxis", "deployable"),
    },
    "consumers": {
        "contracts-go": ["org-core", "org-service", "org-bff", "cyclic"],
        "org-core": ["org-core-postgres", "org-service"],  # org-bff edge missing on purpose
        "org-core-postgres": ["org-service"],
        "frontend-core": ["web-app", "web-console"],
    },
    "release_order": [["contracts-go", "frontend-core"], ["org-core"], ["org-core-postgres"],
                      ["org-service", "org-bff", "web-app", "web-console"]],
    "unorderable": ["cyclic"],
}

FILES = {
    "org-core/go.mod": "module github.com/coderaxis/org-core\n\nrequire github.com/coderaxis/contracts-go v0.146.0\n",
    "org-core-postgres/go.mod": ("module github.com/coderaxis/org-core-postgres\n\nrequire (\n"
                                 "\tgithub.com/coderaxis/org-core v0.40.0\n"
                                 "\tgithub.com/coderaxis/contracts-go v0.83.0 // indirect\n)\n"),
    "org-service/go.mod": ("module github.com/InboxxHQ-CoderAxis/org-service\n\nrequire (\n"
                           "\tgithub.com/coderaxis/contracts-go v0.83.0\n"
                           "\tgithub.com/coderaxis/org-core v0.44.0\n"
                           "\tgithub.com/coderaxis/org-core-postgres v1.32.0\n)\n"),
    "org-bff/go.mod": ("module github.com/InboxxHQ-CoderAxis/org-bff\n\nrequire (\n"
                       "\tgithub.com/coderaxis/org-core v0.40.0\n"
                       "\tgithub.com/coderaxis/contracts-go v0.0.0-20260101000000-abcdefabcdef\n)\n"),
    "cyclic/go.mod": "module x/cyclic\n\nrequire github.com/coderaxis/contracts-go v0.1.0\n",
    "web-app/.frontend-core-ref": "core_repo=coderaxis/frontend-core\ncore_sha=" + "a" * 40 + "\n",
    "web-console/.frontend-core-ref": "core_repo=coderaxis/frontend-core\ncore_sha=" + "b" * 40 + "\n",
}
TAGS = {"contracts-go": ["v0.83.0", "v0.146.0", "v0.9.0", "not-a-tag"],
        "org-core": ["v0.40.0", "v0.46.0"], "org-core-postgres": ["v1.31.0", "v1.32.0"]}
HEADS = {"frontend-core": "b" * 40}


def main() -> int:
    r = cr.plan(GRAPH, StubApi(FILES, TAGS, HEADS), set())
    by = {b["name"]: b for b in r["bumps"]}

    print("\ncascade reconcile: lag from ground truth")
    expect("org-core" not in by, "a consumer already on every newest release is not bumped")
    expect("org-core-postgres" in by and by["org-core-postgres"]["module"].endswith("/org-core"),
           "an indirect require is ignored; the direct one that lags is bumped")
    svc = by.get("org-service", {})
    expect(svc.get("module", "").endswith("/contracts-go") and svc.get("version") == "v0.146.0",
           "primary is the most upstream lagging module, at its newest semver tag (not lexical)")
    expect(svc.get("companions") == "github.com/coderaxis/org-core@v0.46.0",
           "every other lagging module moves in the same change; one already current does not")
    expect(svc.get("requires") == sorted(["github.com/coderaxis/contracts-go", "github.com/coderaxis/org-core",
                                          "github.com/coderaxis/org-core-postgres"]),
           "requires lists every direct platform pin for the pin policy gate")

    print("\ncascade reconcile: what the release fan-out cannot see")
    expect("org-bff requires org-core but the graph has no edge" in r["drift"],
           "a direct require with no graph edge is reported as drift")
    expect(by.get("org-bff", {}).get("module", "").endswith("/org-core"),
           "and the consumer behind that missing edge is still bumped")
    expect(any("pseudo" in s or "not a release tag" in s for s in r["skipped"]),
           "a pseudo-version pin is reported, not guessed at")
    expect("cyclic" not in by and any(s.startswith("cyclic:") for s in r["skipped"]),
           "a consumer in a dependency cycle is withheld, as the release fan-out withholds it")

    print("\ncascade reconcile: shared source pinned by commit")
    names = {f["name"] for f in r["frontends"]}
    expect(names == {"web-app"}, "only the frontend whose core_sha differs from the tip is re-announced")

    only = cr.plan(GRAPH, StubApi(FILES, TAGS, HEADS), {"org-bff"})
    expect([b["name"] for b in only["bumps"]] == ["org-bff"] and not only["frontends"],
           "--only limits the run to the named consumers")

    print("\ncascade reconcile: go.mod parsing")
    reqs = cr.direct_requires("module github.com/coderaxis/self\n\nrequire (\n"
                              "\tgithub.com/coderaxis/self v1.0.0\n"
                              "\tgithub.com/CoderAxis/Mixed v0.2.0\n"
                              "\tgithub.com/other/lib v1.0.0\n)\n"
                              "require github.com/coderaxis/single v0.3.1\n")
    expect(reqs == {"github.com/coderaxis/mixed": "v0.2.0", "github.com/coderaxis/single": "v0.3.1"},
           "both require forms, case-insensitive owner, own module and non-platform modules excluded")

    print(f"\n{'FAILED' if FAILS else 'passed'}: {FAILS} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
