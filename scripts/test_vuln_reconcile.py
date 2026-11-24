#!/usr/bin/env python3
"""Tests for scripts/vuln_reconcile.py against a stubbed GitHub API and database (no network)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vuln_reconcile as vr  # noqa: E402

FAILS = 0
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)


def expect(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'}  {msg}")
    FAILS += 0 if cond else 1


class StubApi:
    def __init__(self, files: dict[str, str], pulls: dict[str, list[dict]]):
        self.files, self.pulls = files, pulls

    def get(self, owner, path, raw=False):
        if owner.lower() not in ("coderaxis", "inboxxhq-coderaxis"):
            raise RuntimeError(f"no token for owner {owner!r}; mint one with extra-owners")
        repo = path.split("/")[3]
        if "/pulls?" in path:
            return self.pulls.get(repo, [])
        if "/contents/" in path:
            return self.files.get(f"{repo}/{path.split('/contents/', 1)[1].split('?')[0]}")
        return None


def node(name, org, typ, module=None, service=""):
    n = {"name": name, "type": typ, "service": service, "repository": {"organization": org, "name": name}}
    if module:
        n["module_path"] = module
    return n


def osv(id_, *affected):
    return {"id": id_, "affected": [
        {"package": {"ecosystem": "Go", "name": m}, "ranges": [{"type": "SEMVER", "events": ev}]}
        for m, ev in affected]}


OTEL = osv("GO-2026-6975", ("go.opentelemetry.io/otel", [{"introduced": "1.0.0-RC1"}, {"fixed": "1.47.0"}]))
SDK = osv("GO-2026-6974", ("go.opentelemetry.io/otel/sdk", [{"introduced": "1.33.0"}, {"fixed": "1.47.0"}]),
          ("go.opentelemetry.io/otel/trace", [{"introduced": "1.35.0"}, {"fixed": "1.47.0"}]))
CRYPTO = osv("GO-2026-5932", ("golang.org/x/crypto", [{"introduced": "0"},
                                                       {"fixed": "0.58.1-0.20261009181718-2f1f834330b2"}]))
X509 = osv("GO-2026-6809", ("stdlib", [{"introduced": "0"}, {"fixed": "1.25.12"},
                                       {"introduced": "1.26.0-0"}, {"fixed": "1.26.10"},
                                       {"introduced": "1.27.0-0"}, {"fixed": "1.27.0-rc.2"}]))
NET = osv("GO-2026-6865", ("golang.org/x/net", [{"introduced": "0"}, {"fixed": "0.61.0"}]),
          ("stdlib", [{"introduced": "1.26.0-0"}, {"fixed": "1.26.11"}]))
NOFIX = osv("GO-2026-0001", ("example.com/abandoned", [{"introduced": "0"}]))
DB = {}
for o in (OTEL, SDK, CRYPTO, X509, NET, NOFIX):
    for a in o["affected"]:
        DB.setdefault(a["package"]["name"], []).append(o)

GRAPH = {"nodes": {
    "contracts-go": node("contracts-go", "coderaxis", "shared-library", "github.com/coderaxis/contracts-go"),
    "ai-service": node("ai-service", "InboxxHQ-CoderAxis", "deployable", service="ai"),
    "product-service": node("product-service", "InboxxHQ-CoderAxis", "deployable", service="product"),
    "clean-service": node("clean-service", "InboxxHQ-CoderAxis", "deployable", service="clean"),
    "open-service": node("open-service", "InboxxHQ-CoderAxis", "deployable", service="open"),
    "skentra-post": node("skentra-post", "Skentra-CoderAxis", "deployable", service="post"),
    "docs": node("docs", "coderaxis", "content"),
}}

FILES = {
    "contracts-go/go.mod": "module github.com/coderaxis/contracts-go\n\ngo 1.26.11\n",
    "ai-service/go.mod": (
        "module github.com/inboxxhq-coderaxis/ai-service\n\ngo 1.26.9\n\nrequire (\n"
        "\tgithub.com/coderaxis/contracts-go v0.140.0\n"
        "\tgithub.com/coderaxis/private-thing v1.0.0\n"
        "\tgo.opentelemetry.io/otel v1.45.0\n"
        "\tgo.opentelemetry.io/otel/sdk v1.45.0\n"
        "\tgolang.org/x/net v0.60.0 // indirect\n"
        "\tgolang.org/x/crypto v0.58.0 // indirect\n"
        "\texample.com/replaced v1.0.0\n)\n\n"
        "require go.opentelemetry.io/otel/trace v1.47.0 // indirect\n\n"
        "replace example.com/replaced => ../replaced\n"),
    "product-service/go.mod": ("module github.com/inboxxhq-coderaxis/product-service\n\ngo 1.26.11\n\n"
                               "require example.com/abandoned v0.3.0\n"),
    "clean-service/go.mod": ("module github.com/inboxxhq-coderaxis/clean-service\n\ngo 1.26.11\n"
                             "toolchain go1.26.11\n\nrequire go.opentelemetry.io/otel v1.47.0\n"),
    "open-service/go.mod": ("module github.com/inboxxhq-coderaxis/open-service\n\ngo 1.26.11\n\n"
                            "require go.opentelemetry.io/otel v1.46.0\n"),
}
PULLS = {"open-service": [{"number": 41, "head": {"ref": "chore/bump-otel-v1.47.0"},
                           "updated_at": "2026-11-24T01:00:00Z"}]}


def main() -> int:
    print("\nvuln reconcile: versions")
    expect(vr.vkey("v1.47.0") > vr.vkey("1.47.0-rc.1") > vr.vkey("1.46.9"), "release > its pre-release > older")
    expect(vr.vkey("0.58.1-0.20261009181718-2f1f834330b2") < vr.vkey("0.58.1"), "a pseudo-version sorts before its release")
    expect(vr.vkey("0") < vr.vkey("v0.0.0-20180306012644-bacd9c7ef1dd"), '"0" is below every version')
    expect(vr.fix_for("1.26.9", vr.ranges_of(X509, "stdlib")) == (True, "1.26.10"), "the fix of the interval holding the version")
    expect(vr.fix_for("1.25.3", vr.ranges_of(X509, "stdlib")) == (True, "1.25.12"), "an older minor keeps its own line")
    expect(vr.fix_for("1.26.10", vr.ranges_of(X509, "stdlib")) == (False, None), "a fixed version is not affected")
    expect(vr.fix_for("1.0.0-RC1", vr.ranges_of(OTEL, "go.opentelemetry.io/otel"))[0], "introduced is inclusive")
    expect(vr.fix_for("0.3.0", vr.ranges_of(NOFIX, "example.com/abandoned")) == (True, None), "an open range has no fix")

    print("\nvuln reconcile: go.mod")
    mod = vr.parse_gomod(FILES["ai-service/go.mod"])
    expect(mod["go"] == "1.26.9" and mod["requires"]["golang.org/x/net"] == "v0.60.0", "go line and indirect requires read")
    expect(mod["requires"]["go.opentelemetry.io/otel/trace"] == "v1.47.0", "single-line require read")
    expect(mod["replaced"] == {"example.com/replaced"}, "replace directive read")
    expect(vr.go_version(vr.parse_gomod("module x\ngo 1.26.0\ntoolchain go1.26.9\n")) == "1.26.9", "a newer toolchain line wins")
    expect(vr.go_version(vr.parse_gomod("module x\ngo 1.26\n")) is None, "a go line without a patch is not guessed")

    print("\nvuln reconcile: plan")
    seen: list[list[str]] = []

    def fake_query(_bin, wanted):
        seen.append(wanted)
        return DB

    vr.query = fake_query
    res = vr.plan(GRAPH, StubApi(FILES, PULLS), "govulncheck", set(), timedelta(hours=24), NOW)
    rows = {r["repository"].split("/")[1]: r for r in res["rows"]}
    wanted = seen[0]
    expect(len(seen) == 1, "the database is asked once for the whole fleet")
    expect(not any("coderaxis" in w.lower() for w in wanted), "private platform modules are never sent")
    expect("stdlib@v1.26.9" in wanted and "go.opentelemetry.io/otel@v1.45.0" in wanted, "stdlib and modules looked up")

    ai = rows.get("ai-service")
    expect(ai is not None, "ai-service is planned")
    if ai:
        expect(ai["module"] == "go.opentelemetry.io/otel" and ai["version"] == "v1.47.0", "primary is the first module")
        comps = set(ai["companions"].split())
        expect(comps == {"go.opentelemetry.io/otel/sdk@v1.47.0", "golang.org/x/net@v0.61.0", "go@1.26.11"},
               f"vulnerable modules move together, Go to the highest fix needed: {sorted(comps)}")
        expect("go.opentelemetry.io/otel/trace" not in ai["companions"], "an already fixed module is left alone")
        expect(ai["requires"] == ["github.com/coderaxis/contracts-go"], "platform requires passed for the pin policy")
        expect("GO-2026-6974" in ai["note"] and "not a release" in ai["note"], "PR note names advisories and what is left")
        expect(ai["name"] == "ai-service (security)" and ai["short"] == "otel", "row named apart from the platform bump")
    expect("product-service" not in rows, "a module with no fix opens no PR")
    expect(any("abandoned" in u and "no fixed version" in u for u in res["unfixable"]), "...and is reported")
    expect(any("replaced" in u and "replace directive" in u for u in res["unfixable"]) is False,
           "a replaced module with no advisory is not reported")
    expect("clean-service" not in rows, "a clean repository opens nothing")
    expect("open-service" not in rows and any("#41" in h for h in res["held"]), "a fresh open fix PR is left alone")
    res2 = vr.plan(GRAPH, StubApi(FILES, PULLS), "govulncheck", set(), timedelta(hours=1), NOW)
    expect(any(r["repository"].endswith("/open-service") for r in res2["rows"]), "a stale open fix PR is re-planned")
    expect(any("Skentra" in u for u in res["unscanned"]), "Skentra is excluded, and says so")
    expect(res["scanned"] == 5, f"content and excluded nodes are not scanned (scanned {res['scanned']})")

    only = vr.plan(GRAPH, StubApi(FILES, PULLS), "govulncheck", {"product-service"}, timedelta(hours=24), NOW)
    expect(only["scanned"] == 1, "--only limits the scan")

    print(f"\n{'FAILED' if FAILS else 'passed'}: {FAILS} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
