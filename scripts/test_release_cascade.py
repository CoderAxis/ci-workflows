#!/usr/bin/env python3
"""Assert the release cascade derives the dependency DAG correctly in both directions.

WHY THIS EXISTS
---------------
The cascade replaced a fan-out matrix that was hand-copied into 65 core repositories and gated
by a per-repo variable. That arrangement failed silently in both directions at once: five
families had a core and its own adapter disagreeing on whether to notify anyone, and the pin
policy script named its modules literally, so a service pinning an unlisted core was never
checked while the gate still reported green.

Both failures were invisible because nothing asserted the shape of the graph. These tests do.
A derived fan-out that quietly returns an empty list is indistinguishable from a healthy one
in a green pipeline, so "notifies nobody" and "checks nothing" are the specific cases pinned
down here.

Run: python3 scripts/test_release_cascade.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from resolve_service_identity import resolve  # noqa: E402

HERE = pathlib.Path(__file__).parent
CATALOG = HERE / "testdata" / "release-cascade-catalog"
PINS = HERE / "testdata" / "module-pins"
CHECKER = HERE / "check_module_pins.py"

CORE_ID, SCHEMA_ID, DEPLOYABLE_ID, SHARED_ID = 900000001, 900000002, 900000003, 900000004
CORE_MOD = "github.com/coderaxis/fixture-core"
SCHEMA_MOD = "github.com/coderaxis/fixture-core-postgres"
SHARED_MOD = "github.com/coderaxis/fixture-shared"

failures: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual == expected:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}\n          expected: {expected!r}\n          actual:   {actual!r}")
        failures.append(name)


def facts(repo: str, repo_id: int) -> dict:
    return resolve(CATALOG, repo, repo_id)


def test_fan_out_downward() -> None:
    """Releasing a module must reach everything the artifact graph says depends on it."""
    print("\nfan-out (downward: who must be bumped)")

    core = facts("coderaxis/fixture-core", CORE_ID)
    check("core repo_kind", core["repo_kind"], "core")
    check("core module path", core["module_path"], CORE_MOD)
    # Both, because the deployable pins the core directly as well as through the adapter - the
    # "diamond". Reaching only the adapter would leave the service on an old core indefinitely.
    # Each consumer carries what IT pins, because the pin policy gate runs against the consumer.
    # Sending the releasing repo's module set instead would enforce the wrong policy on every
    # target, and would pass, because a set that names nothing has nothing to fail on.
    adapter_consumer = {
        "repository": "coderaxis/fixture-core-postgres", "kind": "schema",
        "name": "fixture-core-postgres", "level": 2,
        "requires": [CORE_MOD, SHARED_MOD],
    }
    deployable_consumer = {
        "repository": "InboxxHQ-CoderAxis/inboxxhq-fixture-service", "kind": "deployable",
        "name": "inboxxhq-fixture-service", "level": 3,
        "requires": [CORE_MOD, SCHEMA_MOD, SHARED_MOD],
    }
    check("core reaches adapter AND deployable",
          json.loads(core["consumers"]), [adapter_consumer, deployable_consumer])

    schema = facts("coderaxis/fixture-core-postgres", SCHEMA_ID)
    check("schema repo_kind", schema["repo_kind"], "schema")
    check("adapter reaches the deployable only",
          json.loads(schema["consumers"]), [deployable_consumer])

    # A deployable is an image pinned by digest, not a module anything can `go get`. If it ever
    # produced a module path, module-release.yaml would tag a repo no consumer can resolve.
    dep = facts("InboxxHQ-CoderAxis/inboxxhq-fixture-service", DEPLOYABLE_ID)
    check("deployable repo_kind", dep["repo_kind"], "deployable")
    check("deployable is not releasable as a module", dep["module_path"], "")
    check("deployable reaches nobody", json.loads(dep["consumers"]), [])


def test_shared_library_fan_out() -> None:
    """A library shared across families must cascade like any other artifact.

    This is the case the previous resolver could not express at all. It walked one service entry,
    so a module consumed by many families resolved to nothing and had to be propagated by a
    workflow hand-copied into each consumer - which is how platform-shared-go came to reach 67 of
    its 85 consumers and platform-contracts-go none of its 49.
    """
    print("\nfan-out (a shared library is not a special case)")

    shared = facts("coderaxis/fixture-shared", SHARED_ID)
    check("shared library resolves its own identity", shared["repo_kind"], "shared-library")
    check("shared library module path", shared["module_path"], SHARED_MOD)
    consumers = json.loads(shared["consumers"])
    check("reaches every consumer across families",
          [c["name"] for c in consumers],
          ["fixture-core", "fixture-core-postgres", "inboxxhq-fixture-service"])
    # Ordered, so a consumer is never asked to move to a version that does not exist yet.
    check("consumers are ordered by release level",
          [c["level"] for c in consumers], sorted(c["level"] for c in consumers))


def test_cycle_is_withheld() -> None:
    """Artifacts that require each other have no release order, so neither is bumped.

    Two real cycles exist on the platform today - compliance-core and notification-core each
    require their own postgres adapter, inverting the core <- adapter layering. Picking one to
    go first writes a version the other cannot satisfy, so the cascade must decline and say so
    rather than choose.
    """
    print("\ncycles (withheld, not guessed at)")

    shared = facts("coderaxis/fixture-shared", SHARED_ID)
    names = [c["name"] for c in json.loads(shared["consumers"])]
    check("a consumer inside a cycle is not bumped", "fixture-cycle-a" in names, False)
    check("and it is named rather than silently dropped",
          shared["unorderable_consumers"], "fixture-cycle-a")


def test_pins_upward() -> None:
    """Pin requirements must follow the same DAG read upward."""
    print("\npin requirements (upward: what must be pinned directly)")

    # The core sits at the bottom, so an empty list is the correct answer - not a lookup that
    # failed. The distinction matters: both produce a passing gate.
    check("core pins no platform module",
          json.loads(facts("coderaxis/fixture-core", CORE_ID)["required_modules"]), [])
    check("adapter pins the core",
          json.loads(facts("coderaxis/fixture-core-postgres", SCHEMA_ID)["required_modules"]),
          [CORE_MOD])
    check("deployable pins both sides of the diamond",
          json.loads(facts("InboxxHQ-CoderAxis/inboxxhq-fixture-service",
                           DEPLOYABLE_ID)["required_modules"]),
          [CORE_MOD, SCHEMA_MOD])

    # The release gate needs to know what it must not be published ahead of.
    check("adapter declares its upstream",
          facts("coderaxis/fixture-core-postgres", SCHEMA_ID)["upstream_module"], CORE_MOD)
    check("core has no upstream",
          facts("coderaxis/fixture-core", CORE_ID)["upstream_module"], "")


def run_checker(fixture: str, mode: str) -> int:
    return subprocess.run(
        [sys.executable, str(CHECKER),
         "--go-mod", str(PINS / fixture / "go.mod"),
         "--modules", json.dumps([CORE_MOD, SCHEMA_MOD]),
         "--mode", mode],
        capture_output=True, text=True,
    ).returncode


def test_pin_policy() -> None:
    """A gate that only ever passes proves nothing, so assert each rule actually blocks."""
    print("\npin policy (mutation tests)")
    check("compliant pins pass on a release branch", run_checker("compliant", "prod"), 0)
    check("pseudo-version blocks on a release branch", run_checker("pseudo-version", "prod"), 1)
    check("replace directive blocks", run_checker("replace-directive", "prod"), 1)
    check("indirect-only pin blocks", run_checker("indirect-only", "prod"), 1)
    # Advisory off a release branch, matching the posture of the script this replaced: in-flight
    # work on a feature branch is allowed to point at an untagged commit.
    check("pseudo-version is advisory off a release branch",
          run_checker("pseudo-version", "dev"), 0)

    # The governed sweep is the only thing covering platform-shared-go, which appears in no
    # catalog entry. If it regresses, 86 repositories quietly stop being checked while the
    # required-module assertions above all keep passing - so this case must be pinned separately.
    print("\ngoverned sweep (modules no catalog entry names)")
    check("unlisted platform module with a pseudo-version blocks",
          run_checker("governed-pseudo", "prod"), 1)
    check("...but is advisory off a release branch",
          run_checker("governed-pseudo", "dev"), 0)
    check("disabling the sweep lets it through, proving the sweep is what caught it",
          subprocess.run(
              [sys.executable, str(CHECKER),
               "--go-mod", str(PINS / "governed-pseudo" / "go.mod"),
               "--modules", json.dumps([CORE_MOD, SCHEMA_MOD]),
               "--mode", "prod", "--governed-prefix", ""],
              capture_output=True, text=True).returncode, 0)

    test_scoped_floors()


def test_scoped_floors() -> None:
    """A floor scoped with applies_to binds its role and nothing else.

    The sweep looks at every pin under the platform prefix, which is what stops a consumer being
    missed. That breadth is about which pins are READ; it is not a claim that every repository has
    the same requirements. When the two were the same thing, the platform-shared-go floor - raised
    for GW-0003, whose definition reads applies_when: gateway - was enforced on 78 of the fleet's
    88 Go modules while all six gateways already met it, and every consumer bump PR a contracts
    release opened failed on it.

    Both directions are asserted. A scope that let everything through would be no floor at all,
    and would pass a test that only checked the first case.
    """
    print("\nscoped floors (applies_to)")
    check("a service below a gateway-scoped floor passes",
          run_checker("below-gateway-floor-service", "prod"), 0)
    check("...and the same pin in a gateway blocks",
          run_checker("below-gateway-floor-gateway", "prod"), 1)
    check("a gateway named only by its service contract blocks too",
          run_checker("below-gateway-floor-by-contract", "prod"), 1)
    # --role overrides detection, so the service fixture must block when told it is a gateway.
    # Without this, a detect_role that returned "service" unconditionally would pass every case
    # above and the scope would be enforcing nothing.
    check("the floor itself still bites when the role says gateway",
          subprocess.run(
              [sys.executable, str(CHECKER),
               "--go-mod", str(PINS / "below-gateway-floor-service" / "go.mod"),
               "--modules", json.dumps([CORE_MOD, SCHEMA_MOD]),
               "--mode", "prod", "--role", "gateway"],
              capture_output=True, text=True).returncode, 1)


def main() -> int:
    print("release cascade: dependency DAG derivation")
    test_fan_out_downward()
    test_shared_library_fan_out()
    test_cycle_is_withheld()
    test_pins_upward()
    test_pin_policy()

    print()
    if failures:
        print(f"::error::{len(failures)} assertion(s) failed: {', '.join(failures)}")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
