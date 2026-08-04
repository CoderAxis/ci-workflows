#!/usr/bin/env python3
"""Enforce controls/gateway-baseline.yaml against gateway and gRPC repositories.

Roots are positional, matching check-api-contract.py. Findings are ratcheted through
.gateway-baseline.json: only a count that rises fails; a fall is reported as improved.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")


SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "gateway-baseline.yaml"
DEFAULT_FLOORS = SELF_REPO / "controls" / "module-floors.yaml"
BASELINE_FILE = ".gateway-baseline.json"
BASELINE_COMMENT = ("Frozen gateway-baseline debt. The gate fails if any count RISES; "
                    "lower it by fixing violations, then re-run --write-baseline.")
PLATFORM_SHARED_MODULE = "github.com/coderaxis/platform-shared-go"
OWNED_KEYS = {"auth", "authz", "headers", "ratelimit", "security"}
KNOWN_GATEWAYS = {
    "inboxxhq-edge-gateway", "edge-gateway",
    "inboxxhq-org-bff", "org-bff",
    "inboxxhq-platform-bff", "platform-bff",
    "inboxxhq-realtime-gateway", "realtime-gateway",
    "inboxxhq-event-gateway", "event-gateway",
    "inboxxhq-voice-gateway", "voice-gateway",
}
SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity",
                   "applies_when", "policy", "rationale", "remediation", "detector", "refs")
MAX_DETAILS = 25


@dataclass
class Finding:
    count: int = 0
    evidence: str = ""
    details: list[str] = field(default_factory=list)
    indeterminate: bool = False


def capped(items: list[str]) -> list[str]:
    return items if len(items) <= MAX_DETAILS else items[:MAX_DETAILS] + [
        f"... and {len(items) - MAX_DETAILS} more"
    ]


def strip_go_comments(text: str) -> str:
    """Remove Go comments while preserving strings, runes and line numbers."""
    out, i, state = [], 0, "code"
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if state == "line":
            if c == "\n":
                out.append(c)
                state = "code"
            else:
                out.append(" ")
        elif state == "block":
            if c == "*" and n == "/":
                out.extend("  ")
                i += 1
                state = "code"
            else:
                out.append("\n" if c == "\n" else " ")
        elif state in ("string", "rune"):
            out.append(c)
            quote = '"' if state == "string" else "'"
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif c == quote:
                state = "code"
        elif state == "raw":
            out.append(c)
            if c == "`":
                state = "code"
        elif c == "/" and n == "/":
            out.extend("  ")
            i += 1
            state = "line"
        elif c == "/" and n == "*":
            out.extend("  ")
            i += 1
            state = "block"
        else:
            out.append(c)
            if c == '"':
                state = "string"
            elif c == "'":
                state = "rune"
            elif c == "`":
                state = "raw"
        i += 1
    return "".join(out)


def semver(value: str) -> tuple[int, int, int] | None:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", value or ""):
        return None
    return tuple(int(p) for p in value[1:].split("."))


def parse_go_mod(text: str) -> tuple[dict[str, str], set[str]]:
    requires, replaced, block = {}, set(), None
    for raw in text.splitlines():
        indirect = "// indirect" in raw
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line in ("require (", "require("):
            block = "require"
            continue
        if line in ("replace (", "replace("):
            block = "replace"
            continue
        if line == ")":
            block = None
            continue
        parts = line.split()
        if line.startswith("require ") and len(parts) >= 3 and not indirect:
            requires[parts[1]] = parts[2]
        elif line.startswith("replace ") and len(parts) >= 2:
            replaced.add(parts[1])
        elif block == "require" and len(parts) >= 2 and not indirect:
            requires[parts[0]] = parts[1]
        elif block == "replace" and parts:
            replaced.add(parts[0])
    return requires, replaced


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.name = root.name
        self.yaml_docs: list[tuple[Path, object]] = []
        self.yaml_errors: list[str] = []
        self.go_sources: list[tuple[Path, str]] = []
        self.baseline_error = ""
        self.baseline = self._load_baseline()
        self._load_sources()
        self.contract = self._contract()

    def _ignored(self, path: Path) -> bool:
        parts = path.relative_to(self.root).parts
        return any(p.startswith(".") for p in parts) or bool(
            {"vendor", "node_modules", "testdata"} & set(parts)
        )

    def _load_sources(self) -> None:
        for path in sorted(self.root.rglob("*.go")):
            if self._ignored(path) or path.name.endswith("_test.go"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.go_sources.append((path.relative_to(self.root), strip_go_comments(text)))
            except OSError:
                pass
        yaml_paths = sorted(self.root.rglob("*.yaml")) + sorted(self.root.rglob("*.yml"))
        for path in yaml_paths:
            if self._ignored(path) or path.name in ("gateway-baseline-exceptions.yaml",
                                                    "gateway-baseline-exceptions.yml"):
                continue
            try:
                self.yaml_docs.append((path.relative_to(self.root),
                                       yaml.safe_load(path.read_text(encoding="utf-8"))))
            except (OSError, yaml.YAMLError) as exc:
                self.yaml_errors.append(f"{path.relative_to(self.root)}: {exc}")

    def _contract(self) -> dict:
        path = self.root / "service.contract.yaml"
        if not path.is_file():
            return {}
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return doc if isinstance(doc, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    def _load_baseline(self) -> dict:
        path = self.root / BASELINE_FILE
        if not path.is_file():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            counts = doc.get("controls", {}) if isinstance(doc, dict) else None
            if not isinstance(counts, dict):
                raise ValueError("'controls' is not an object")
            return counts
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.baseline_error = str(exc)
            return {}

    def service(self) -> dict:
        value = self.contract.get("service") or {}
        return value if isinstance(value, dict) else {}

    def is_gateway(self) -> bool:
        service = self.service()
        return (str(service.get("type", "")).lower() in ("gateway", "bff")
                or self.name.endswith(("-gateway", "-bff"))
                or str(service.get("name", "")).endswith(("-gateway", "-bff")))

    def has_grpc(self) -> bool:
        ports = self.service().get("ports") or {}
        return isinstance(ports, dict) and bool(ports.get("grpc"))

    def owns_shared_baseline(self) -> bool:
        """Whether this repository DEFINES the shared baseline rather than consuming it.

        Detected by the embedded policy tree, not by repository name, so a rename or a
        second baseline package does not silently fall out of scope.

        This exists because centralising the perimeter created a blind spot. When
        edge-gateway adopted the shared baseline and deleted its five local policy files,
        GW-0006 went from correctly reporting two HTTP auth decision hops to reporting
        clean - and nothing had been fixed. The hops had moved into platform-shared-go,
        where every gateway control answered "repository does not satisfy gateway" and
        skipped. So the one file that now decides the perimeter for every gateway was the
        one file no gateway control inspected, and the control that guards the platform's
        most security-critical hop could be silenced by moving a line between
        repositories.
        """
        config = self.root / "platform" / "gatewaybaseline" / "config"
        return config.is_dir() and any(config.glob("*.yaml"))

    def is_internet_facing(self) -> bool | None:
        service = self.service()
        if service.get("internet_facing") is not None:
            return bool(service["internet_facing"])
        if "edge-gateway" in self.name or service.get("name") == "edge-gateway":
            return True
        # Absence of an ingress in an application repo is not proof it is internal: ingress is
        # infra-owned in this platform. Do not guess.
        return None

    def exceptions(self) -> dict[str, str]:
        for name in ("gateway-baseline-exceptions.yaml", "gateway-baseline-exceptions.yml"):
            path = self.root / name
            if not path.is_file():
                continue
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                return {}
            entries = doc.get("exceptions", []) if isinstance(doc, dict) else []
            result = {}
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                key, reason = str(entry.get("key", "")).strip(), str(entry.get("reason", "")).strip()
                if key in OWNED_KEYS and reason:
                    result[key] = reason
            return result
        return {}


def shared_baseline_loaded(repo: Repo) -> Finding:
    # GW-0001 asks one question only: has this gateway adopted the shared baseline. Whether it
    # ALSO keeps a local copy is GW-0002's question. An earlier revision short-circuited here on
    # finding local config/base files, which reported the same five files as violations of two
    # controls at once and left GW-0001's evidence describing a symptom of shadowing rather than
    # the absence of adoption it is named for.
    imports, calls = False, False
    for _, text in repo.go_sources:
        if re.search(r'"github\.com/coderaxis/platform-shared-go/[^"]*(?:gateway.?baseline|baseline)"',
                     text, re.I):
            imports = True
        if re.search(r"\b(?:gatewaybaseline|baseline)\.(?:Load|MustLoad|New)\w*\s*\(", text):
            calls = True
    if imports and calls:
        return Finding(evidence="imports the shared gateway baseline and calls its loader")
    facing = repo.is_internet_facing()
    if facing is True:
        missing = []
        if not imports:
            missing.append("shared gateway baseline import")
        if not calls:
            missing.append("baseline loader call")
        return Finding(1, f"internet-facing gateway is missing {', '.join(missing)}",
                       [f"repository source contains no {item}" for item in missing])
    return Finding(evidence=("INDETERMINATE: shared loader not visible and repository source "
                             "does not establish whether this gateway is internet-facing"),
                   indeterminate=True)


def owns_global_defaults(rel: Path) -> bool:
    """Is this file part of the global defaults layer the shared baseline replaces?

    Scope is the load-bearing decision in GW-0002, and getting it wrong in either direction is
    costly. Scanning every YAML in the repository - the first implementation - reported 174
    violations on the edge gateway, of which 159 were per-surface routing files and 10 were
    environment overlays. A control answerable only by writing 169 exception entries measures
    nothing, and it is the same defect shape as a control satisfiable only by redundant code.

    Two layers are deliberately OUT of scope, on the gateway's own documented design:

    config/surfaces/**  - per-surface routing policy. A surface declaring `auth: {required:
                          false}` for a signature-checked third-party webhook is making a routing
                          decision about one path, not redefining how the gateway authenticates.
                          The baseline package says the same thing from the other side: routing
                          configuration, including service registries, is not baseline scope.

    config/environments/** - per-environment overrides. The loader documents the layering as base
                          defaults first, then `config/environments/{env}.yaml` overriding them,
                          so an overlay is the sanctioned mechanism for environment variation
                          rather than a local redefinition of policy.
    """
    parts = rel.parts
    if parts[:2] in (("config", "surfaces"), ("config", "environments")):
        return False
    # Everything else the gateway calls configuration is in scope, whether it sits in
    # config/base/ or loose in config/, so a gateway cannot move a shadowing map one directory up
    # to escape the control.
    return parts[0] == "config" or (len(parts) == 1 and rel.suffix in (".yaml", ".yml"))


def no_local_policy_redefinition(repo: Repo) -> Finding:
    exceptions, violations = repo.exceptions(), []
    for rel, doc in repo.yaml_docs:
        if not isinstance(doc, dict) or not owns_global_defaults(rel):
            continue
        for key in sorted(OWNED_KEYS & set(doc)):
            if key not in exceptions:
                violations.append(f"{rel}: top-level {key!r} shadows baseline-owned policy "
                                  "(declare a reason in gateway-baseline-exceptions.yaml if intentional)")
    if violations:
        return Finding(len(violations), f"{len(violations)} unexcepted global policy map(s)",
                       capped(violations))
    return Finding(evidence="the global defaults layer declares no baseline-owned policy map")


def module_floor(repo: Repo) -> Finding:
    try:
        floors_doc = yaml.safe_load(DEFAULT_FLOORS.read_text(encoding="utf-8")) or {}
        spec = (floors_doc.get("floors") or {}).get(PLATFORM_SHARED_MODULE)
        floor = spec.get("min") if isinstance(spec, dict) else spec
    except (OSError, yaml.YAMLError):
        floor = None
    if not floor or semver(str(floor)) is None:
        return Finding(evidence=f"INDETERMINATE: no released gateway baseline floor is recorded "
                                f"for {PLATFORM_SHARED_MODULE}", indeterminate=True)
    go_mod = repo.root / "go.mod"
    if not go_mod.is_file():
        return Finding(1, "go.mod is absent", ["go.mod: cannot establish the required module pin"])
    requires, replaced = parse_go_mod(go_mod.read_text(encoding="utf-8", errors="ignore"))
    got = requires.get(PLATFORM_SHARED_MODULE)
    if not got:
        return Finding(1, "platform-shared-go is not a direct requirement",
                       [f"go.mod: require {PLATFORM_SHARED_MODULE} {floor} or newer directly"])
    if PLATFORM_SHARED_MODULE in replaced:
        return Finding(1, "platform-shared-go is replaced",
                       ["go.mod: replace makes the released module floor unverifiable"])
    if semver(got) is None:
        return Finding(1, f"platform-shared-go pin {got} is not a released stable version",
                       [f"go.mod: {got} cannot be compared with floor {floor}"])
    if semver(got) < semver(str(floor)):
        return Finding(1, f"platform-shared-go {got} is below floor {floor}",
                       [f"go.mod: bump {PLATFORM_SHARED_MODULE} to {floor} or newer"])
    return Finding(evidence=f"platform-shared-go {got} meets floor {floor}")


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def protocol_gateway_proxy(repo: Repo) -> Finding:
    violations, unresolved = [], []
    for rel, doc in repo.yaml_docs:
        for node in _walk(doc):
            if not isinstance(node, dict) or node.get("websocket") is not True:
                continue
            upstream = node.get("service", node.get("upstream"))
            if not isinstance(upstream, str) or "${" in upstream:
                unresolved.append(f"{rel}: websocket rule has a dynamic or missing upstream")
                continue
            normalized = upstream.strip().lower()
            if normalized in KNOWN_GATEWAYS or normalized.endswith(("-gateway", "-bff")):
                violations.append(f"{rel}: websocket: true proxies to gateway {upstream!r}")
    if violations:
        return Finding(len(violations), f"{len(violations)} protocol surface(s) proxied by a gateway",
                       capped(violations))
    if unresolved:
        return Finding(evidence=f"INDETERMINATE: {len(unresolved)} websocket upstream(s) unresolved",
                       details=capped(unresolved), indeterminate=True)
    return Finding(evidence="no websocket surface proxies another gateway")


def bounded_long_lived_connections(repo: Repo) -> Finding:
    violations, uncertain = [], []
    for rel, text in repo.go_sources:
        copies = list(re.finditer(r"\bio\.Copy\s*\(", text))
        if len(copies) < 2:
            if copies:
                uncertain.append(f"{rel}: long-lived copy found but bidirectional surface and "
                                 "limits cannot be correlated confidently")
            continue
        has_deadline = bool(re.search(r"\.Set(?:Read)?Deadline\s*\(", text))
        # Credit explicit connection limits, semaphores and bounded admission channels. Do not
        # credit MaxConnsPerHost: it governs http.Transport, not a hijacked raw connection.
        has_cap = bool(re.search(
            r"(?i)\b(?:max(?:imum)?_?(?:concurrent_?)?connections?|connection_?limit|"
            r"maxConnections|connLimit)\b|make\s*\(\s*chan\s+struct\{\}\s*,", text))
        if not has_deadline:
            violations.append(f"{rel}:{text[:copies[0].start()].count(chr(10)) + 1}: "
                              "bidirectional io.Copy tunnel has no read deadline")
        if not has_cap:
            violations.append(f"{rel}: bidirectional connection surface has no explicit "
                              "maximum concurrent connection cap (MaxConnsPerHost is not a raw cap)")
    if violations:
        return Finding(len(violations), f"{len(violations)} missing long-lived connection bound(s)",
                       capped(sorted(set(violations))))
    if uncertain:
        return Finding(evidence=f"INDETERMINATE: {len(uncertain)} long-lived copy site(s) could "
                                "not be classified", details=capped(uncertain), indeterminate=True)
    return Finding(evidence="every detected long-lived connection surface declares both bounds")


def grpc_decision_hops(repo: Repo) -> Finding:
    violations = []
    for rel, doc in repo.yaml_docs:
        for node in _walk(doc):
            if not isinstance(node, dict):
                continue
            for key in ("validate_endpoint", "check_endpoint"):
                value = node.get(key)
                if isinstance(value, str) and (value.startswith("/") or
                                               re.match(r"https?://", value, re.I)):
                    violations.append(f"{rel}: {key}={value!r} configures an HTTP decision hop")
    if violations:
        return Finding(len(violations), f"{len(violations)} HTTP auth decision endpoint(s)",
                       capped(violations))
    if repo.yaml_errors:
        return Finding(evidence="INDETERMINATE: relevant YAML could not all be parsed",
                       details=capped(repo.yaml_errors), indeterminate=True)
    return Finding(evidence="no HTTP auth validate/authz check endpoint is configured")


def grpc_interceptor_security(repo: Repo) -> Finding:
    all_text = "\n".join(text for _, text in repo.go_sources)
    # grpcx.NewServer owns both unary and stream authentication/authorization chains. The older
    # shared bootstrap is also legitimate: it delegates to that same platform runtime.
    if re.search(r"\bgrpcx\.NewServer\s*\(", all_text):
        return Finding(evidence="shared grpcx server runtime installs authentication and authorization")
    if re.search(r"\b(?:sharedgrpc|grpcHandler)\.StartGRPCServer\s*\(", all_text):
        return Finding(evidence="shared gRPC server bootstrap installs authentication and authorization")

    direct = re.search(r"\bgrpc\.NewServer\s*\(", all_text)
    if not direct:
        return Finding(evidence="INDETERMINATE: service contract declares gRPC but server assembly "
                                "is not visible in repository source", indeterminate=True)
    authn = bool(re.search(r"(?i)(?:authenticate|authentication|authn|service.?token).*(?:interceptor|chain)|"
                           r"(?:interceptor|chain).*(?:authenticate|authentication|authn|service.?token)",
                           all_text))
    authz = bool(re.search(r"(?i)(?:authorize|authorization|authz|permission).*(?:interceptor|chain)|"
                           r"(?:interceptor|chain).*(?:authorize|authorization|authz|permission)",
                           all_text))
    unary = "UnaryInterceptor" in all_text or "ChainUnaryInterceptor" in all_text
    stream = "StreamInterceptor" in all_text or "ChainStreamInterceptor" in all_text
    if authn and authz and unary and stream:
        return Finding(evidence="explicit unary and stream chains include authentication and authorization")
    if not authn and not authz:
        return Finding(1, "gRPC server has no authentication/authorization interceptor evidence",
                       ["grpc.NewServer is assembled without visible authn/authz interceptors"])
    return Finding(evidence="INDETERMINATE: custom gRPC server has partial interceptor evidence "
                            "that cannot prove both controls on all RPC kinds", indeterminate=True)


def tls_backend_hops(repo: Repo) -> Finding:
    violations, uncertain = [], []
    call_re = re.compile(r"\bnet\.(Dial|DialTimeout)\s*\(\s*[^,]+,\s*([^,\n\)]+)")
    for rel, text in repo.go_sources:
        for match in call_re.finditer(text):
            target = match.group(2).strip()
            line = text[:match.start()].count("\n") + 1
            if re.search(r"(?i)\b(?:backend|upstream|service|target).*host\b|"
                         r"\b(?:backendHost|upstreamHost|serviceHost|targetHost)\b", target):
                violations.append(f"{rel}:{line}: net.{match.group(1)} dials backend {target} without TLS")
            else:
                uncertain.append(f"{rel}:{line}: raw net.{match.group(1)} destination {target} "
                                 "cannot be classified as backend or non-backend")
    if violations:
        return Finding(len(violations), f"{len(violations)} plaintext backend dial(s)",
                       capped(violations))
    if uncertain:
        return Finding(evidence=f"INDETERMINATE: {len(uncertain)} raw dial destination(s) could "
                                "not be classified", details=capped(uncertain), indeterminate=True)
    return Finding(evidence="no raw gateway-to-backend net.Dial/net.DialTimeout call")


# Keyed by the catalog's `detector` NAME, not by control id, matching
# check-api-contract.py. RFC-0039 §"Machine verification" tells the reader that the catalog's
# detector key is what binds a control to its implementation, so binding by id here would make
# that statement false and would let a catalog entry be renamed without anything noticing that
# the implementation stayed behind.
DETECTORS = {
    "shared_baseline_loaded": shared_baseline_loaded,
    "no_local_policy_redefinition": no_local_policy_redefinition,
    "module_floor": module_floor,
    "protocol_gateway_proxy": protocol_gateway_proxy,
    "bounded_long_lived_connections": bounded_long_lived_connections,
    "grpc_decision_hops": grpc_decision_hops,
    "grpc_interceptor_security": grpc_interceptor_security,
    "tls_backend_hops": tls_backend_hops,
}


def load_controls(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"::error::cannot read control catalog {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: expected a controls list")
    errors, seen = [], set()
    for control in doc["controls"]:
        cid = control.get("id", "<missing>")
        missing = [key for key in REQUIRED_FIELDS if not control.get(key)]
        if missing:
            errors.append(f"{cid}: missing {missing}")
        if control.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {control.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate id")
        seen.add(cid)
    if errors:
        for error in errors:
            print(f"::error::gateway-baseline catalog invalid: {error}")
        raise SystemExit(2)
    return doc


def applies(repo: Repo, value: str) -> bool:
    if value == "gateway":
        return repo.is_gateway()
    if value == "grpc-service":
        return repo.has_grpc()
    # Controls about the CONTENT of the perimeter follow the content. A gateway is in
    # scope because it enforces the policy, and the baseline owner because it declares
    # it; scoping such a control to gateways alone lets the policy escape enforcement by
    # being centralised, which is the opposite of what centralising it is for.
    if value == "gateway-or-baseline-owner":
        return repo.is_gateway() or repo.owns_shared_baseline()
    return False


def evaluate(repo: Repo, controls: list[dict]) -> list[dict]:
    results = []
    for control in controls:
        cid = control["id"]
        rec = {
            "control": cid, "title": control["title"], "severity": control["severity"],
            "scope": control["scope"], "applies_when": control["applies_when"],
            "owner": control["owner"], "status": control["status"], "result": "skipped",
            "evidence": "", "details": [], "count": 0,
            "baseline": int(repo.baseline.get(cid, 0)),
        }
        if control["status"] != "active":
            rec["evidence"] = f"lifecycle status={control['status']}"
        elif not applies(repo, control["applies_when"]):
            rec["evidence"] = f"repository does not satisfy {control['applies_when']}"
        else:
            finding = DETECTORS[control["detector"]](repo)
            rec.update(evidence=finding.evidence, details=finding.details, count=finding.count)
            if finding.indeterminate:
                rec["result"] = "indeterminate"
            elif finding.count > rec["baseline"]:
                rec["result"] = "fail"
            elif finding.count < rec["baseline"]:
                rec["result"] = "improved"
            elif finding.count:
                rec["result"] = "frozen"
            else:
                rec["result"] = "pass"
        results.append(rec)
    return results


def enforced(rec: dict, threshold: int) -> bool:
    return rec["result"] == "fail" and SEVERITY_ORDER[rec["severity"]] >= threshold


MARK = {"pass": "ok", "fail": "XX", "frozen": "==", "improved": "->",
        "skipped": "--", "indeterminate": "??"}


def render_text(repo: Repo, results: list[dict], threshold: int) -> None:
    print(f"::group::gateway-baseline: {repo.name}")
    for rec in results:
        suffix = (f" (baseline {rec['baseline']}, now {rec['count']})"
                  if rec["result"] in ("frozen", "improved") else "")
        print(f"  [{MARK[rec['result']]}] {rec['control']} [{rec['severity']}] "
              f"{rec['title']}: {rec['evidence']}{suffix}")
    print("::endgroup::")
    for rec in results:
        if rec["result"] == "fail":
            level = "error" if enforced(rec, threshold) else "warning"
            print(f"::{level}::[{rec['control']}][{rec['severity']}] {rec['title']}: "
                  f"{rec['evidence']}")
            for detail in rec["details"]:
                print(f"    - {detail}")
        elif rec["result"] == "improved":
            print(f"::notice::[{rec['control']}] improved from {rec['baseline']} to "
                  f"{rec['count']} - run --write-baseline to lock the gain in")
        elif rec["result"] == "indeterminate":
            print(f"::notice::[{rec['control']}] {rec['evidence']}")


def report(repo: Repo, results: list[dict], doc: dict, fail_on: str, threshold: int) -> dict:
    return {
        "service": repo.name, "root": str(repo.root), "policy_ssot": doc.get("policy_ssot", []),
        "fail_on": fail_on, "gateway": repo.is_gateway(), "grpc_service": repo.has_grpc(),
        "baseline_present": bool(repo.baseline), "controls_total": len(results),
        "passed": sum(r["result"] == "pass" for r in results),
        "frozen": sum(r["result"] == "frozen" for r in results),
        "improved": sum(r["result"] == "improved" for r in results),
        "failed": sum(r["result"] == "fail" for r in results),
        "indeterminate": sum(r["result"] == "indeterminate" for r in results),
        "skipped": sum(r["result"] == "skipped" for r in results),
        "enforced_failures": sum(enforced(r, threshold) for r in results),
        "open_violations": {r["control"]: r["count"] for r in results if r["count"]},
        "ok": not any(enforced(r, threshold) for r in results), "results": results,
    }


def write_baseline(repo: Repo, results: list[dict]) -> None:
    counts = {r["control"]: r["count"] for r in results if r["count"]}
    path = repo.root / BASELINE_FILE
    if not counts:
        if path.is_file():
            path.unlink()
            print(f"gateway-baseline: {repo.name} is clean - removed {BASELINE_FILE}")
        else:
            print(f"gateway-baseline: {repo.name} is clean - no baseline needed")
        return
    path.write_text(json.dumps({"_comment": BASELINE_COMMENT, "controls": counts}, indent=2) + "\n",
                    encoding="utf-8")
    print(f"gateway-baseline: wrote {path} ({sum(counts.values())} violation(s) frozen)")


def render_markdown(doc: dict) -> str:
    lines = ["| Control | Policy | Severity | Scope | Applies | Owner | Status |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for control in doc["controls"]:
        policy = " ".join(str(control["policy"]).split())
        lines.append(f"| {control['id']} | {policy} | {control['severity']} | "
                     f"{control['scope']} | {control['applies_when']} | {control['owner']} | "
                     f"{control['status']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", default=["."], help="repository roots to check")
    parser.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    parser.add_argument("--report", help="write the JSON report to this path")
    parser.add_argument("--write-baseline", action="store_true",
                        help=f"freeze current violation counts into {BASELINE_FILE}")
    args = parser.parse_args(argv)
    doc = load_controls(Path(args.controls))
    if args.format == "markdown":
        print(render_markdown(doc))
        return 0
    threshold = SEVERITY_ORDER[args.fail_on]
    reports, failed = [], False
    for raw in args.roots or ["."]:
        root = Path(raw).resolve()
        if not root.is_dir():
            print(f"::error::not a directory: {root}")
            return 2
        repo = Repo(root)
        if repo.baseline_error and not args.write_baseline:
            print(f"::error::{root / BASELINE_FILE} is unreadable: {repo.baseline_error}")
            return 2
        results = evaluate(repo, doc["controls"])
        if args.write_baseline:
            write_baseline(repo, results)
            continue
        payload = report(repo, results, doc, args.fail_on, threshold)
        reports.append(payload)
        if args.format == "text":
            render_text(repo, results, threshold)
        failed = failed or not payload["ok"]
    if args.write_baseline:
        return 0
    payload = reports[0] if len(reports) == 1 else {
        "repos": reports, "ok": all(item["ok"] for item in reports)
    }
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
