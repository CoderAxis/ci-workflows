#!/usr/bin/env python3
"""RFC-0007 §10 port guard (CI). Run against an assembled multi-repo workspace.

Executable, data-driven policy-as-code for the port half of RFC-0007. This is the port-domain
analogue of check-dockerfile-standard.py / check-gateway-baseline.py: the control catalog
(controls/service-ports.yaml) defines POLICY ONLY; this file provides DETECTOR implementations
bound to it by name.

Design (identical framework to check-dockerfile-standard.py):
  RFC/ADR (intent) -> control catalog (policy + severity + ownership + lifecycle)
                   -> detector (verifies compliance) -> CI (executes).
  * DATA-DRIVEN, SEVERITY-AWARE (critical/major fail; minor advisory via --fail-on).
  * LIFECYCLE-AWARE: a control with `status: deferred` is reported and never enforced, which
    is how SP-0006 records the migration's destination without gating on it.
  * RATCHETED: findings are frozen in controls/service-ports-baseline.json. Only a count that
    RISES fails; a fall is reported as improved. Mirrors check-gateway-baseline.py.
  * STATIC ANALYSIS ONLY. Never contacts a cluster.

WHICH PORT TYPES ARE ASSERTED
-----------------------------
RFC-0007 §4.1 names four listeners, and the first revision of this checker asserted the first
three unevenly and the fourth not at all:

  HTTP     SP-0001 agreement, SP-0002 against the registry, SP-0006 the constant
  gRPC     the same three
  metrics  SP-0001 agreement and SP-0006 the constant, plus SP-0010 for the scrape config.
           NOT compared against the registry, and deliberately: the registry holds the two
           ports a caller dials, and Prometheus does not resolve through it. Adding a metrics
           port there would be a copy of a fact nothing reads, which is the failure §12 records.
  pprof    SP-0008. Nothing checked it before, and two services carry a PPROF_PORT that is not
           6060. It is the one port whose correct state is "declared but unreachable", so the
           control asserts both the constant and the absence from any Service or containerPort.

SP-0001 also asserts each port ENVIRONMENT value against its own concern rather than against
the set of container ports. Set membership passes PORT=50051 on a service publishing http=4015,
which is an HTTP listener advertised on the gRPC number: agreement about the wrong thing.

WHY IT NEEDS A WORKSPACE RATHER THAN A REPO
-------------------------------------------
The facts this checks live in three repositories and the defects are precisely the
disagreements BETWEEN them: the manifests are in inboxxhq-infra, the Dockerfiles and service
contracts are in the service repos, and the registry every caller resolves through is in
platform-shared-go. A per-repo checker can only ever confirm that a repository agrees with
itself, which is the one thing that was never wrong - all 44 services are internally
consistent today, and 39 of them are consistent about a number the standard withdrew.

So --workspace names an assembled tree, laid out as platform-governance.yaml assembles it:

  <workspace>/
    infra/inboxxhq-infra/services/<service>/{base,overlays}/     manifests
    services/<domain>/<repo>/                                    Dockerfile, contract
    gateways/<repo>/                                             Dockerfile, contract
    shared/platform-shared-go/platform/servicediscovery/ports.generated.go

ports.generated.go holds TWO tables since the RFC-0007 §4 split: clusterPorts, generated from
the manifests, and localPorts, the per-service allocation §4.2 retains for a workstation. They
have the same shape, so each is located by name -- collecting entries from both would leave
SP-0002 comparing the manifests against the local table, which reads as green while the two
agree and turns red the moment a service migrates.

Trees that are absent are SKIPPED with that recorded as the evidence, never passed. A checker
that reports "clean" about a directory it was not given is reporting on its own reach.

WHAT THIS DELIBERATELY DOES NOT ENFORCE
---------------------------------------
RFC-0007 §10.3 - no *_SERVICE_URL / *_GRPC_ENDPOINT - is ALREADY enforced, in both halves,
and neither is here:

  application code   ARCH-0010 in inboxxhq-architecture-check (Error, baselined, per repo)
  manifests          inboxxhq-infra scripts/check-service-addressing.py (ratcheted at 321)

SP-0007 reports the manifest count as an ADVISORY cross-check so the three can be compared,
and never as a second hard gate. The two existing halves share one deliberately-wide pattern
so they cannot disagree about what is forbidden; a third independent copy would be a third
thing to keep in step, and drift between copies of a fact is the failure RFC-0007 §12 records.

Usage:
  check-service-ports.py --workspace DIR [--controls PATH] [--format text|json|markdown]
                         [--fail-on critical|major|minor] [--report PATH]
  check-service-ports.py --workspace DIR --write-baseline
  check-service-ports.py --write-docs README.md
  check-service-ports.py --verify-docs README.md

SSOT: this file lives in coderaxis/ci-workflows and is invoked by the central reusable
workflow .github/workflows/service-ports.yaml.
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
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("::error::PyYAML is required: python3 -m pip install PyYAML") from exc

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "service-ports.yaml"
BASELINE_PATH = SELF_REPO / "controls" / "service-ports-baseline.json"
BASELINE_COMMENT = ("Frozen RFC-0007 §10 port debt. The gate fails if any count RISES; lower "
                    "it by fixing violations, then re-run --write-baseline. Every entry must "
                    "carry a reason and a phase in the migration playbook.")

SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
# `deferred` is not a lifecycle borrowed from the neighbouring catalogs. It means the control
# states a real, current requirement that the fleet does not yet meet, as distinct from
# `deprecated` (was a requirement, is not) or `superseded` (replaced by another control).
# SP-0006 is the case it exists for: RFC-0007 §4.1's constants are the standard TODAY and 39
# services do not meet them, so calling that control deprecated would be false and calling it
# active would make the lane permanently red.
VALID_STATUS = {"active", "deferred", "deprecated", "superseded"}
VALID_SCOPE = {"workspace"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "policy",
                   "rationale", "remediation", "detector", "refs")

# RFC-0007 §4.1. Named here rather than read from the RFC because this checker must run with
# no network and no core-docs checkout; §4.1 records the same values and why they were chosen.
#
# All FOUR listeners §4.1 names are here, not the three that appear in a Service. pprof is
# absent from every Service on purpose -- §9 requires it to bind 127.0.0.1 and stay disabled
# outside local development -- but "absent from the Service" is not the same as "unchecked", and
# it was unchecked: two services carry a PPROF_PORT that is not 6060, which nothing objected to.
# SP-0008 asserts the constant AND the absence, because both are §4.1 requirements and the
# second is the one that would let a debug endpoint become reachable in a cluster.
CONSTANTS = {"http": 8080, "grpc": 50051, "metrics": 9464}
PPROF_CONSTANT = 6060

# RFC-0007 §3. These constrain a genuinely shared resource, which is why they survived the
# withdrawal of the per-service allocation. The §4.1 constants were chosen so none appears
# here, so SP-0003 and SP-0006 cannot contradict each other.
RESERVED_PORTS = {80, 443, 3000, 9090, 9091, 9093, 3100, 3200, 4317, 4318, 9095, 5432,
                  6379, 8500}

# Deliberately identical to ARCH-0010's and to inboxxhq-infra's check-service-addressing.py.
# SP-0007 only counts what those two enforce; if the three patterns drift, the advisory number
# stops describing the gated one and becomes worse than no number at all.
ADDRESS_VAR = re.compile(
    r"\bname:\s*([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:SERVICE_URL|SERVICE_BASE_URL|SERVICE_HOST"
    r"|SERVICE_PORT|GRPC_ENDPOINT|GRPC_ADDR|GRPC_ADDRESS|GRPC_URL|GRPC))\b"
)
NOT_AN_ADDRESS = re.compile(
    r"\A(?:[A-Z][A-Z0-9_]*_USE_GRPC|KUBERNETES_SERVICE_(?:HOST|PORT[A-Z0-9_]*))\Z")
EXEMPT_ADDRESS_VARS = {"CHECKOUT_SUCCESS_URL", "CHECKOUT_CANCEL_URL",
                       "ORDER_CHECKOUT_SUCCESS_URL", "ORDER_CHECKOUT_CANCEL_URL"}

PORT_ENV_NAMES = ("PORT", "GRPC_PORT", "METRICS_PORT")
# Which listener each of those describes. Membership in the containerPort set was the only
# thing checked before, and membership is too weak to catch the mistake this migration makes:
# PORT=50051 in a service whose Service still publishes http=4015 is a set member, so it passed,
# and it is a Pod serving HTTP where the gRPC listener is expected.
PORT_ENV_CONCERN = {"PORT": "http", "GRPC_PORT": "grpc", "METRICS_PORT": "metrics"}
PPROF_ENV_NAME = "PPROF_PORT"
MAX_DETAILS = 25

DOCS_BEGIN = "<!-- BEGIN service-ports-controls (generated: scripts/check-service-ports.py --write-docs) -->"
DOCS_END = "<!-- END service-ports-controls -->"


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


# --- workspace model --------------------------------------------------------------------

@dataclass
class Service:
    """One service, assembled from every repository that holds a fact about its ports."""
    name: str
    infra_dir: Path
    published: dict[str, int] = field(default_factory=dict)   # port name -> spec.ports[].port
    target_ports: dict[str, int] = field(default_factory=dict)
    overlays: dict[str, dict] = field(default_factory=dict)   # env -> parsed port facts
    # ServiceMonitor endpoints, as declared: a string is a port NAME, an int is a number.
    # RFC-0007 §9 requires the name, and the distinction is the whole content of SP-0010.
    monitor_ports: list[object] = field(default_factory=list)
    monitor_path: str = ""
    repo_dir: Path | None = None
    parse_errors: list[str] = field(default_factory=list)

    def all_published(self) -> set[int]:
        return set(self.published.values())


class Workspace:
    """The assembled tree. Anything absent is recorded as absent, never assumed compliant."""

    def __init__(self, root: Path):
        self.root = root
        self.infra = root / "infra" / "inboxxhq-infra"
        self.registry_path = (root / "shared" / "platform-shared-go" / "platform" /
                              "servicediscovery" / "ports.generated.go")
        self.registry: dict[str, tuple[int, int]] = {}       # clusterPorts: what a Pod answers on
        self.local_registry: dict[str, tuple[int, int]] = {}  # localPorts: what a laptop binds
        self.registry_error = ""
        self.local_registry_error = ""
        self.services: dict[str, Service] = {}
        self.repos_by_service: dict[str, Path] = {}
        self._load_registry()
        self._load_repos()
        self._load_services()

    # ports.generated.go is Go source, and parsing it with a regex is a deliberate choice over
    # `go run`: this checker runs on a Python runner with no Go toolchain, and the file's whole
    # purpose is to be a flat generated table. The header asserts DO NOT EDIT, so the shape is
    # the generator's to keep stable, and a shape change fails loudly below rather than
    # silently yielding an empty registry that would make every SP-0002 comparison vacuous.
    _REGISTRY_ENTRY = re.compile(
        r'"([a-z0-9-]+)":\s*\{HTTPPort:\s*(\d+),\s*GRPCPort:\s*(\d+)\}')

    # THE TABLE HAS TO BE NAMED, and this is the one place in this file where a loose match
    # would be actively dangerous rather than merely imprecise.
    #
    # The file now holds two tables of identical shape: clusterPorts, generated from the
    # manifests, and localPorts, the per-service allocation a workstation binds (RFC-0007 §4.2).
    # Matching entries anywhere in the file collects both, and because a dict keeps the last
    # write, SP-0002 would end up comparing the manifests against the LOCAL table. That reads
    # as green today, when the two agree for every unmigrated service, and turns red the moment
    # a service migrates -- with a finding that looks exactly like the outage SP-0002 exists to
    # catch. So each table is located by name and parsed from its own block.
    @staticmethod
    def _table(text: str, name: str, value_type: str = "ServiceEndpoint") -> str | None:
        opener = f"var {name} = map[string]{value_type}{{"
        start = text.find(opener)
        if start < 0:
            return None
        end = text.find("\n}", start)
        return text[start + len(opener):end if end > 0 else len(text)]

    # The cluster table records which listeners a service runs, not on what port: every service
    # in Kubernetes answers on the §4.1 constants, so a per-service number there would be a copy
    # of a constant that each caller pins separately. The ports below are therefore read from
    # the same constants the resolver uses, and the table supplies only membership and shape.
    #
    # Which still has to be read. A service with no gRPC listener must resolve to no gRPC port
    # rather than to 50051, or SP-0002 would require org-bff's Service to publish a gRPC port it
    # does not serve.
    _CLUSTER_HTTP_PORT = 8080
    _CLUSTER_GRPC_PORT = 50051
    _LISTENERS_ENTRY = re.compile(
        r'"([a-z0-9-]+)":\s*\{HTTP:\s*(true|false),\s*GRPC:\s*(true|false)\}')

    def _load_registry(self) -> None:
        if not self.registry_path.is_file():
            self.registry_error = f"{self._rel(self.registry_path)} is absent"
            self.local_registry_error = self.registry_error
            return
        text = self.registry_path.read_text(encoding="utf-8", errors="replace")

        cluster = self._table(text, "clusterListeners", "listeners")
        if cluster is None:
            if self._table(text, "clusterPorts") is not None:
                legacy = (" (it still declares `clusterPorts`, which recorded a port per service "
                          "-- the copy the §4.1 constants removed)")
            elif "standardPorts" in text:
                legacy = (" (it still declares the pre-split `standardPorts`, which served both "
                          "environments from one table)")
            else:
                legacy = ""
            self.registry_error = (f"{self._rel(self.registry_path)} declares no "
                                   f"`clusterListeners` table{legacy}")
        else:
            for match in self._LISTENERS_ENTRY.finditer(cluster):
                name, has_http, has_grpc = match.group(1), match.group(2) == "true", \
                    match.group(3) == "true"
                self.registry[name] = (
                    self._CLUSTER_HTTP_PORT if has_http else 0,
                    self._CLUSTER_GRPC_PORT if has_grpc else 0,
                )
            if not self.registry:
                self.registry_error = (f"{self._rel(self.registry_path)} parsed to zero cluster "
                                       "entries; the generated table's shape has changed")

        local = self._table(text, "localPorts")
        if local is None:
            self.local_registry_error = (f"{self._rel(self.registry_path)} declares no "
                                         "`localPorts` table, so the local-development "
                                         "allocation RFC-0007 §4.2 retains cannot be read")
        else:
            for match in self._REGISTRY_ENTRY.finditer(local):
                self.local_registry[match.group(1)] = (int(match.group(2)), int(match.group(3)))
            if not self.local_registry:
                self.local_registry_error = (f"{self._rel(self.registry_path)} parsed to zero "
                                             "local entries")

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # A repository's directory name is not its service name: inboxxhq-notification-service-ctx
    # holds notification-service. service.contract.yaml declares the real one, so it is read
    # first and the directory name is only a fallback. Getting this backwards turns a working
    # repository into an SP-0004 finding about a port it legitimately owns.
    def _load_repos(self) -> None:
        for pattern in ("services/*/*", "gateways/*"):
            for path in sorted(self.root.glob(pattern)):
                if not path.is_dir():
                    continue
                name = self._service_name_of(path)
                if name and name not in self.repos_by_service:
                    self.repos_by_service[name] = path

    def _service_name_of(self, repo: Path) -> str | None:
        contract = repo / "service.contract.yaml"
        if contract.is_file():
            try:
                doc = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                doc = {}
            if isinstance(doc, dict):
                declared = (doc.get("service") or {}).get("name") if isinstance(
                    doc.get("service"), dict) else doc.get("name")
                if isinstance(declared, str) and declared.strip():
                    return re.sub(r"^inboxxhq-", "", declared.strip())
        if repo.name.startswith("inboxxhq-"):
            return repo.name[len("inboxxhq-"):]
        return None

    def _load_services(self) -> None:
        services_dir = self.infra / "services"
        if not services_dir.is_dir():
            return
        for path in sorted(services_dir.iterdir()):
            if not path.is_dir():
                continue
            service = Service(name=path.name, infra_dir=path)
            self._load_base(service)
            self._load_monitor(service)
            self._load_overlays(service)
            if not service.published and not service.overlays:
                # A directory with no Service and no Deployment is a Job or a config bundle
                # (kafka-topic-init, observability-config). It has no ports to be wrong about.
                continue
            service.repo_dir = self.repos_by_service.get(service.name)
            self.services[service.name] = service

    def _load_base(self, service: Service) -> None:
        path = service.infra_dir / "base" / "service.yaml"
        if not path.is_file():
            return
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            service.parse_errors.append(f"{self._rel(path)}: {exc}")
            return
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "Service":
                continue
            for entry in (doc.get("spec") or {}).get("ports") or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or f"port-{entry.get('port')}")
                if isinstance(entry.get("port"), int):
                    service.published[name] = entry["port"]
                # A string targetPort names a containerPort by name, which is a valid and
                # strictly safer spelling; only a numeric one can disagree numerically.
                if isinstance(entry.get("targetPort"), int):
                    service.target_ports[name] = entry["targetPort"]

    def _load_monitor(self, service: Service) -> None:
        path = service.infra_dir / "base" / "servicemonitor.yaml"
        if not path.is_file():
            return
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            service.parse_errors.append(f"{self._rel(path)}: {exc}")
            return
        service.monitor_path = self._rel(path)
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "ServiceMonitor":
                continue
            for endpoint in (doc.get("spec") or {}).get("endpoints") or []:
                if not isinstance(endpoint, dict):
                    continue
                # `port` names a Service port; `targetPort` may be either. Both are collected
                # because either one spelled as a number is the defect §9 describes.
                for key in ("port", "targetPort"):
                    if key in endpoint and endpoint[key] is not None:
                        service.monitor_ports.append(endpoint[key])

    def _load_overlays(self, service: Service) -> None:
        overlays = service.infra_dir / "overlays"
        if not overlays.is_dir():
            return
        for env_dir in sorted(overlays.iterdir()):
            path = env_dir / "deployment.yaml"
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                docs = list(yaml.safe_load_all(text))
            except (OSError, yaml.YAMLError) as exc:
                service.parse_errors.append(f"{self._rel(path)}: {exc}")
                continue
            facts = {"path": self._rel(path), "container_ports": set(),
                     "probe_ports": set(), "env_ports": {}, "pprof_ports": set()}
            for doc in docs:
                if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
                    continue
                spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
                for container in spec.get("containers") or []:
                    if not isinstance(container, dict):
                        continue
                    self._collect_container_ports(container, facts)
            if facts["container_ports"] or facts["probe_ports"]:
                service.overlays[env_dir.name] = facts

    @staticmethod
    def _collect_container_ports(container: dict, facts: dict) -> None:
        for entry in container.get("ports") or []:
            if isinstance(entry, dict) and isinstance(entry.get("containerPort"), int):
                facts["container_ports"].add(entry["containerPort"])
        for entry in container.get("env") or []:
            if not isinstance(entry, dict):
                continue
            name, value = entry.get("name"), entry.get("value")
            if name in PORT_ENV_NAMES and isinstance(value, (str, int)):
                try:
                    facts["env_ports"][name] = int(str(value))
                except ValueError:
                    pass
            if name == PPROF_ENV_NAME and isinstance(value, (str, int)):
                try:
                    facts["pprof_ports"].add(int(str(value)))
                except ValueError:
                    pass
        for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
            spec = container.get(probe)
            if not isinstance(spec, dict):
                continue
            for handler in ("httpGet", "tcpSocket", "grpc"):
                target = spec.get(handler)
                if isinstance(target, dict) and isinstance(target.get("port"), int):
                    facts["probe_ports"].add(target["port"])


# --- detectors ---------------------------------------------------------------------------

def k8s_ports_internally_consistent(ws: Workspace) -> Finding:
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations = []
    for service in ws.services.values():
        for name, port in service.published.items():
            target = service.target_ports.get(name)
            if target is not None and target != port:
                violations.append(f"{service.name}: Service port {name}={port} but "
                                  f"targetPort={target}; the Service routes to a port it does "
                                  "not publish")
        published = service.all_published()
        for env, facts in service.overlays.items():
            for port in sorted(facts["container_ports"]):
                if published and port not in published:
                    violations.append(f"{service.name}/{env}: containerPort {port} is not "
                                      f"published by the Service {sorted(published)} "
                                      f"({facts['path']})")
            for port in sorted(facts["probe_ports"]):
                if facts["container_ports"] and port not in facts["container_ports"]:
                    violations.append(f"{service.name}/{env}: probe targets {port}, which no "
                                      f"container binds {sorted(facts['container_ports'])} "
                                      f"({facts['path']})")
            for var, port in sorted(facts["env_ports"].items()):
                if facts["container_ports"] and port not in facts["container_ports"]:
                    violations.append(f"{service.name}/{env}: {var}={port} but containerPorts "
                                      f"are {sorted(facts['container_ports'])} "
                                      f"({facts['path']})")
                # ...and it must be the port for ITS OWN concern. Membership alone accepts
                # PORT=50051 on a service publishing http=4015, which is an HTTP listener
                # advertised on the gRPC number - agreement about the wrong thing.
                concern = PORT_ENV_CONCERN[var]
                expected = service.published.get(concern)
                if expected is not None and port != expected:
                    violations.append(f"{service.name}/{env}: {var}={port} but the Service "
                                      f"publishes {concern}={expected} ({facts['path']})")
                elif expected is None and service.published:
                    violations.append(f"{service.name}/{env}: {var}={port} but the Service "
                                      f"publishes no {concern} port ({facts['path']})")
    if violations:
        return Finding(len(violations), f"{len(violations)} inconsistent port declaration(s)",
                       capped(violations))
    envs = sum(len(s.overlays) for s in ws.services.values())
    return Finding(evidence=f"every containerPort, targetPort, probe and PORT value agrees "
                            f"across {len(ws.services)} services and {envs} overlays")


def k8s_ports_match_registry(ws: Workspace) -> Finding:
    if ws.registry_error:
        return Finding(evidence=f"INDETERMINATE: {ws.registry_error}", indeterminate=True)
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations, unregistered = [], []
    for service in ws.services.values():
        entry = ws.registry.get(service.name)
        if entry is None:
            # Not every directory under services/ is an addressable service: kafka-exporter and
            # notification-canary publish a metrics port and are scraped, never resolved by
            # logical name. Absence from the registry is the correct state for them, so this is
            # reported rather than counted - counting it would make the control fail on
            # infrastructure that has nothing to comply with.
            if service.published:
                unregistered.append(f"{service.name}: publishes {sorted(service.all_published())} "
                                    "and has no registry entry (expected for a scrape-only "
                                    "component; a violation for an addressable service)")
            continue
        http_port, grpc_port = entry
        for name, expected in (("http", http_port), ("grpc", grpc_port)):
            actual = service.published.get(name)
            if actual is None:
                continue
            if expected and actual != expected:
                violations.append(f"{service.name}: Service publishes {name}={actual} but the "
                                  f"registry resolves callers to {expected}; one of them is "
                                  "unreachable")
            elif not expected:
                # Zero is how the generator records "this service has no listener of that
                # kind", and the callers treat it as unresolvable rather than dialling it. A
                # Service that publishes the port anyway is reachable by everything except the
                # registry, which is the one path RFC-0007 §5 allows.
                violations.append(f"{service.name}: Service publishes {name}={actual} but the "
                                  f"registry records no {name} port, so a caller resolving by "
                                  "name is refused rather than routed")
    if violations:
        return Finding(len(violations), f"{len(violations)} service(s) disagree with the registry",
                       capped(violations))
    checked = sum(1 for s in ws.services if s in ws.registry)
    evidence = f"{checked} service(s) publish exactly what the registry resolves callers to"
    return Finding(evidence=evidence, details=capped(unregistered))


def no_reserved_ports(ws: Workspace) -> Finding:
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations = []
    for service in ws.services.values():
        for name, port in sorted(service.published.items()):
            if port in RESERVED_PORTS:
                violations.append(f"{service.name}: publishes {name}={port}, reserved to "
                                  "platform infrastructure by RFC-0007 §3")
        for env, facts in service.overlays.items():
            for port in sorted(facts["container_ports"] & RESERVED_PORTS):
                violations.append(f"{service.name}/{env}: binds containerPort {port}, reserved "
                                  "by RFC-0007 §3")
    if violations:
        return Finding(len(violations), f"{len(violations)} reserved-port binding(s)",
                       capped(violations))
    return Finding(evidence=f"no service among {len(ws.services)} binds an RFC-0007 §3 "
                            "reserved infrastructure port")


_EXPOSE_RE = re.compile(r"^EXPOSE\s+(.*)$", re.MULTILINE)
_HEALTHCHECK_PORT_RE = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d+)")


def dockerfile_ports_are_own(ws: Workspace) -> Finding:
    if not ws.repos_by_service:
        return Finding(evidence="INDETERMINATE: no service repositories in the workspace",
                       indeterminate=True)
    # Who owns which port, from the manifests. Built once so a Dockerfile naming a port can be
    # told WHOSE it is - which is the whole difference between this control and DS-0006.
    owner: dict[int, set[str]] = {}
    for service in ws.services.values():
        for port in service.all_published():
            owner.setdefault(port, set()).add(service.name)

    violations, unchecked = [], []
    for name, repo in sorted(ws.repos_by_service.items()):
        dockerfile = repo / "Dockerfile"
        if not dockerfile.is_file():
            continue
        service = ws.services.get(name)
        if service is None or not service.published:
            unchecked.append(f"{repo.name}: no Kubernetes Service found for {name!r}, so its "
                             "own ports cannot be established")
            continue
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        mine = service.all_published()
        exposed = {int(tok) for line in _EXPOSE_RE.findall(text)
                   for tok in re.findall(r"\d+", line)}
        checked = {int(port) for port in _HEALTHCHECK_PORT_RE.findall(text)}
        for port in sorted(exposed | checked):
            if port in mine:
                continue
            others = sorted(owner.get(port, set()) - {name})
            where = "EXPOSE" if port in exposed else "HEALTHCHECK"
            if others:
                violations.append(f"{repo.name}: {where} names port {port}, which belongs to "
                                  f"{', '.join(others)}, not to {name}")
            else:
                violations.append(f"{repo.name}: {where} names port {port}, which {name} does "
                                  f"not publish {sorted(mine)}")
        if checked and not (checked & mine):
            violations.append(f"{repo.name}: HEALTHCHECK targets {sorted(checked)}, none of "
                              f"which {name} publishes")
    if violations:
        return Finding(len(violations), f"{len(violations)} Dockerfile port(s) not owned by "
                                        "their service", capped(sorted(set(violations))))
    return Finding(evidence=f"every EXPOSE and HEALTHCHECK port across "
                            f"{len(ws.repos_by_service)} repositories belongs to its own "
                            "service", details=capped(unchecked))


def _contract_ports(repo: Path) -> tuple[dict[str, int], str | None]:
    path = repo / "service.contract.yaml"
    if not path.is_file():
        return {}, None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"{path.name}: {exc}"
    if not isinstance(doc, dict):
        return {}, None
    # Two shapes are live in the fleet: ports nested under `service:` and ports at the top
    # level. Both are read, because a checker that understands one of them silently skips
    # every repository using the other.
    ports = doc.get("ports")
    if not isinstance(ports, dict):
        ports = (doc.get("service") or {}).get("ports") if isinstance(
            doc.get("service"), dict) else None
    if not isinstance(ports, dict):
        return {}, None
    return {str(k): v for k, v in ports.items() if isinstance(v, int)}, None


def _catalog_ports(ws: Workspace) -> dict[str, int]:
    """service -> the `canonical_port` its core-docs catalog entry declares.

    A ninth place a service's HTTP port is written, found while auditing the eighteen in
    infra. It agrees fleet-wide today, which is exactly why it is worth holding: it is the
    copy most likely to be forgotten during the migration, because nothing dials it and no
    rollout reads it. An unread copy that is right is one deploy away from being an unread
    copy that is wrong.
    """
    catalog = ws.root / "docs" / "core-docs" / "catalog" / "services"
    if not catalog.is_dir():
        return {}
    found: dict[str, int] = {}
    for path in sorted(catalog.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("canonical_port"), int) and doc.get("name"):
            found[str(doc["name"])] = doc["canonical_port"]
    return found


def contract_ports_match_service(ws: Workspace) -> Finding:
    if not ws.repos_by_service:
        return Finding(evidence="INDETERMINATE: no service repositories in the workspace",
                       indeterminate=True)
    owner: dict[int, set[str]] = {}
    for service in ws.services.values():
        for port in service.all_published():
            owner.setdefault(port, set()).add(service.name)

    violations, errors, checked = [], [], 0
    catalog_ports = _catalog_ports(ws)
    for name, port in sorted(catalog_ports.items()):
        service = ws.services.get(name)
        if service is None or "http" not in service.published:
            continue
        if port != service.published["http"]:
            violations.append(f"catalog/services/{name}.yaml: canonical_port={port} but "
                              f"base/service.yaml publishes http={service.published['http']}")
    for name, repo in sorted(ws.repos_by_service.items()):
        declared, error = _contract_ports(repo)
        if error:
            errors.append(f"{repo.name}: {error}")
            continue
        if not declared:
            continue
        service = ws.services.get(name)
        if service is None or not service.published:
            continue
        checked += 1
        mine = service.all_published()
        for concern, port in sorted(declared.items()):
            expected = service.published.get(concern)
            if expected is not None and port != expected:
                violations.append(f"{repo.name}: service.contract.yaml declares {concern}="
                                  f"{port} but base/service.yaml publishes {expected}")
                continue
            if expected is None and port not in mine:
                others = sorted(owner.get(port, set()) - {name})
                if others:
                    violations.append(f"{repo.name}: service.contract.yaml declares {concern}="
                                      f"{port}, which belongs to {', '.join(others)}")
                else:
                    violations.append(f"{repo.name}: service.contract.yaml declares {concern}="
                                      f"{port}, which no Kubernetes Service publishes")
    if violations:
        return Finding(len(violations), f"{len(violations)} contract port(s) disagree with the "
                                        "manifests", capped(violations))
    if errors:
        return Finding(evidence="INDETERMINATE: a service contract could not be parsed",
                       details=capped(errors), indeterminate=True)
    catalog_note = (f" and {len(catalog_ports)} catalog canonical_port(s) agree"
                    if catalog_ports else
                    "; the core-docs catalog was not in the workspace, so canonical_port "
                    "was not compared")
    return Finding(evidence=f"{checked} service contract(s) declare exactly what their "
                            f"Kubernetes Service publishes{catalog_note}")


def _off_constant(service: Service) -> list[str]:
    """Every port of this service that is not the §4.1 constant for its own concern.

    PER CONCERN, not "is it one of the three numbers". The set test accepts a Service
    publishing http=9464, which is three quarters of a migration and a metrics endpoint served
    where callers expect an API. It also accepts a Service on the constants whose overlays are
    still on the old numbers, which is the half-migrated state RFC-0007 §11 says takes a service
    down - so containerPorts, probes and the port environment values are read here too. SP-0001
    catches that disagreement from the other direction; both matter, because SP-0001 is what
    holds during the migration and this is what says the migration finished.
    """
    off = []
    for name, port in sorted(service.published.items()):
        want = CONSTANTS.get(name)
        if want is None:
            off.append(f"publishes an unrecognised port name {name!r}={port}")
        elif port != want:
            off.append(f"publishes {name}={port}, not {want}")
    for name, port in sorted(service.target_ports.items()):
        want = CONSTANTS.get(name)
        if want is not None and port != want:
            off.append(f"routes {name} to targetPort {port}, not {want}")
    for env, facts in sorted(service.overlays.items()):
        for port in sorted(facts["container_ports"] - set(CONSTANTS.values())):
            off.append(f"{env} binds containerPort {port}")
        for port in sorted(facts["probe_ports"] - set(CONSTANTS.values())):
            off.append(f"{env} probes {port}")
        for var, port in sorted(facts["env_ports"].items()):
            want = CONSTANTS[PORT_ENV_CONCERN[var]]
            if port != want:
                off.append(f"{env} sets {var}={port}, not {want}")
    return off


def k8s_ports_are_constants(ws: Workspace) -> Finding:
    """SP-0006. Enforced since the fleet migration; it now catches a service moving back off."""
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations, on_constants, total = [], 0, 0
    for service in ws.services.values():
        if service.name not in ws.registry or not service.published:
            continue
        total += 1
        off = _off_constant(service)
        if off:
            violations.append(f"{service.name}: {'; '.join(off[:4])}"
                              + (f" (+{len(off) - 4} more)" if len(off) > 4 else ""))
        else:
            on_constants += 1
    if violations:
        return Finding(len(violations),
                       f"{on_constants} of {total} addressable service(s) are on the §4.1 "
                       f"constants; {len(violations)} off them", capped(violations))
    return Finding(evidence=f"all {total} addressable service(s) are on the §4.1 constants, "
                            "in every place their ports are written")


def pprof_is_the_constant_and_stays_private(ws: Workspace) -> Finding:
    """SP-0008. The fourth listener in RFC-0007 §4.1, and the one nothing was checking.

    Two halves, because pprof is the one port whose correct state is "declared but not
    reachable". §4.1 fixes it at 6060; §9 requires it to bind 127.0.0.1 and stay disabled
    outside local development. So a service may name it and must not publish it, and the
    interesting failure is not a wrong number -- it is a debug endpoint that acquires a
    containerPort and becomes reachable from inside the cluster.
    """
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations, declared = [], 0
    for service in ws.services.values():
        for name, port in sorted(service.published.items()):
            if port == PPROF_CONSTANT or "pprof" in name.lower():
                violations.append(f"{service.name}: the Service publishes {name}={port}; "
                                  "RFC-0007 §9 keeps pprof on 127.0.0.1 and off the network")
        for env, facts in sorted(service.overlays.items()):
            for port in sorted(facts["pprof_ports"]):
                declared += 1
                if port != PPROF_CONSTANT:
                    violations.append(f"{service.name}/{env}: PPROF_PORT={port}, not the "
                                      f"RFC-0007 §4.1 constant {PPROF_CONSTANT} "
                                      f"({facts['path']})")
            if PPROF_CONSTANT in facts["container_ports"]:
                violations.append(f"{service.name}/{env}: binds containerPort "
                                  f"{PPROF_CONSTANT}, which exposes pprof to the cluster "
                                  f"({facts['path']})")
    if violations:
        return Finding(len(violations), f"{len(violations)} pprof port declaration(s) that "
                                        "§4.1 or §9 forbids", capped(violations))
    return Finding(evidence=f"{declared} PPROF_PORT declaration(s) are all {PPROF_CONSTANT} and "
                            "no service publishes or binds it")


def local_allocation_is_per_service(ws: Workspace) -> Finding:
    """SP-0009. The local carve-out is still a carve-out.

    RFC-0007 §4.2 keeps a per-service port for the laptop, where all services share one network
    namespace and the second to want 8080 cannot have it. That only works while the local table
    is a DIFFERENT table from the cluster one: until 2026-08-21 both branches of
    ServiceRegistry.Resolve read one map generated from the manifests, so the first service to
    publish 8080 in Kubernetes would have become 8080 locally too, and §4.2's whole purpose
    would have been defeated by the mechanism meant to deliver it.

    Two assertions, and they are the two ways that can come back. The local table must exist and
    hold every service the cluster table holds -- a missing entry resolves to localhost:0, which
    dials and reports a refused connection rather than the missing allocation. And no two
    services may share a local port, which is the collision guard RFC-0007 §12 records as never
    having been built. Both go red immediately if the local table is ever regenerated from the
    manifests again, because the manifests converge on one number and this table must not.
    """
    if ws.local_registry_error:
        return Finding(evidence=f"INDETERMINATE: {ws.local_registry_error}", indeterminate=True)
    if not ws.registry:
        return Finding(evidence="INDETERMINATE: the cluster port table could not be read, so "
                                "the local allocation has nothing to be complete against",
                       indeterminate=True)
    violations = []
    for name in sorted(ws.registry):
        if name not in ws.local_registry:
            violations.append(f"{name} is in the cluster table with no local-development "
                              "allocation, so it resolves to localhost:0 on a workstation")
    for index, concern in ((0, "http"), (1, "grpc")):
        seen: dict[int, str] = {}
        for name in sorted(ws.local_registry):
            port = ws.local_registry[name][index]
            if not port:
                continue
            if port in seen:
                violations.append(f"local {concern} port {port} is allocated to both "
                                  f"{seen[port]} and {name}; on one host the second to start "
                                  "cannot bind it")
            seen[port] = name
    if violations:
        return Finding(len(violations), f"{len(violations)} defect(s) in the local-development "
                                        "allocation", capped(violations))
    return Finding(evidence=f"{len(ws.local_registry)} services hold a distinct local HTTP and "
                            "gRPC port, independent of what Kubernetes publishes")


def metrics_are_scraped_by_port_name(ws: Workspace) -> Finding:
    """SP-0010. Metrics continuity across the migration, which is free only while it is checked.

    RFC-0007 §9: every ServiceMonitor selects its endpoint by port NAME. That is what makes a
    service's move to 9464 invisible to Prometheus, and §9 states plainly that keeping it that
    way is a requirement rather than an observation - a ServiceMonitor naming a number would be
    one more copy of the port, and it would break silently, because a failed scrape looks like a
    service with no metrics rather than like a misconfiguration.
    """
    if not ws.services:
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    violations, checked = [], 0
    for service in ws.services.values():
        if not service.monitor_ports:
            continue
        checked += 1
        for port in service.monitor_ports:
            if isinstance(port, bool) or not isinstance(port, int):
                continue
            violations.append(f"{service.name}: {service.monitor_path} scrapes port {port} by "
                              "number; RFC-0007 §9 requires the port name, so that a service "
                              "moving to 9464 does not silently stop being scraped")
    if violations:
        return Finding(len(violations), f"{len(violations)} ServiceMonitor endpoint(s) naming a "
                                       "number", capped(violations))
    if not checked:
        return Finding(evidence="INDETERMINATE: no ServiceMonitor found in the workspace",
                       indeterminate=True)
    return Finding(evidence=f"all {checked} ServiceMonitor(s) select their endpoint by port "
                            "name, so metrics survive a port change unchanged")


def manifest_address_vars(ws: Workspace) -> Finding:
    """SP-0007. Advisory cross-check. Authoritative gates named in the catalog."""
    services_dir = ws.infra / "services"
    if not services_dir.is_dir():
        return Finding(evidence="INDETERMINATE: no infra services tree in the workspace",
                       indeterminate=True)
    per_service: dict[str, int] = {}
    for path in sorted(services_dir.rglob("*.yaml")):
        service = path.relative_to(services_dir).parts[0]
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ADDRESS_VAR.finditer(text):
            var = match.group(1)
            if var in EXEMPT_ADDRESS_VARS or NOT_AN_ADDRESS.match(var):
                continue
            per_service[service] = per_service.get(service, 0) + 1
    total = sum(per_service.values())
    if total:
        worst = sorted(per_service.items(), key=lambda kv: (-kv[1], kv[0]))
        return Finding(total, f"{total} manifest entries across {len(per_service)} services "
                              "still supply an upstream address (authoritative gate: "
                              "inboxxhq-infra scripts/check-service-addressing.py)",
                       capped([f"{name}: {count}" for name, count in worst]))
    return Finding(evidence="no manifest supplies an upstream address")


# The first platform-shared-go release whose registry answers cluster resolution with the
# RFC-0007 §4.1 constants instead of a generated per-service port.
CONSTANTS_REGISTRY_MIN = (1, 48, 0)

SHARED_GO_REQUIRE = re.compile(
    r"^\s*(?:require\s+)?github\.com/coderaxis/platform-shared-go\s+v(\d+)\.(\d+)\.(\d+)",
    re.MULTILINE)


def _pinned_shared_go(gomod: Path) -> tuple[int, int, int] | None:
    match = SHARED_GO_REQUIRE.search(gomod.read_text(encoding="utf-8", errors="replace"))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def _resolves_by_name(repo: Path) -> bool:
    """Whether this repo asks the registry where a service lives, as opposed to being told."""
    for path in repo.rglob("*.go"):
        if path.name.endswith("_test.go") or "vendor" in path.parts:
            continue
        try:
            if "servicediscovery." in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def resolvers_pin_a_constants_registry(ws: Workspace) -> Finding:
    """SP-0011. The drift no manifest can show you.

    Every other control here reads a number someone wrote down, so it can compare two
    written things. This one is about a number nobody wrote: a service that resolves by
    name has no address in its manifest, its Dockerfile or its contract, and gets one at
    runtime from whichever registry snapshot its go.mod pinned. So the port it actually
    dials is a property of a dependency version, and a stale pin is invisible to every
    check that reads configuration -- including all nine above.

    That is not a theoretical gap. At the fleet migration edge-gateway was nine minor
    versions back, on a tag whose registry still held the pre-migration allocation. The
    manifests were correct, SP-0001 through SP-0010 were green, and the gateway would have
    resolved auth-service to :4002 and voice-gateway to :4020 -- ports no Service publishes
    any more. Every upstream unreachable, from a repository in which nothing was wrong.

    It was caught by the pre-push guard building the way CI does rather than by any port
    control, and only because a gitignored go.work had been masking it locally: the local
    suite compiled against the working copy of the library and passed, while the image is
    built from the pin. A green local run says nothing here, which is precisely why this
    belongs in CI.
    """
    if not ws.repos_by_service:
        return Finding(evidence="INDETERMINATE: no service repositories in the workspace",
                       indeterminate=True)
    minimum = "v" + ".".join(map(str, CONSTANTS_REGISTRY_MIN))
    violations, resolvers = [], 0
    for name, repo in sorted(ws.repos_by_service.items()):
        gomod = repo / "go.mod"
        if not gomod.is_file() or not _resolves_by_name(repo):
            continue
        resolvers += 1
        pinned = _pinned_shared_go(gomod)
        if pinned is None:
            violations.append(f"{name}: resolves services by name but pins no "
                              "platform-shared-go, so it has no registry to resolve against")
        elif pinned < CONSTANTS_REGISTRY_MIN:
            violations.append(
                f"{name}: pins platform-shared-go v{'.'.join(map(str, pinned))}, whose registry "
                f"answers with pre-migration per-service ports that no Service publishes; "
                f"needs {minimum} or later")
    if violations:
        return Finding(len(violations),
                       f"{resolvers - len(violations)} of {resolvers} name-resolving service(s) "
                       f"pin a registry that answers with the §4.1 constants",
                       capped(violations))
    return Finding(evidence=f"all {resolvers} name-resolving service(s) pin {minimum} or later, "
                            "so they resolve to the §4.1 constants")


DETECTORS = {
    "k8s_ports_internally_consistent": k8s_ports_internally_consistent,
    "k8s_ports_match_registry": k8s_ports_match_registry,
    "no_reserved_ports": no_reserved_ports,
    "dockerfile_ports_are_own": dockerfile_ports_are_own,
    "contract_ports_match_service": contract_ports_match_service,
    "k8s_ports_are_constants": k8s_ports_are_constants,
    "manifest_address_vars": manifest_address_vars,
    "pprof_is_the_constant_and_stays_private": pprof_is_the_constant_and_stays_private,
    "local_allocation_is_per_service": local_allocation_is_per_service,
    "metrics_are_scraped_by_port_name": metrics_are_scraped_by_port_name,
    "resolvers_pin_a_constants_registry": resolvers_pin_a_constants_registry,
}


# --- catalog, evaluation, reporting -------------------------------------------------------

def load_controls(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"::error::cannot read control catalog {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: expected a controls list")
    errors, seen = [], set()
    for index, control in enumerate(doc["controls"]):
        cid = control.get("id", f"#{index}")
        missing = [key for key in REQUIRED_FIELDS if not control.get(key)]
        if missing:
            errors.append(f"{cid}: missing required field(s) {missing}")
        if control.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity {control.get('severity')!r} not in "
                          f"{sorted(VALID_SEVERITY)}")
        if control.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status {control.get('status')!r} not in {sorted(VALID_STATUS)}")
        if control.get("scope") not in VALID_SCOPE:
            errors.append(f"{cid}: scope {control.get('scope')!r} not in {sorted(VALID_SCOPE)}")
        if control.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {control.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for error in errors:
            print(f"::error::service-ports catalog invalid: {error}")
        raise SystemExit(2)
    return doc


def load_baseline() -> tuple[dict[str, int], str]:
    if not BASELINE_PATH.is_file():
        return {}, ""
    try:
        doc = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        counts = doc.get("controls", {}) if isinstance(doc, dict) else None
        if not isinstance(counts, dict):
            raise ValueError("'controls' is not an object")
        return {str(k): int(v) for k, v in counts.items()}, ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def evaluate(ws: Workspace, controls: list[dict], baseline: dict[str, int]) -> list[dict]:
    results = []
    for control in controls:
        cid = control["id"]
        rec = {
            "control": cid, "title": control["title"], "severity": control["severity"],
            "owner": control["owner"], "status": control["status"], "result": "skipped",
            "evidence": "", "details": [], "count": 0, "remediation": "",
            "baseline": int(baseline.get(cid, 0)),
        }
        if control["status"] in ("deprecated", "superseded"):
            rec["evidence"] = f"lifecycle status={control['status']} (not evaluated)"
            results.append(rec)
            continue
        finding = DETECTORS[control["detector"]](ws)
        rec.update(evidence=finding.evidence, details=finding.details, count=finding.count)
        if control["status"] == "deferred":
            # Measured and printed, never gated. The number is the migration's remaining work.
            rec["result"] = "deferred"
        elif finding.indeterminate:
            rec["result"] = "indeterminate"
        elif finding.count > rec["baseline"]:
            rec["result"] = "fail"
            rec["remediation"] = " ".join(str(control["remediation"]).split())
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


MARK = {"pass": "ok", "fail": "XX", "frozen": "==", "improved": "->", "skipped": "--",
        "indeterminate": "??", "deferred": ".."}


def build_report(ws: Workspace, results: list[dict], doc: dict, fail_on: str,
                 threshold: int) -> dict:
    return {
        "workspace": str(ws.root),
        "policy_ssot": doc.get("policy_ssot", []),
        "fail_on": fail_on,
        "services_seen": len(ws.services),
        "repositories_seen": len(ws.repos_by_service),
        "registry_entries": len(ws.registry),
        "controls_total": len(results),
        "passed": sum(r["result"] == "pass" for r in results),
        "frozen": sum(r["result"] == "frozen" for r in results),
        "improved": sum(r["result"] == "improved" for r in results),
        "failed": sum(r["result"] == "fail" for r in results),
        "deferred": sum(r["result"] == "deferred" for r in results),
        "indeterminate": sum(r["result"] == "indeterminate" for r in results),
        "skipped": sum(r["result"] == "skipped" for r in results),
        "enforced_failures": sum(enforced(r, threshold) for r in results),
        "open_violations": {r["control"]: r["count"] for r in results if r["count"]},
        "ok": not any(enforced(r, threshold) for r in results),
        "results": results,
    }


def render_text(report: dict, threshold: int) -> None:
    results = report["results"]
    print(f"::group::service-ports controls ({report['services_seen']} services, "
          f"{report['repositories_seen']} repositories, "
          f"{report['registry_entries']} registry entries)")
    for rec in results:
        suffix = (f" (baseline {rec['baseline']}, now {rec['count']})"
                  if rec["result"] in ("frozen", "improved") else "")
        print(f"  [{MARK[rec['result']]}] {rec['control']} [{rec['severity']}/{rec['owner']}] "
              f"{rec['title']}: {rec['evidence']}{suffix}")
    print("::endgroup::")

    for rec in results:
        if rec["result"] == "fail":
            level = "error" if enforced(rec, threshold) else "warning"
            tail = f" | fix: {rec['remediation']}" if rec["remediation"] else ""
            print(f"::{level}::[{rec['control']}][{rec['severity']}] {rec['title']} - "
                  f"{rec['evidence']}{tail}")
            for detail in rec["details"]:
                print(f"    - {detail}")
        elif rec["result"] == "improved":
            print(f"::notice::[{rec['control']}] improved from {rec['baseline']} to "
                  f"{rec['count']} - run --write-baseline to lock the gain in")
        elif rec["result"] == "deferred":
            print(f"::notice::[{rec['control']}] deferred: {rec['evidence']}")
            for detail in rec["details"][:5]:
                print(f"    - {detail}")
        elif rec["result"] == "indeterminate":
            print(f"::notice::[{rec['control']}] {rec['evidence']}")

    ssot = ", ".join(report["policy_ssot"])
    if report["ok"]:
        print(f"service-ports: OK - {report['passed']} upheld, {report['frozen']} frozen, "
              f"{report['deferred']} deferred to the migration playbook.")
    else:
        print(f"service-ports: FAILED - {report['enforced_failures']} enforced violation(s), "
              f"{report['passed']} controls upheld.")
    print(f"Policy SSOT: {ssot} (fail-on={report['fail_on']}).")


def write_baseline(results: list[dict]) -> None:
    # Deferred controls are excluded on purpose: SP-0006's count is the migration's remaining
    # work, not debt to be frozen, and freezing it would let the fleet drift AWAY from §4.1 by
    # one service without anything objecting.
    counts = {r["control"]: r["count"] for r in results
              if r["count"] and r["result"] != "deferred"}
    existing = {}
    if BASELINE_PATH.is_file():
        try:
            doc = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                existing = {k: v for k, v in doc.items()
                            if k not in ("_comment", "controls", "frozen_on")}
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
    if not counts:
        if BASELINE_PATH.is_file():
            BASELINE_PATH.unlink()
            print(f"service-ports: clean - removed {BASELINE_PATH.name}")
        else:
            print("service-ports: clean - no baseline needed")
        return
    # A control that reached zero leaves `controls`, and its reason has to leave
    # with it. Carrying it forward leaves the file asserting that findings which
    # were just fixed are still frozen -- which is how SP-0005 came to describe
    # three contract defects for a while after they were corrected.
    reasons = existing.get("reasons")
    if isinstance(reasons, dict):
        existing = {**existing,
                    "reasons": {k: v for k, v in reasons.items() if k in counts}}

    from datetime import date
    doc = {"_comment": BASELINE_COMMENT, "frozen_on": date.today().isoformat(),
           **existing, "controls": counts}
    # ensure_ascii=False because the file is written and read as UTF-8, so
    # escaping turns the section signs this file is full of into \u00a7 and
    # makes every reason harder to read for no gain.
    BASELINE_PATH.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"service-ports: wrote {BASELINE_PATH} ({sum(counts.values())} violation(s) frozen)")


def render_docs(doc: dict) -> str:
    lines = [
        DOCS_BEGIN, "",
        "_Generated from `controls/service-ports.yaml` by `scripts/check-service-ports.py "
        "--write-docs` — do not edit by hand._", "",
        "| Control | Policy | Severity | Owner | Status |",
        "| ------- | ------ | -------- | ----- | ------ |",
    ]
    for control in doc["controls"]:
        policy = " ".join(str(control["policy"]).split())
        lines.append(f"| {control['id']} | {policy} | {control['severity']} | "
                     f"{control['owner']} | {control['status']} |")
    lines += ["", DOCS_END]
    return "\n".join(lines)


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        print(f"::error::{path}: markers not found. Add these two lines where the table "
              f"should go:\n  {DOCS_BEGIN}\n  {DOCS_END}")
        return 1
    new = text.split(DOCS_BEGIN, 1)[0] + render_docs(doc) + text.split(DOCS_END, 1)[1]
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"service-ports: wrote generated control table into {path}")
    else:
        print(f"service-ports: {path} control table already up to date")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        print(f"::error::{path}: generated-controls markers not found; run --write-docs")
        return 1
    current = DOCS_BEGIN + text.split(DOCS_BEGIN, 1)[1].split(DOCS_END, 1)[0] + DOCS_END
    if current.strip() != render_docs(doc).strip():
        print(f"::error::{path}: control table is out of sync with the catalog; run: "
              "python3 scripts/check-service-ports.py --write-docs " + str(path))
        return 1
    print(f"service-ports: {path} control table is in sync with the catalog")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="RFC-0007 §10 port guard.")
    parser.add_argument("--workspace", default=".",
                        help="assembled multi-repo workspace root")
    parser.add_argument("--controls", default=str(DEFAULT_CONTROLS),
                        help="control catalog YAML")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    parser.add_argument("--report", help="write the JSON report to this path")
    parser.add_argument("--write-baseline", action="store_true",
                        help=f"freeze current violation counts into {BASELINE_PATH.name}")
    parser.add_argument("--write-docs", metavar="FILE",
                        help="regenerate the control table in FILE and exit")
    parser.add_argument("--verify-docs", metavar="FILE",
                        help="fail if FILE's control table drifted; then exit")
    args = parser.parse_args(argv)

    controls_path = Path(args.controls)
    if not controls_path.is_file():
        print(f"::error::service-ports: control catalog not found: {controls_path}")
        return 2
    doc = load_controls(controls_path)

    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_docs(doc))
        return 0

    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"::error::service-ports: not a directory: {root}")
        return 2

    baseline, baseline_error = load_baseline()
    if baseline_error and not args.write_baseline:
        print(f"::error::{BASELINE_PATH} is unreadable: {baseline_error}")
        return 2

    ws = Workspace(root)
    # An empty workspace must not report clean. Every detector below would return
    # INDETERMINATE, which reads as "nothing to see" in a summary line, and a checker that
    # exits 0 against a tree it was never given is the failure mode fleet-governance.yaml was
    # written to close.
    if not ws.services and not ws.repos_by_service:
        print(f"::error::service-ports: no services found under {root}. Expected "
              "infra/inboxxhq-infra/services and/or services/*/*; the workspace did not "
              "assemble, so no control could be evaluated.")
        return 2

    results = evaluate(ws, doc["controls"], baseline)
    if args.write_baseline:
        write_baseline(results)
        return 0

    threshold = SEVERITY_ORDER[args.fail_on]
    report = build_report(ws, results, doc, args.fail_on, threshold)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        render_text(report, threshold)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
