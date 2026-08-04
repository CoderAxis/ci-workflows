#!/usr/bin/env python3
"""Precision/recall and wiring tests for GW-0001..GW-0008."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

HERE = pathlib.Path(__file__).resolve().parent
CHECKER = HERE / "check-gateway-baseline.py"
CONTROLS = HERE.parent / "controls" / "gateway-baseline.yaml"
FIXTURES = HERE.parent / "fixtures"
FAILURES: list[str] = []


def load_checker():
    spec = importlib.util.spec_from_file_location("check_gateway_baseline", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = load_checker()


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def make_repo(tmp: str, files: dict[str, str]) -> "m.Repo":
    root = pathlib.Path(tmp)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return m.Repo(root)


GATEWAY_CONTRACT = """service:
  name: test-gateway
  type: gateway
  internet_facing: true
  ports: {http: 8080}
"""
GRPC_CONTRACT = """service:
  name: test-service
  ports: {grpc: 9090}
"""


def test_gw0001_shared_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "main.go": '''package main
import "github.com/coderaxis/platform-shared-go/platform/gatewaybaseline"
func main() { _ = gatewaybaseline.Load() }
''',
        })
        expect(m.shared_baseline_loaded(repo).count == 0,
               "GW-0001 must credit the shared package import plus loader call")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/base/security.yaml": "security:\n  tls: true\n",
        })
        expect(m.shared_baseline_loaded(repo).count == 1,
               "GW-0001 must reject a repository-local baseline-owned file")


def test_gw0002_local_overrides():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/local.yaml": "headers:\n  response: {}\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 1,
               "GW-0002 must reject an unexcepted top-level owned key")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/local.yaml": "headers:\n  response: {}\n",
            "gateway-baseline-exceptions.yaml":
                "exceptions:\n  - key: headers\n    reason: legacy partner response policy\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 0,
               "GW-0002 must credit a declared exception carrying a reason")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/surface.yaml": "surface:\n  auth:\n    required: true\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 0,
               "GW-0002 must not mistake a nested surface key for a top-level redefinition")

    # The two out-of-scope layers, guarded because scanning them is what produced 174 findings on
    # the edge gateway where 5 were real. Both cases are TOP-LEVEL owned keys, so only the scope
    # rule can exclude them - a nested-key check would not.
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/surfaces/public/webhook.yaml": "auth:\n  required: false\n",
            "config/surfaces/me/profile.yaml": "ratelimit:\n  enabled: true\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 0,
               "GW-0002 must not flag per-surface routing policy, which the baseline does not own")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/environments/prod.yaml": "security:\n  hsts: {max_age: 63072000}\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 0,
               "GW-0002 must not flag an environment overlay, the loader's sanctioned override layer")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "config/base/headers.yaml": "headers:\n  response: {}\n",
            "config/environments/prod.yaml": "security:\n  hsts: {max_age: 63072000}\n",
            "config/surfaces/public/webhook.yaml": "auth:\n  required: false\n",
        })
        expect(m.no_local_policy_redefinition(repo).count == 1,
               "GW-0002 must still catch the base layer while both excluded layers are present")


def test_gw0003_module_floor():
    previous = m.DEFAULT_FLOORS
    with tempfile.TemporaryDirectory() as tmp:
        floors = pathlib.Path(tmp) / "floors.yaml"
        floors.write_text("""floors:
  github.com/coderaxis/platform-shared-go:
    min: v2.0.0
""", encoding="utf-8")
        m.DEFAULT_FLOORS = floors
        try:
            with tempfile.TemporaryDirectory() as repo_tmp:
                repo = make_repo(repo_tmp, {
                    "service.contract.yaml": GATEWAY_CONTRACT,
                    "go.mod": "module example.com/x\n\ngo 1.25\n\n"
                              "require github.com/coderaxis/platform-shared-go v1.9.0\n",
                })
                expect(m.module_floor(repo).count == 1,
                       "GW-0003 must reject a released pin below the floor")
            with tempfile.TemporaryDirectory() as repo_tmp:
                repo = make_repo(repo_tmp, {
                    "service.contract.yaml": GATEWAY_CONTRACT,
                    "go.mod": "module example.com/x\n\ngo 1.25\n\n"
                              "require github.com/coderaxis/platform-shared-go v2.1.0\n",
                })
                expect(m.module_floor(repo).count == 0,
                       "GW-0003 must credit a stable direct pin above the floor")
        finally:
            m.DEFAULT_FLOORS = previous


def test_gw0004_protocol_proxy():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "surface.yaml":
                "routing:\n  rules:\n    - service: inboxxhq-realtime-gateway\n"
                "      websocket: true\n",
        })
        expect(m.protocol_gateway_proxy(repo).count == 1,
               "GW-0004 must reject websocket proxying to another gateway")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "surface.yaml":
                "routing:\n  rules:\n    - service: inboxxhq-chat-service\n"
                "      websocket: true\n",
        })
        expect(m.protocol_gateway_proxy(repo).count == 0,
               "GW-0004 must allow a websocket rule whose upstream is not a gateway")


def test_gw0005_long_lived_bounds():
    bad = """package proxy
