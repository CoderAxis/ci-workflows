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

This runs identically from service-ci.yml and from deploy-reusable.yml. Because a reusable
workflow sees the ORIGINAL caller in GITHUB_REPOSITORY_ID, calling the delivery workflow
directly gains an attacker nothing: it derives the caller's own identity either way.
"""

from __future__ import annotations

import argparse
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
        # deployable_repository first: it is the repo CI actually runs in. `repository` is the
        # sibling core module, which shares the service's standards.
        for block in (entry.get("deployable_repository") or {}, entry.get("repository") or {}):
            gid = block.get("github_id")
            if gid:
                by_id.setdefault(int(gid), entry)
                expected.setdefault(int(gid), block.get("name"))
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
    want = expected.get(repo_id)
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
    # A core module is a library: linted and tested, never built into an image.
    deployable = (want or repo).endswith(DEPLOYABLE_SUFFIXES)

    return {
        "service": str(entry.get("name") or repo),
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
