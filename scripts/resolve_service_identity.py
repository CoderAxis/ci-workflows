#!/usr/bin/env python3
"""Resolve the calling repository to its service identity, using only platform-owned data.

WHY THIS EXISTS
---------------
Every fact that decides which CI gates run used to be supplied by the repository being
gated. 42 repositories passed their own `service_name` to the delivery workflow, 60 passed
their own messaging `role`, and each passed its own Dockerfile capability list. A repository
that names itself differently publishes under a different identity; one that declares
`role: E` ("no events") skips event compliance entirely. Self-declared identity is not
identity, it is a request.

So nothing here is read from the caller. Identity is derived from two things:

  1. GITHUB_REPOSITORY_ID - assigned by GitHub, immutable across renames, never reused, and
     not settable from inside the repository.
  2. The service catalog and standards in core-docs, owned by the platform team.

WHY THE ID AND NOT THE NAME
---------------------------
Renaming inboxxhq-auth-service to inboxxhq-order-service makes GITHUB_REPOSITORY report
order-service. A name-keyed resolver would then build auth's code, publish it as
order-service, and pin that digest into order-service's GitOps overlay - destroying a
service nobody touched. Keyed on the id, the renamed repo still resolves to auth-service,
and the name mismatch is reported as catalog drift instead of silently obeyed.

This runs identically from service-ci.yaml and from deploy-reusable.yaml. Because a reusable
workflow sees the ORIGINAL caller in GITHUB_REPOSITORY_ID, calling the delivery workflow
directly gains an attacker nothing: it derives the caller's own identity either way.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import yaml

# Coverage is a property of how critical the service is, not a number the service picks.
# The previous per-repo CI set COVERAGE_THRESHOLD to 0, so its gate always passed.
TIER_COVERAGE = {"tier-0": "70", "tier-1": "60", "tier-2": "50"}
DEFAULT_COVERAGE = "50"

DEPLOYABLE_SUFFIXES = ("-service", "-gateway", "-bff")


class Unresolved(SystemExit):
    """Fail closed with an actionable message. An unresolved repo gets no pipeline."""

    def __init__(self, message: str) -> None:
        super().__init__(f"::error::{message}")


def load_bindings(catalog_root: pathlib.Path) -> tuple[dict, dict]:
    """Map immutable repository id -> (catalog entry, the name the catalog expects)."""
    services = catalog_root / "catalog" / "services"
    if not services.is_dir():
        raise Unresolved(f"catalog not found at {services}; cannot resolve identity")

    by_id: dict[int, dict] = {}
    expected: dict[int, str] = {}
    for path in sorted(services.glob("*.yaml")):
        entry = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # A service is spread across up to three repositories and CI runs in all of them:
        # the deployable, the core Go module, and the core-postgres schema module. All three
        # must be bound, or the unbound ones resolve to nothing and silently get no pipeline -
        # which is exactly what happened to all 30 *-core-postgres repos before this.
        for kind, key in (("deployable", "deployable_repository"),
                          ("core", "repository"),
                          ("schema", "schema_repository")):
            block = entry.get(key) or {}
            gid = block.get("github_id")
            if gid:
                by_id.setdefault(int(gid), entry)
                # The KIND is recorded alongside the name because these three repos are three
                # different shapes, not one shape with optional parts. A deployable ships an
                # image; a core module is a Go library; a schema module owns sql/ + sqlc.yaml and
                # has no Makefile at all. Gating them on one requirement set fails 27 of them on
                # absences that are correct.
                expected.setdefault(int(gid), (block.get("name"), kind))

    # Shared libraries are not services and have no service entry, so before the artifact catalog
    # existed they resolved to nothing: platform-shared-go, which 85 repositories pin, could not
    # run central CI on its own release because the resolver did not know what it was. They bind
    # the same way, by immutable id, and are marked with their own kind so the gates that assume
    # a service (coverage tier, Dockerfile capabilities, deploy) do not fire on a library.
    artifacts = catalog_root / "catalog" / "artifacts"
    for path in sorted(artifacts.glob("*.yaml")) if artifacts.is_dir() else []:
        entry = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        block = entry.get("repository") or {}
        gid = block.get("github_id")
        if gid:
            by_id.setdefault(int(gid), entry)
            expected.setdefault(int(gid), (block.get("name"), "shared-library"))
    return by_id, expected


def resolve_role(entry: dict, *, has_events: bool, owns_db: bool) -> str:
    """P = owns Postgres, outbox-only. DK = no Postgres, direct Kafka. E = no events.

    H / Hybrid / Bridge are genuine exceptions. They must be stated by the CATALOG
    (messaging.role) so the exception is a reviewed platform decision - never inferred here,
    and never accepted from the repository.
    """
    declared = (entry.get("messaging") or {}).get("role")
    if declared:
        return str(declared)
    if not has_events:
        return "E"
    return "P" if owns_db else "DK"


def resolve_allowed_topics(entry: dict) -> str:
    """Topics a DK/Hybrid repo's direct producer may publish to, comma-separated.

    Read from the same catalog block as the role, because the two are one decision: a role
    that sanctions a direct producer is meaningless without saying what it may produce, and
    the compliance gate refuses a DK/Hybrid repo that declares no allowlist.

    Nothing supplied this before. The gate has always accepted an allowed_topics input and
    the catalog has always been able to declare Hybrid, but no code carried one to the
    other - so declaring Hybrid bought a repo a different failure rather than a pass, and
    the exception the pattern document describes could not actually be taken.
    """
    topics = (entry.get("messaging") or {}).get("allowedTopics") or []
    if isinstance(topics, str):
        topics = [topics]
    return ",".join(str(t).strip() for t in topics if str(t).strip())


def resolve_capabilities(catalog_root: pathlib.Path, repo_name: str, *, deployable: bool) -> list[str]:
    """Read the platform's capability matrix. Keyed on the catalog's name, not GitHub's."""
    matrix_path = (
        catalog_root / "standards" / "infrastructure" / "dockerfile-capability-matrix.yaml"
    )
    caps: list[str] = []
    if matrix_path.exists():
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
        for row in matrix.get("services") or []:
            if row.get("repo") == repo_name:
                caps = list(row.get("capabilities") or [])
                break
    if deployable and not caps:
        raise Unresolved(
            f"{repo_name} is deployable but is absent from "
            "standards/infrastructure/dockerfile-capability-matrix.yaml. Capabilities decide "
            "which Dockerfile mandates apply, so an empty set silently waives all of them."
        )
    return caps


