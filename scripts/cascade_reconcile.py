#!/usr/bin/env python3
"""Plan the cascade reconcile: every consumer whose pins lag the newest platform release.

The release fan-out (module-release.yaml, content-fanout.yaml) is the FAST path: a release bumps
the consumers the artifact graph names, once. It cannot repair anything it did not do:

  - a bump job that failed (a build break fixed later, a runner reaped mid-job, a cancelled run)
    is never retried, so that consumer stays on the old version until the NEXT release;
  - an edge added to a go.mod after the graph was last generated is invisible to the fan-out, so
    that consumer is never bumped at all (2026-11-18: org-core had five deployables pinning it
    and the graph named one; the others sat six minors behind);
  - a bump that was withheld under an older tier rule stays withheld.

This is the SLOW path that makes the fast one safe to trust. It reads GROUND TRUTH, not the
graph's edges: every catalogued repository's go.mod on its default branch, the newest semver tag
of every platform module, and every `.frontend-core-ref`. Anything behind becomes one bump per
consumer, all of its lagging modules moved together (a core and its -postgres adapter only build
together), gated exactly like a release bump by bump-module-pin.

The graph is still used for what it is right about: which repositories exist (nodes), which are
modules, their release order, and which consumers sit in a dependency cycle. Direct requires the
graph has no edge for are reported as drift so the graph gets regenerated.

Inputs: --graph (artifact-graph.generated.json), --tokens (JSON {owner: token}, read scope).
Writes to --out: bumps.json (matrix rows), frontends.json, summary.md. Exit 0 unless the plan
itself could not be made; lag is data, not an error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# Same shape module-release and the graph builder accept: optional `require`, then path + version.
REQUIRE = re.compile(r"^\s*(?:require\s+)?(github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+(v\S+)(.*)$")
PLATFORM = re.compile(r"^github\.com/coderaxis/", re.I)
FRONTEND_CORE = "frontend-core"


class Api:
    def __init__(self, tokens: dict[str, str]):
        self.tokens = {k.lower(): v for k, v in tokens.items()}

    def get(self, owner: str, path: str, raw: bool = False):
        token = self.tokens.get(owner.lower())
        if not token:
            raise RuntimeError(f"no token for owner {owner!r}; mint one with extra-owners")
        req = urllib.request.Request(f"{API}{path}")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                    return body.decode() if raw else json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (403, 429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise
        return None


def semver_key(v: str):
    m = SEMVER.match(v)
    return tuple(map(int, m.groups())) if m else None


def latest_tag(api: Api, owner: str, repo: str) -> str | None:
    refs = api.get(owner, f"/repos/{owner}/{repo}/git/matching-refs/tags/v") or []
    best = None
    for ref in refs:
        tag = ref["ref"].rsplit("/", 1)[-1]
        k = semver_key(tag)
        if k and (best is None or k > best[0]):
            best = (k, tag)
    return best[1] if best else None


def direct_requires(gomod: str) -> dict[str, str]:
    """module path (lower-cased) -> pinned version, direct platform requires only."""
    out: dict[str, str] = {}
    own = re.search(r"^module\s+(\S+)", gomod, re.M)
    own_path = own.group(1).lower() if own else ""
    for line in gomod.splitlines():
        m = REQUIRE.match(line)
        if not m or "// indirect" in m.group(3):
            continue
        path = m.group(1).lower()
        if PLATFORM.match(path) and path != own_path:
            out[path] = m.group(2)
    return out


def plan(graph: dict, api: Api, only: set[str]) -> dict:
    nodes: dict = graph.get("nodes") or {}
    consumers_map: dict = graph.get("consumers") or {}
    unorderable = set(graph.get("unorderable") or [])
    level_of = {n: i for i, lvl in enumerate(graph.get("release_order") or []) for n in lvl}

    def repo_of(node: dict) -> tuple[str, str] | None:
        r = node.get("repository") or {}
        return (r["organization"], r["name"]) if r.get("organization") and r.get("name") else None

    # Every platform module with a published semver release.
    modules: dict[str, dict] = {}
    for name, node in nodes.items():
        mp = (node.get("module_path") or "").lower()
        ref = repo_of(node)
        if not mp or not ref:
            continue
        tag = latest_tag(api, *ref)
        if tag:
            modules[mp] = {"name": name, "short": mp.rsplit("/", 1)[-1], "latest": tag,
                           "path": node["module_path"], "level": level_of.get(name, 0)}

    bumps, drift, skipped, frontends = [], [], [], []
    for name, node in sorted(nodes.items()):
        if only and name not in only:
            continue
        ref = repo_of(node)
        if not ref or node.get("type") == "content":
            continue
        gomod = api.get(ref[0], f"/repos/{ref[0]}/{ref[1]}/contents/go.mod", raw=True)
        if gomod is None:
            continue
        pins = direct_requires(gomod)
        moves, requires = [], []
        for mp, pinned in pins.items():
            mod = modules.get(mp)
            if not mod:
                continue
            requires.append(mod["path"])
            if name not in (consumers_map.get(mod["name"]) or []):
                drift.append(f"{name} requires {mod['name']} but the graph has no edge")
            have, want = semver_key(pinned), semver_key(mod["latest"])
            if have is None:
                skipped.append(f"{name}: {mod['name']} pinned at {pinned} (not a release tag)")
                continue
            if have < want:
                moves.append((mod["level"], mod, pinned))
        if not moves:
            continue
        if name in unorderable:
            skipped.append(f"{name}: sits in a dependency cycle; withheld like the release fan-out")
            continue
        moves.sort(key=lambda m: (m[0], m[1]["path"]))
        _, primary, _ = moves[0]
        bumps.append({
            "repository": f"{ref[0]}/{ref[1]}",
            "name": name,
            "kind": node.get("type", ""),
            "service": node.get("service", ""),
            "module": primary["path"],
            "version": primary["latest"],
            "short": primary["short"],
            "companions": " ".join(f"{m['path']}@{m['latest']}" for _, m, _ in moves[1:]),
            "requires": sorted(requires),
            "moves": [f"{m['short']} {p} -> {m['latest']}" for _, m, p in moves],
        })

    # Shared source consumed by checkout: compare each consumer's pinned commit with the tip.
    fc = nodes.get(FRONTEND_CORE)
    if fc and repo_of(fc) and (not only or only & set(consumers_map.get(FRONTEND_CORE) or [])):
        owner, repo = repo_of(fc)
        head = (api.get(owner, f"/repos/{owner}/{repo}/commits/HEAD") or {}).get("sha")
        for name in consumers_map.get(FRONTEND_CORE) or []:
            if only and name not in only:
                continue
            cref = repo_of(nodes.get(name) or {})
            if not cref or not head:
                continue
            pin = api.get(cref[0], f"/repos/{cref[0]}/{cref[1]}/contents/.frontend-core-ref", raw=True) or ""
            m = re.search(r"^core_sha=([0-9a-f]{40})", pin, re.M)
            if not m:
                skipped.append(f"{name}: .frontend-core-ref has no core_sha= line")
            elif m.group(1) != head:
                frontends.append({"repository": f"{cref[0]}/{cref[1]}", "name": name,
                                  "pinned": m.group(1), "sha": head})

    bumps.sort(key=lambda b: (min(level_of.get(b["name"], 0), 99), b["repository"]))
    return {"bumps": bumps, "frontends": frontends, "drift": sorted(set(drift)),
            "skipped": skipped, "modules": len(modules)}


def summary(result: dict) -> str:
    lines = ["## Cascade reconcile", "",
             f"Platform modules with a release: {result['modules']}. "
             f"Consumers behind: {len(result['bumps'])}. Frontends behind: {len(result['frontends'])}.", ""]
    if result["bumps"]:
        lines += ["| Consumer | Moves |", "| --- | --- |"]
        lines += [f"| {b['repository']} | {'<br>'.join(b['moves'])} |" for b in result["bumps"]]
        lines.append("")
    if result["frontends"]:
        lines += ["| Frontend | Pinned | Tip |", "| --- | --- | --- |"]
        lines += [f"| {f['repository']} | {f['pinned'][:8]} | {f['sha'][:8]} |" for f in result["frontends"]]
        lines.append("")
    if result["drift"]:
        lines += ["### Graph drift (the release fan-out cannot see these edges)", "",
                  "Regenerate with `scripts/build_artifact_graph.py` in core-docs.", ""]
        lines += [f"- {d}" for d in result["drift"]] + [""]
    if result["skipped"]:
        lines += ["### Not bumped", ""] + [f"- {s}" for s in result["skipped"]] + [""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--tokens", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", default="", help="space-separated graph node names to limit the run to")
    args = ap.parse_args()

    graph = json.loads(args.graph.read_text())
    api = Api(json.loads(args.tokens.read_text()))
    result = plan(graph, api, set(args.only.split()))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bumps.json").write_text(json.dumps(result["bumps"], separators=(",", ":")))
    (args.out / "frontends.json").write_text(json.dumps(result["frontends"], separators=(",", ":")))
    (args.out / "summary.md").write_text(summary(result))
    # Annotations, not only the summary: the reconcile repairs the lag, but until the graph is
    # regenerated every release keeps missing these consumers and only this run catches them.
    for d in result["drift"]:
        print(f"::warning::artifact graph drift: {d}; regenerate it with core-docs "
              "scripts/build_artifact_graph.py")
    print(f"{len(result['bumps'])} consumers behind, {len(result['frontends'])} frontends behind, "
          f"{len(result['drift'])} graph drift, {len(result['skipped'])} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
