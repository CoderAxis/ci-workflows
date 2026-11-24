#!/usr/bin/env python3
"""Plan the vulnerability half of the cascade reconcile: every catalogued Go repository whose go.mod
pins a third-party module, or a Go version, that a known vulnerability affects and a release fixes.

WHY. Our own modules reach their consumers through the release cascade (module-release.yaml is the
fast path, cascade_reconcile.py the slow one). Nothing did the same for the rest of a go.mod. When an
advisory is published against a module the fleet pins, service-ci's `vulnerabilities` job turns red,
the `artifact` job that needs it is skipped, and the service stops deploying with nothing said, until
somebody bumps every repository by hand. That was done by hand on 2026-10-09 (go1.26.6 stdlib and
x/net), 10-29 (crypto/x509), 11-07 (x/net, klauspost/compress) and 11-23 (go.opentelemetry.io/otel
v1.45.0, GO-2026-6974 and GO-2026-6975), each time after a deploy had already gone missing.

WHAT IT READS. Every catalogued repository's go.mod on its default branch - the ground truth
cascade_reconcile.py reads - and the Go vulnerability database, through `govulncheck -mode=query`
asked ONCE for the union of public module versions the fleet pins. No repository is cloned, no
dependency downloaded, nothing built: the whole fleet is planned in seconds, and a run with nothing
to fix costs one job. Private platform modules are never sent: the database has none of them, and
their names stay inside the estate.

WHAT COUNTS. Every module go.mod requires, direct and indirect. Since Go 1.17 go.mod lists every
module that provides a package the repository builds, which is the set CI's Trivy scan reads and a
superset of what govulncheck's symbol scan reaches. An advisory against a function nobody calls
today blocks the deploy on the day someone calls it, which is the worst moment to learn of it.
The Go version counts too: CI installs the version go.mod names and scans the standard library at
it, so a Go security release is fixed by moving the `go` line, as was done by hand on 10-09.

WHAT IT PLANS. One bump per repository, moving every vulnerable module to the lowest RELEASED
version that fixes all of its advisories, run by bump-module-pin like any release bump (build,
unit and contract suites, pin policy, auto-merge once green, a major move withheld for a person).
Reported, not bumped: an advisory with no fix, a fix that exists only as a pseudo-version or a
pre-release, and a module a replace directive redirects (moving its require would change nothing).
A repository whose fix branch already exists and was pushed within --fresh-hours is not re-planned,
so an open fix is not rebuilt every six hours while its CI runs.

These are PRs of their own, not companions of the same repository's platform bump: a platform
bump that keeps failing its gates must never hold a security fix back.

Inputs: --graph (artifact-graph.generated.json), --tokens (JSON {owner: token}, read scope),
--govulncheck (the binary). Writes to --out: vulns.json (matrix rows in the shape bump-module-pin
takes), vulns.md (summary). Exit 0 unless the plan itself could not be made.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cascade_reconcile as cr  # noqa: E402  (Api, direct_requires: one reading of go.mod)

PRIVATE = re.compile(r"^github\.com/(coderaxis|inboxxhq-coderaxis)/", re.I)
# Skentra's CI runs on its production node and its repositories take one change at a time, by
# hand. Excluded here even if a catalog ever names one, so no schedule can fan out into it.
EXCLUDED_OWNER = re.compile(r"skentra", re.I)
GO_LINE = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
MAJOR_SUFFIX = re.compile(r"^v\d+$")
STDLIB = "stdlib"
QUERY_CHUNK = 200


# ── go.mod ──────────────────────────────────────────────────────────────────────────────────────

def parse_gomod(text: str) -> dict:
    """module, go, toolchain, every require (path -> version) and every replaced path."""
    out: dict = {"module": "", "go": "", "toolchain": "", "requires": {}, "replaced": set()}
    block = None

    def take(kind: str, fields: list[str]) -> None:
        if kind == "require" and len(fields) >= 2:
            out["requires"][fields[0].strip('"')] = fields[1]
        elif kind == "replace" and "=>" in fields:
            out["replaced"].add(fields[0].strip('"'))

    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if block:
            if line == ")":
                block = None
            else:
                take(block, fields)
            continue
        kw = fields[0]
        if kw in ("require", "replace", "exclude", "retract") and fields[1:] == ["("]:
            block = kw
        elif kw == "module" and len(fields) > 1:
            out["module"] = fields[1].strip('"')
        elif kw == "go" and len(fields) > 1:
            out["go"] = fields[1]
        elif kw == "toolchain" and len(fields) > 1:
            out["toolchain"] = fields[1]
        elif kw in ("require", "replace"):
            take(kw, fields[1:])
    return out


def go_version(mod: dict) -> str | None:
    """The Go version CI installs for this go.mod: the toolchain line when it is newer, else go."""
    candidates = [v for v in (mod["go"], mod["toolchain"].removeprefix("go")) if GO_LINE.match(v)]
    return max(candidates, key=vkey) if candidates else None


# ── versions ────────────────────────────────────────────────────────────────────────────────────

LOWEST = (-1,)


def vkey(v: str):
    """Semver precedence for Go module and toolchain versions, `v` optional. "0" is the origin."""
    v = v.removeprefix("v")
    if v == "0":
        return LOWEST
    core, _, pre = v.partition("+")[0].partition("-")
    nums = core.split(".")
    nums += ["0"] * (3 - len(nums))
    try:
        major, minor, patch = (int(n) for n in nums[:3])
    except ValueError:
        return LOWEST
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    # A release sorts after every pre-release of its version; pseudo-versions are pre-releases.
    return (major, minor, patch, 0 if pre else 1, ids)


def fix_for(version: str, ranges: list[dict]) -> tuple[bool, str | None]:
    """(affected, the fixed version of the interval holding `version`, None if it has none)."""
    v = vkey(version)
    for r in ranges:
        if r.get("type") not in (None, "SEMVER", "ECOSYSTEM"):
            continue
        start = None
        intervals = []
        for e in sorted(r.get("events") or [], key=lambda e: vkey(next(iter(e.values())))):
            if "introduced" in e:
                start = e["introduced"] if start is None else start
            elif start is not None and ("fixed" in e or "last_affected" in e):
                intervals.append((start, e.get("fixed"), e.get("last_affected")))
                start = None
        if start is not None:
            intervals.append((start, None, None))
        for lo, fixed, last in intervals:
            if v < vkey(lo):
                continue
            if fixed is not None and v < vkey(fixed):
                return True, fixed
            if last is not None and v <= vkey(last):
                return True, None
            if fixed is None and last is None:
                return True, None
    return False, None


# ── the database ────────────────────────────────────────────────────────────────────────────────

def parse_stream(text: str) -> list[dict]:
    dec, i, out = json.JSONDecoder(), 0, []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


def query(govulncheck: str, wanted: list[str]) -> dict[str, list[dict]]:
    """module path -> the advisories (OSV) the database holds for any queried version of it."""
    by_module: dict[str, dict[str, dict]] = {}
    for i in range(0, len(wanted), QUERY_CHUNK):
        chunk = wanted[i:i + QUERY_CHUNK]
        p = subprocess.run([govulncheck, "-mode=query", "-format=json", *chunk],
                           capture_output=True, text=True, timeout=900)
        if p.returncode not in (0, 3):
            raise RuntimeError(f"govulncheck -mode=query failed ({p.returncode}): {p.stderr.strip()[:2000]}")
        for obj in parse_stream(p.stdout):
            osv = obj.get("osv")
            if not osv:
                continue
            for a in osv.get("affected") or []:
                pkg = a.get("package") or {}
                if pkg.get("ecosystem", "Go") == "Go" and pkg.get("name"):
                    by_module.setdefault(pkg["name"], {})[osv["id"]] = osv
    return {m: list(v.values()) for m, v in by_module.items()}


def ranges_of(osv: dict, module: str) -> list[dict]:
    return [r for a in osv.get("affected") or [] if (a.get("package") or {}).get("name") == module
            for r in a.get("ranges") or []]


# ── the plan ────────────────────────────────────────────────────────────────────────────────────

def short_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) > 1 and MAJOR_SUFFIX.match(parts[-1]):
        parts = parts[:-1]
    return parts[-1]


def scan_repo(mod: dict, db: dict[str, list[dict]]) -> dict:
    """Per module: current, target, advisories fixed; plus what cannot be fixed by a bump."""
    targets: dict[str, dict] = {}
    unfixable: list[str] = []
    checks = [(m, v) for m, v in mod["requires"].items() if not PRIVATE.match(m)]
    gv = go_version(mod)
    if gv:
        checks.append((STDLIB, gv))
    for path, current in sorted(checks):
        for osv in sorted(db.get(path) or [], key=lambda o: o["id"]):
            hit, fixed = fix_for(current, ranges_of(osv, path))
            if not hit:
                continue
            if path in mod["replaced"]:
                unfixable.append(f"{path} {current}: {osv['id']} (redirected by a replace directive)")
            elif fixed is None:
                unfixable.append(f"{path} {current}: {osv['id']} (no fixed version published)")
            elif not RELEASE.match(fixed):
                unfixable.append(f"{path} {current}: {osv['id']} (fixed only in {fixed}, not a release)")
            else:
                t = targets.setdefault(path, {"current": current, "target": fixed, "ids": []})
                t["ids"].append(osv["id"])
                if vkey(fixed) > vkey(t["target"]):
                    t["target"] = fixed
    return {"targets": targets, "unfixable": unfixable}


def row_for(name: str, node: dict, ref: tuple[str, str], scan: dict, requires: list[str]) -> dict:
    moves = []
    for path, t in sorted(scan["targets"].items()):
        if path == STDLIB:
            # `go get go@X` raises the go line, which is what CI installs and scans; a stale
            # toolchain line below it is dropped by the same command.
            moves.append({"module": "go", "short": "go", "version": t["target"], **t})
        else:
            moves.append({"module": path, "short": short_of(path), "version": "v" + t["target"], **t})
    primary, rest = moves[0], moves[1:]
    ids = sorted({i for m in moves for i in m["ids"]})
    note = "\n".join(
        ["### Why: known vulnerabilities", "",
         "Planned by the scheduled cascade reconcile (`scripts/vuln_reconcile.py`) from the Go "
         "vulnerability database. Each module moves to the lowest release that fixes every "
         "advisory listed, and nothing else in go.mod is changed on purpose.", "",
         "| Module | From | To | Advisories |", "| --- | --- | --- | --- |"]
        + [f"| `{m['module']}` | `{m['current']}` | `{m['version']}` | "
           + ", ".join(f"[{i}](https://pkg.go.dev/vuln/{i})" for i in m["ids"]) + " |" for m in moves]
        + ([""] + ["Not fixed by this change (no released fix yet):", ""]
           + [f"- {u}" for u in scan["unfixable"]] if scan["unfixable"] else []))
    return {
        "repository": f"{ref[0]}/{ref[1]}",
        "name": f"{name} (security)",
        "kind": node.get("type", ""),
        "service": node.get("service", ""),
        "module": primary["module"],
        "version": primary["version"],
        "short": primary["short"],
        "companions": " ".join(f"{m['module']}@{m['version']}" for m in rest),
        "requires": requires,
        "moves": [f"{m['module']} {m['current']} -> {m['version']}" for m in moves],
        "advisories": ids,
        "branch": f"chore/bump-{primary['short']}-{primary['version']}",
        "note": note,
    }


def open_bump(api, ref: tuple[str, str], branch: str, fresh: timedelta, now: datetime) -> str | None:
    """The fix branch already carrying this exact bump, if it was pushed recently enough to leave.

    Read from the branch, not the pull request list: the planner's token is the read scope, which
    has contents but not pull requests (the first dispatch, 2026-11-24, failed 403 on /pulls).
    bump-module-pin names the branch after the bump and deletes it when the PR merges or has
    nothing to add, so a live branch is an open fix, and its tip's commit date is when it was
    last rebuilt. A branch that cannot be read holds nothing back: the bump job's own guard and
    supersede steps still refuse a duplicate.
    """
    try:
        tip = api.get(ref[0], f"/repos/{ref[0]}/{ref[1]}/commits/{urllib.parse.quote(branch, safe='')}")
    except urllib.error.HTTPError:
        return None
    if not tip:
        return None
    pushed = (((tip.get("commit") or {}).get("committer") or {}).get("date") or "")
    try:
        when = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"branch {branch}" if now - when < fresh else None


def plan(graph: dict, api, govulncheck: str, only: set[str], fresh: timedelta,
         now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    nodes: dict = graph.get("nodes") or {}
    platform = {(n.get("module_path") or "").lower(): n["module_path"]
                for n in nodes.values() if n.get("module_path")}

    repos, unscanned = [], []
    for name, node in sorted(nodes.items()):
        if only and name not in only:
            continue
        r = node.get("repository") or {}
        if not r.get("organization") or not r.get("name") or node.get("type") == "content":
            continue
        ref = (r["organization"], r["name"])
        if EXCLUDED_OWNER.search(ref[0]):
            unscanned.append(f"{name}: owner {ref[0]} is excluded (changed by hand, one at a time)")
            continue
        try:
            gomod = api.get(ref[0], f"/repos/{ref[0]}/{ref[1]}/contents/go.mod", raw=True)
        except RuntimeError as e:
            unscanned.append(f"{name}: {e}")
            continue
        if gomod is None:
            continue
        repos.append((name, node, ref, gomod, parse_gomod(gomod)))

    wanted = sorted({f"{m}@{v}" for *_, mod in repos for m, v in mod["requires"].items()
                     if not PRIVATE.match(m)}
                    | {f"{STDLIB}@v{gv}" for *_, mod in repos if (gv := go_version(mod))})
    db = query(govulncheck, wanted) if wanted else {}

    rows, held, unfixable = [], [], []
    for name, node, ref, gomod, mod in repos:
        scan = scan_repo(mod, db)
        unfixable += [f"{ref[0]}/{ref[1]}: {u}" for u in scan["unfixable"]]
        if not scan["targets"]:
            continue
        requires = sorted(platform[p] for p in cr.direct_requires(gomod) if p in platform)
        row = row_for(name, node, ref, scan, requires)
        pr = open_bump(api, ref, row["branch"], fresh, now)
        if pr:
            held.append(f"{row['repository']}: {pr} already carries this fix and is still fresh")
            continue
        rows.append(row)
    return {"rows": rows, "held": held, "unfixable": unfixable, "unscanned": unscanned,
            "scanned": len(repos), "queried": len(wanted)}


def summary(result: dict) -> str:
    lines = ["## Vulnerability reconcile", "",
             f"Go repositories scanned: {result['scanned']}. Module versions looked up: "
             f"{result['queried']}. Repositories to fix: {len(result['rows'])}.", ""]
    if result["rows"]:
        lines += ["| Repository | Moves | Advisories |", "| --- | --- | --- |"]
        lines += [f"| {r['repository']} | {'<br>'.join(r['moves'])} | {', '.join(r['advisories'])} |"
                  for r in result["rows"]]
        lines.append("")
    for title, key in (("Fix already open", "held"),
                       ("Known vulnerable, no released fix to move to", "unfixable"),
                       ("Not scanned", "unscanned")):
        if result[key]:
            lines += [f"### {title}", ""] + [f"- {s}" for s in result[key]] + [""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--tokens", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--govulncheck", default="govulncheck")
    ap.add_argument("--only", default="", help="space-separated graph node names to limit the run to")
    ap.add_argument("--fresh-hours", type=float, default=24.0,
                    help="leave an open fix PR alone while it was updated this recently")
    args = ap.parse_args()

    graph = json.loads(args.graph.read_text())
    api = cr.Api(json.loads(args.tokens.read_text()))
    result = plan(graph, api, args.govulncheck, set(args.only.split()),
                  timedelta(hours=args.fresh_hours))
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [{k: v for k, v in r.items() if k not in ("advisories", "branch")} for r in result["rows"]]
    (args.out / "vulns.json").write_text(json.dumps(rows, separators=(",", ":")))
    (args.out / "vulns.md").write_text(summary(result))
    for r in result["rows"]:
        print(f"::warning::{r['repository']} pins vulnerable modules ({', '.join(r['advisories'])}); "
              "a fix PR is planned")
    for u in result["unfixable"]:
        print(f"::warning::no released fix to move to: {u}")
    print(f"{result['scanned']} Go repositories, {len(result['rows'])} to fix, "
          f"{len(result['held'])} already open, {len(result['unfixable'])} unfixable, "
          f"{len(result['unscanned'])} not scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
