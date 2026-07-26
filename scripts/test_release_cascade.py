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

CORE_ID, SCHEMA_ID, DEPLOYABLE_ID = 900000001, 900000002, 900000003
CORE_MOD = "github.com/coderaxis/fixture-core"
SCHEMA_MOD = "github.com/coderaxis/fixture-core-postgres"

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
    """Releasing a module must notify everything the catalog says depends on it."""
    print("\nfan-out (downward: who must be notified)")

    core = facts("coderaxis/fixture-core", CORE_ID)
    check("core repo_kind", core["repo_kind"], "core")
    check("core module path", core["module_path"], CORE_MOD)
    check("core dispatch event", core["dispatch_event"], "fixture-core-released")
    # Both, because the deployable pins the core directly as well as through the adapter - the
    # "diamond". Notifying only the adapter would leave the service on an old core indefinitely.
    check(
        "core notifies adapter AND deployable",
        json.loads(core["consumers"]),
        [
            {"repository": "coderaxis/fixture-core-postgres", "kind": "schema"},
            {"repository": "InboxxHQ-CoderAxis/inboxxhq-fixture-service", "kind": "deployable"},
        ],
    )

    schema = facts("coderaxis/fixture-core-postgres", SCHEMA_ID)
    check("schema repo_kind", schema["repo_kind"], "schema")
    check("schema dispatch event", schema["dispatch_event"], "fixture-core-postgres-released")
    check(
        "adapter notifies the deployable only",
        json.loads(schema["consumers"]),
        [{"repository": "InboxxHQ-CoderAxis/inboxxhq-fixture-service", "kind": "deployable"}],
    )

    # A deployable is an image pinned by digest, not a module anything can `go get`. If it ever
    # produced a module path, module-release.yml would tag a repo no consumer can resolve.
    dep = facts("InboxxHQ-CoderAxis/inboxxhq-fixture-service", DEPLOYABLE_ID)
    check("deployable repo_kind", dep["repo_kind"], "deployable")
    check("deployable is not releasable as a module", dep["module_path"], "")
    check("deployable notifies nobody", json.loads(dep["consumers"]), [])


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


def main() -> int:
    print("release cascade: dependency DAG derivation")
    test_fan_out_downward()
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