func tunnel(a, b net.Conn) {
  go io.Copy(a, b)
  go io.Copy(b, a)
}
"""
    good = """package proxy
const maxConcurrentConnections = 100
func tunnel(a, b net.Conn) {
  _ = a.SetReadDeadline(time.Now().Add(time.Minute))
  _ = b.SetReadDeadline(time.Now().Add(time.Minute))
  go io.Copy(a, b)
  go io.Copy(b, a)
}
"""
    with tempfile.TemporaryDirectory() as tmp:
        finding = m.bounded_long_lived_connections(make_repo(
            tmp, {"service.contract.yaml": GATEWAY_CONTRACT, "proxy.go": bad}))
        expect(finding.count == 2,
               "GW-0005 must report both missing deadline and missing raw connection cap")
    with tempfile.TemporaryDirectory() as tmp:
        finding = m.bounded_long_lived_connections(make_repo(
            tmp, {"service.contract.yaml": GATEWAY_CONTRACT, "proxy.go": good}))
        expect(finding.count == 0,
               "GW-0005 must credit explicit read deadlines and an explicit concurrent cap")


def test_gw0006_grpc_decisions():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "auth.yaml": "auth:\n  validate_endpoint: /api/v1/auth/validate\n"
                         "authz:\n  check_endpoint: https://authz/check\n",
        })
        expect(m.grpc_decision_hops(repo).count == 2,
               "GW-0006 must reject HTTP paths and URLs for both decision hops")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GATEWAY_CONTRACT,
            "auth.yaml": "auth:\n  grpc_service: auth:50060\n"
                         "authz:\n  grpc_service: authz:50061\n",
        })
        expect(m.grpc_decision_hops(repo).count == 0,
               "GW-0006 must credit gRPC decision-service configuration")


def test_gw0006_follows_the_policy_into_the_baseline_owner():
    """GW-0006 must not be satisfiable by MOVING the decision hop to the shared module.

    This is a regression test for a real evasion, performed accidentally. When
    edge-gateway adopted the shared baseline and deleted its five local policy files,
    GW-0006 flipped from correctly reporting two HTTP decision hops to reporting clean -
    and nothing had been fixed. The hops now lived in platform-shared-go, which is neither
    a gateway nor a gRPC service, so all eight controls reported "does not satisfy" and
    skipped. The single file that decides the perimeter for every gateway was the only one
    no gateway control read.
    """
    baseline_owner = {
        # No service.contract.yaml and a name that is not a gateway: this repository must
        # come into scope on the strength of the embedded policy tree ALONE, since that is
        # what makes it the baseline owner.
        "platform/gatewaybaseline/config/auth.yaml":
            "auth:\n  validate_endpoint: /api/v1/auth/validate\n",
        "platform/gatewaybaseline/config/authz.yaml":
            "authz:\n  check_endpoint: /api/v1/authz/check-permission\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, baseline_owner)
        expect(repo.owns_shared_baseline(),
               "the embedded policy tree must identify the baseline owner")
        expect(m.applies(repo, "gateway-or-baseline-owner"),
               "GW-0006 must apply to the baseline owner, or centralising the policy "
               "removes it from enforcement")
        expect(not m.applies(repo, "gateway"),
               "the baseline owner is deliberately NOT a gateway; if it satisfied `gateway` "
               "the other seven controls would run on a module that enforces nothing")
        expect(m.grpc_decision_hops(repo).count == 2,
               "GW-0006 must report both HTTP decision hops in the shared baseline")

    # And a module that merely resembles the owner must not be pulled in: the trigger is
    # the policy tree, not a `platform/` directory.
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {"platform/envutil/env.go": "package envutil\n"})
        expect(not m.applies(repo, "gateway-or-baseline-owner"),
               "a shared module with no embedded baseline must stay out of scope")


def test_gw0007_grpc_interceptors():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GRPC_CONTRACT,
            "server.go": "package server\nfunc start() { _, _ = grpcx.NewServer(grpcx.ServerConfig{}) }\n",
        })
        expect(m.grpc_interceptor_security(repo).count == 0,
               "GW-0007 must credit the shared grpcx runtime that installs both controls")
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, {
            "service.contract.yaml": GRPC_CONTRACT,
            "server.go": "package server\nfunc start() { _ = grpc.NewServer() }\n",
        })
        expect(m.grpc_interceptor_security(repo).count == 1,
               "GW-0007 must reject a bare gRPC server with no authn/authz interceptor evidence")


def test_gw0008_tls_backend():
    bad = """package proxy