def _repo_ref(block: dict) -> str:
    """owner/name for a catalog repository block, or "" when the block is absent."""
    org = (block or {}).get("organization")
    name = (block or {}).get("name")
    return f"{org}/{name}" if org and name else ""


def load_artifact_graph(catalog_root: pathlib.Path) -> dict:
    """The platform artifact graph, or an empty graph when it has not been published yet."""
    path = catalog_root / "generated" / "artifact-graph" / "artifact-graph.generated.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def graph_pins(graph: dict, repo_name: str) -> set[str] | None:
    """What this repository actually pins, per the generated artifact graph.

    None means the graph cannot answer - it has not been published, or it does not model
    this repository - so a caller can fall back rather than read silence as "pins nothing".
    """
    if repo_name not in (graph.get("nodes") or {}):
        return None
    return {e["to"] for e in graph.get("edges") or [] if e.get("from") == repo_name}


def resolve_release_targets(entry: dict, repo_kind: str, repo_name: str,
                            graph: dict) -> dict[str, str]:
    """Derive the module path and every repository a release must open a bump PR against.

    This used to read one service entry and walk that family's three repositories:

        core  <-  core-postgres  <-  deployable

    which is correct for a family and blind to everything shared between families. Two modules
    fall outside any single family and they are the two most depended-upon artifacts on the
    platform: platform-shared-go with 85 direct consumers and platform-contracts-go with 49.
    Neither could be expressed, so neither was cascaded - platform-shared-go was propagated by a
    315-line workflow hand-copied into 70 repositories (which reached 67 of its 85 consumers,
    missing every *-core-postgres adapter), and platform-contracts-go was not propagated at all,
    because no workflow anywhere subscribed to its release.

    Reading the artifact graph instead answers the same question for every artifact kind with no
    special case: who directly requires this module, according to the go.mod files that the
    compiler actually reads. The family walk is preserved exactly - it is what the graph contains
    for a core - and the shared libraries stop being exceptions.

    Consumers carry their release level so the caller can bump in dependency order. A consumer
    the graph cannot order (it sits in a cycle) is withheld rather than guessed at: two artifacts
    that require each other have no first, and bumping one of them arbitrarily writes a version
    that the other cannot yet satisfy.
    """
    nodes = graph.get("nodes") or {}
    node = nodes.get(repo_name)
    if node is None or not node.get("module_path"):
        # A deployable is an image, not a published module: nothing can require it, so it has no
        # consumers by construction rather than by omission.
        return {"module_path": "", "consumers": "[]", "unorderable_consumers": ""}

    level_of = {name: i for i, level in enumerate(graph.get("release_order") or [])
                for name in level}
    unorderable = set(graph.get("unorderable") or [])

    # What each consumer pins directly, read off its own outgoing edges. The pin policy gate runs
    # against the consumer, so it needs the consumer's module set and not the releasing repo's.
    # Deriving it from the same graph edges keeps the gate honest: the modules it enforces are
    # exactly the ones the consumer's go.mod actually requires.
    requires: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        target_node = nodes.get(edge["to"]) or {}
        if target_node.get("module_path"):
            requires.setdefault(edge["from"], []).append(target_node["module_path"])

    consumers, withheld = [], []
    for name in graph.get("consumers", {}).get(repo_name, []):
        target = nodes.get(name)
        if target is None:
            continue
        if name in unorderable or repo_name in unorderable:
            withheld.append(name)
            continue
        repo = target.get("repository") or {}
        ref = f"{repo.get('organization')}/{repo.get('name')}"
        consumers.append({"repository": ref, "kind": target.get("type"),
                          "name": name, "level": level_of.get(name, 0),
                          "requires": sorted(requires.get(name, []))})

    consumers.sort(key=lambda c: (c["level"], c["repository"]))
    return {
        "module_path": node["module_path"],
        "consumers": json.dumps(consumers, separators=(",", ":")),
        "unorderable_consumers": ",".join(sorted(withheld)),
    }