func dial(backendHost string) { _, _ = net.DialTimeout("tcp", backendHost, time.Second) }
"""
    commented = """package proxy
// Old code used net.DialTimeout("tcp", backendHost, time.Second).
func dial() { _ = "documented only" }
"""
    with tempfile.TemporaryDirectory() as tmp:
        expect(m.tls_backend_hops(make_repo(
            tmp, {"service.contract.yaml": GATEWAY_CONTRACT, "proxy.go": bad})).count == 1,
               "GW-0008 must reject a raw backend DialTimeout")
    with tempfile.TemporaryDirectory() as tmp:
        expect(m.tls_backend_hops(make_repo(
            tmp, {"service.contract.yaml": GATEWAY_CONTRACT, "proxy.go": commented})).count == 0,
               "GW-0008 must ignore raw dial examples in comments")


def test_catalog_shape_and_end_to_end_fixtures():
    doc = yaml.safe_load(CONTROLS.read_text(encoding="utf-8"))
    # The shape is api-contract.yaml's, deliberately: the GW family and the API family are the
    # same kind of control, so a reader who knows one catalog knows this one. `exact` rather than
    # a subset because an unrecognised key is far more likely to be a typo that silently does
    # nothing than a deliberate extension - `remediaton:` would read as present to a human and be
    # absent to every consumer.
    exact = {"id", "title", "owner", "scope", "status", "severity", "applies_when", "policy",
             "rationale", "remediation", "detector", "refs"}
    for control in doc["controls"]:
        expect(set(control) == exact,
               f"{control.get('id')}: catalogue entry must use the exact required shape; "
               f"missing={sorted(exact - set(control))} unexpected={sorted(set(control) - exact)}")

    # Every detector named by the catalog must resolve, and every implemented detector must be
    # named by exactly one control. The second half is the one worth having: an implementation
    # nothing references is dead code that still reads as enforcement.
    named = [c.get("detector") for c in doc["controls"]]
    expect(sorted(named) == sorted(set(named)), f"a detector is bound twice: {named}")
    expect(set(named) == set(m.DETECTORS),
           f"catalog and implementation disagree; catalog-only={sorted(set(named) - set(m.DETECTORS))} "
           f"implementation-only={sorted(set(m.DETECTORS) - set(named))}")

    # refs must name real policy documents rather than a plausible-looking string, since the
    # catalog is where a reader goes to find the normative text behind a finding.
    for control in doc["controls"]:
        for ref in control["refs"]:
            expect(re.fullmatch(r"(ADR|RFC)-\d{4}", ref) is not None,
                   f"{control['id']}: ref {ref!r} is not an ADR-NNNN or RFC-NNNN identifier")
        expect("RFC-0039" in control["refs"],
               f"{control['id']}: must cite RFC-0039, which carries its normative definition")

    clean = subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / "gateway-baseline-conformant"),
         "--controls", str(CONTROLS), "--format", "json"],
        capture_output=True, text=True,
    )
    expect(clean.returncode == 0,
           f"conformant end-to-end fixture must pass\n{clean.stdout}\n{clean.stderr}")

    bad = subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / "gateway-baseline-violating"),
         "--controls", str(CONTROLS), "--format", "json"],
        capture_output=True, text=True,
    )
    expect(bad.returncode == 1,
           f"violating end-to-end fixture must fail\n{bad.stdout}\n{bad.stderr}")
    try:
        report = json.loads(bad.stdout)
        fired = {r["control"] for r in report["results"] if r["result"] == "fail"}
        # GW-0003 is intentionally INDETERMINATE until the sibling release supplies its tag.
        for cid in ("GW-0001", "GW-0002", "GW-0004", "GW-0005",
                    "GW-0006", "GW-0007", "GW-0008"):
            expect(cid in fired, f"end-to-end violating fixture did not fire {cid}")
    except json.JSONDecodeError:
        expect(False, f"violating fixture emitted invalid JSON: {bad.stdout}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    if FAILURES:
        print(f"gateway-baseline controls: FAILED ({len(FAILURES)} assertion(s))")
        for failure in FAILURES:
            print(f"  ::error:: {failure}")
        return 1
    print(f"gateway-baseline controls: OK ({len(tests)} test functions; every control has "
          "a MUST-fail and MUST-pass case)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