def resolve_required_modules(entry: dict, repo_kind: str) -> str:
    """The platform modules this repo must pin DIRECTLY, as a JSON array.

    Reading up the same DAG the fan-out reads down. A deployable pins both the core and the
    schema adapter - the "diamond" - so both must be present, direct, replace-free and, on a
    release branch, at a stable tag. An adapter pins only the core.

    The per-repo check_core_module_versions.sh hardcoded its two module paths, so the policy
    was correct only for the one repo it was copied into, and a service that pinned a core
    it forgot to list was simply not checked. Derived from the catalog, the list cannot fall
    out of step with what the service actually is.
    """
    core = _repo_ref(entry.get("repository") or {})
    schema = _repo_ref(entry.get("schema_repository") or {})

    if repo_kind == "deployable":
        refs = [r for r in (core, schema) if r]
    elif repo_kind == "schema":
        refs = [r for r in (core,) if r]
    else:
        refs = []  # a core sits at the bottom of the DAG and pins no platform module

    return json.dumps([f"github.com/{r}" for r in refs], separators=(",", ":"))


def resolve(catalog_root: pathlib.Path, repo_full: str, repo_id: int,
            coverage_override: str = "") -> dict[str, str]:
    repo = repo_full.split("/", 1)[1] if "/" in repo_full else repo_full
    by_id, expected = load_bindings(catalog_root)

    entry = by_id.get(repo_id)
    if entry is None:
        raise Unresolved(
            f"repository id {repo_id} ({repo_full}) is not bound to any service in the catalog. "
            "CI is catalog-driven and fails closed: an unbound repository gets no pipeline "
            "rather than a permissive default. Add its github_id under catalog/services. A new "
            "repository cannot inherit another service's identity because it cannot inherit "
            "another repository's id."
        )

    # The name is never used to resolve, but a mismatch means the repo was renamed without the
    # catalog being updated. That is drift between model and reality, so it stops the build.
    want, repo_kind = expected.get(repo_id, (None, "unknown"))
    if want and want != repo:
        raise Unresolved(
            f"repository id {repo_id} is bound to {want!r} in the catalog but GitHub reports "
            f"{repo!r}. The repository was renamed without updating the catalog. The id binding "
            "is what stopped this build from publishing under the wrong service."
        )

    interfaces = entry.get("interfaces") or {}
    events = interfaces.get("events") or {}
    database = entry.get("database") or {}
    seeding = entry.get("seeding") or {}

    has_events = bool(events.get("publishes") or events.get("consumes"))
    owns_db = database.get("mode") == "owned"
    tier = str(entry.get("service_tier") or entry.get("tier") or "unknown")
    # Which catalog block bound this id is authoritative. The name suffix is only a fallback for
    # an entry that predates the kind-aware bindings.
    deployable = (repo_kind == "deployable"
                  if repo_kind != "unknown" else (want or repo).endswith(DEPLOYABLE_SUFFIXES))

    graph = load_artifact_graph(catalog_root)

    # A schema module that pins its core directly must not release ahead of it: tagging
    # against a core tag that was never published advertises a dependency consumers cannot
    # fetch. The release workflow gates on that pin, so it needs to know which module is
    # upstream of this one.
    #
    # Most schema modules do pin their core, and this was taken to mean all of them do -
    # the family shape (core <- schema <- deployable) was read as the dependency. Three do
    # not: stripe-adapter-core-postgres, twilio-adapter-core-postgres and
    # chat-sync-core-postgres depend on platform-shared-go alone, their cores having never
    # been adopted. For those the gate demanded a pin that does not exist and no release
    # could pass it, which is how a payment fix came to be blocked by a module nothing
    # imports.
    #
    # The claim being made here is factual - "this repository pins that module" - so it is
    # read from the artifact graph, which records what is actually pinned and is already
    # the source the fan-out walks in the other direction. The catalog shape remains the
    # fallback for a repository the graph does not model, because a gate that quietly
    # stops applying is worse than one that occasionally asks for a pin.
    core_ref = _repo_ref(entry.get("repository") or {})
    pins = graph_pins(graph, repo)
    pins_its_core = (
        (entry.get("repository") or {}).get("name") in pins
        if pins is not None
        else bool(core_ref)
    )
    upstream = f"github.com/{core_ref}" if repo_kind == "schema" and core_ref and pins_its_core else ""

    return {
        "service": str(entry.get("name") or repo),
        "repo_kind": repo_kind,
        "upstream_module": upstream,
        "required_modules": resolve_required_modules(entry, repo_kind),
        **resolve_release_targets(entry, repo_kind, repo, graph),
        "language": str((entry.get("runtime") or {}).get("language") or "unknown"),
        "archetype": str(entry.get("archetype") or "unresolved"),
        "tier": tier,
        "owner": str(entry.get("owner") or "unknown"),
        "deployable": str(deployable).lower(),
        "owns_database": str(owns_db).lower(),
        "seeds": str(bool(seeding.get("mechanism"))).lower(),
        "has_rest": str(bool(interfaces.get("rest"))).lower(),
        "has_grpc": str(bool(interfaces.get("grpc"))).lower(),
        "has_events": str(has_events).lower(),
        "coverage_threshold": coverage_override or TIER_COVERAGE.get(tier, DEFAULT_COVERAGE),
        "role": resolve_role(entry, has_events=has_events, owns_db=owns_db),
        "allowed_topics": resolve_allowed_topics(entry),
        "capabilities": ",".join(
            resolve_capabilities(catalog_root, want or repo, deployable=deployable)
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog-root", default=".catalog",
                    help="Checkout root of the catalog repo.")
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                    help="Where to write key=value outputs.")
    ap.add_argument("--assert-service", default="",
                    help="Fail unless the resolved service matches. Used to reject a "
                         "caller-supplied service_name that contradicts the catalog.")
    args = ap.parse_args()

    repo_full = os.environ.get("GITHUB_REPOSITORY")
    repo_id = os.environ.get("GITHUB_REPOSITORY_ID")
    if not repo_full or not repo_id:
        raise Unresolved("GITHUB_REPOSITORY and GITHUB_REPOSITORY_ID must both be set")

    facts = resolve(
        pathlib.Path(args.catalog_root),
        repo_full,
        int(repo_id),
        os.environ.get("COVERAGE_OVERRIDE", ""),
    )

    if args.assert_service and args.assert_service != facts["service"]:
        raise Unresolved(
            f"this workflow was passed service_name={args.assert_service!r}, but repository id "
            f"{repo_id} is bound to {facts['service']!r}. The input is ignored for identity and "
            "exists only until callers stop sending it - remove it rather than correcting it."
        )

    rendered = "\n".join(f"{k}={v}" for k, v in facts.items())
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    else:
        print(rendered)

    print(
        f"resolved {repo_full} (id {repo_id}) -> {facts['service']} "
        f"[{facts['archetype']}, {facts['language']}, {facts['tier']}, "
        f"role={facts['role']}, deployable={facts['deployable']}, "
        f"coverage>={facts['coverage_threshold']}%]",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
