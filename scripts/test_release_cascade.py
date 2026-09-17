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

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from resolve_service_identity import resolve  # noqa: E402

HERE = pathlib.Path(__file__).parent
CATALOG = HERE / "testdata" / "release-cascade-catalog"
PINS = HERE / "testdata" / "module-pins"


def checker_module():
    """Import check_module_pins as a module, the way ci.yaml's inline unit test does."""
    spec = importlib.util.spec_from_file_location("pins", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
CHECKER = HERE / "check_module_pins.py"

CORE_ID, SCHEMA_ID, DEPLOYABLE_ID, SHARED_ID = 900000001, 900000002, 900000003, 900000004
ADAPTER_CORE_ID, ADAPTER_SCHEMA_ID = 900000005, 900000006
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

    # A family whose core was never adopted. The catalog names one, so the family shape says
    # there is an upstream, and the shape was what this used to answer from - which made the
    # release gate demand a pin that does not exist and reject every version the module could
    # ever publish. Three real families are shaped this way, and the first of them to attempt
    # a release found a payment fix held up by a module nothing imports.
    #
    # The graph is asked instead, because "pins directly" is a fact about the go.mod rather
    # than about how the catalog groups repositories.
    check("a schema module that does not pin its core declares no upstream",
          facts("coderaxis/fixture-adapter-core-postgres", ADAPTER_SCHEMA_ID)["upstream_module"],
          "")
    # ...and it is still recognised as a schema module, so this is the gate declining to apply
    # rather than the repository failing to resolve.
    check("...and is still resolved as a schema module",
          facts("coderaxis/fixture-adapter-core-postgres", ADAPTER_SCHEMA_ID)["repo_kind"],
          "schema")


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

    test_check_accepts_a_plain_floor_mapping()


def test_check_accepts_a_plain_floor_mapping() -> None:
    """check() is imported and called directly, not only through the CLI.

    ci.yaml's floor-comparison unit test builds {module: "vX.Y.Z"} itself rather than going
    through load_floors, which is the shape this function took before floors could be scoped.
    Adding the scope broke that caller and nothing here noticed, because every case above shells
    out to the CLI - so the library contract needs a case of its own.
    """
    print("\nfloors passed in as plain strings (the library calling convention)")
    module = "github.com/coderaxis/platform-shared-go"
    go_mod = pathlib.Path(tempfile.mkdtemp()) / "go.mod"
    go_mod.write_text(f"module x\n\ngo 1.24\n\nrequire {module} v1.21.0\n", encoding="utf-8")

    errors, _, _ = checker_module().check(
        go_mod, [], "prod", "github.com/coderaxis/", {module: "v1.22.0"})
    check("a bare version string is read as a fleet-wide floor and still blocks",
          bool(errors), True)


BUMP_ACTION = HERE.parent / "bump-module-pin" / "action.yaml"
REGEN_STEP = "Regenerate OpenAPI contract from the bumped module"


def bump_steps() -> list:
    import yaml  # the release-cascade job installs PyYAML; the pin checker needs it too

    return yaml.safe_load(BUMP_ACTION.read_text(encoding="utf-8"))["runs"]["steps"]


def run_regen_step(tree: pathlib.Path) -> subprocess.CompletedProcess:
    """Run the action's own regenerate script, exactly as written, inside a fixture consumer."""
    step = next(s for s in bump_steps() if s.get("name") == REGEN_STEP)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tree),
           "MODULE": "github.com/coderaxis/platform-shared-go", "VERSION": "v1.60.0",
           "GOWORK": "/somewhere/go.work"}
    return subprocess.run(["bash", "-c", step["run"]], cwd=tree, env=env,
                          capture_output=True, text=True)


def consumer(spec: bool, makefile: str | None) -> pathlib.Path:
    tree = pathlib.Path(tempfile.mkdtemp())
    if spec:
        (tree / "docs").mkdir()
        (tree / "docs" / "openapi.json").write_text('{"stale": true}\n', encoding="utf-8")
    if makefile is not None:
        (tree / "Makefile").write_text(makefile, encoding="utf-8")
    return tree


# Recipes are tab-indented. The target records the GOWORK it ran under, so the test can prove
# the regeneration resolved go.mod rather than a workspace.
GENERATING_MAKEFILE = (
    ".PHONY: openapi-contract openapi-contract-check\n"
    "openapi-contract: ## Regenerate docs/openapi.json\n"
    "\t@printf '%s' \"$$GOWORK\" > gowork.seen\n"
    "\t@printf '{\"regenerated\": true}\\n' > docs/openapi.json\n"
    "\n"
    "openapi-contract-check:\n"
    "\t@echo check\n"
)
CHECK_ONLY_MAKEFILE = (
    "openapi-contract-check:\n"
    "\t@printf '{\"wrong target\": true}\\n' > docs/openapi.json\n"
)
FAILING_MAKEFILE = "openapi-contract:\n\t@exit 3\n"


def test_bump_regenerates_openapi_contract() -> None:
    """The bump PR regenerates a spec-publishing consumer's docs/openapi.json.

    The step used to `go run ${MODULE}/adapters/inbound/http/openapi/cmd/openapi-contract`, a path
    platform-shared-go never had, so every shared-go fan-out opened pin-only PRs over stale specs
    while the step printed a notice and reported success. A step that silently does nothing is
    the failure this suite exists to catch, so each branch is exercised by running the action's
    own script rather than a copy of it.
    """
    print("\nbump-module-pin: OpenAPI regeneration runs the consumer's make target")
    names = [s.get("name") for s in bump_steps()]
    step = next(s for s in bump_steps() if s.get("name") == REGEN_STEP)
    check("regeneration still tolerates a broken generator (continue-on-error)",
          step.get("continue-on-error"), True)
    check("regeneration runs after the pin moves and before the tests judge the tree",
          names.index("Bump the pin") < names.index(REGEN_STEP) < names.index("Test against the new pin"),
          True)
    check("the non-existent in-module generator path is gone",
          "adapters/inbound/http/openapi/cmd/openapi-contract" in step["run"], False)

    tree = consumer(spec=True, makefile=GENERATING_MAKEFILE)
    proc = run_regen_step(tree)
    check("spec + openapi-contract target: step succeeds", proc.returncode, 0)
    check("spec + openapi-contract target: docs/openapi.json is regenerated",
          (tree / "docs" / "openapi.json").read_text(encoding="utf-8"), '{"regenerated": true}\n')
    seen = tree / "gowork.seen"
    check("spec + openapi-contract target: make runs with GOWORK=off",
          seen.read_text(encoding="utf-8") if seen.exists() else None, "off")
    check("spec + openapi-contract target: the log names module and version",
          "contract regenerated against github.com/coderaxis/platform-shared-go v1.60.0" in proc.stdout,
          True)

    tree = consumer(spec=True, makefile=CHECK_ONLY_MAKEFILE)
    proc = run_regen_step(tree)
    check("only openapi-contract-check: the prefix does not match, nothing runs",
          ((tree / "docs" / "openapi.json").read_text(encoding="utf-8"), proc.returncode),
          ('{"stale": true}\n', 0))
    check("only openapi-contract-check: a notice says the gate still applies",
          "::notice::" in proc.stdout, True)

    tree = consumer(spec=True, makefile=None)
    proc = run_regen_step(tree)
    check("spec without a Makefile: skipped with a notice",
          (proc.returncode, "::notice::" in proc.stdout), (0, True))

    tree = consumer(spec=False, makefile=GENERATING_MAKEFILE.replace("docs/openapi.json", "spec.out"))
    proc = run_regen_step(tree)
    check("target without docs/openapi.json (a library): make is not run",
          (proc.returncode, (tree / "gowork.seen").exists(), (tree / "spec.out").exists()),
          (0, False, False))

    tree = consumer(spec=True, makefile=FAILING_MAKEFILE)
    proc = run_regen_step(tree)
    check("a failing generator fails the step (continue-on-error keeps the bump going)",
          proc.returncode != 0, True)


# A stand-in for `go` that behaves like a consumer whose tests boot a router: it applies the rule
# platform-shared-go's envutil.IsDeployed() applies (KUBERNETES_SERVICE_HOST non-empty, or an
# ENVIRONMENT other than local/test) and fails the way ginmiddleware.ServiceAuthMiddleware does
# when there is no SERVICE_TOKEN_SECRET. `go list` prints one package so the unit step has work.
FAKE_GO = r"""#!/usr/bin/env bash
if [[ "$1" == "list" ]]; then echo "example.com/consumer/internal/app"; exit 0; fi
echo "$*" >> "${GO_CALLS}"
env_=$(printf '%s' "${ENVIRONMENT:-}" | tr '[:upper:]' '[:lower:]')
if [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] || [[ -n "${env_}" && "${env_}" != "local" && "${env_}" != "test" ]]; then
  if [[ -z "${SERVICE_TOKEN_SECRET:-}" ]]; then
    echo "ginmiddleware: consumer-service is deployed and has no SERVICE_TOKEN_SECRET; refusing to serve unauthenticated"
    exit 1
  fi
fi
exit 0
"""

# What an ARC runner pod's environment looks like to a step: the kubelet injects these into every
# container, including the runner's.
RUNNER_POD_ENV = {"KUBERNETES_SERVICE_HOST": "172.20.0.1", "KUBERNETES_SERVICE_PORT": "443",
                  "KUBERNETES_PORT": "tcp://172.20.0.1:443"}


def run_bump_step(name: str, tree: pathlib.Path, runner_env: dict, apply_step_env: bool = True):
    """Run a bump-module-pin step's script as the runner would: pod env, then the step's env on top.

    A step `env:` value replaces the inherited variable, and "" sets it to empty rather than
    leaving the pod's value, which is what makes the blanking work. Expression values cannot be
    evaluated here and are replaced by a placeholder; the variables this test cares about are
    literals.
    """
    step = next(s for s in bump_steps() if s.get("name") == name)
    bindir = tree / ".fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "go"
    fake.write_text(FAKE_GO, encoding="utf-8")
    fake.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin", "HOME": str(tree),
           "GO_CALLS": str(tree / "go.calls")}
    env.update(runner_env)
    if apply_step_env:
        for key, value in (step.get("env") or {}).items():
            env[key] = "placeholder" if "${{" in str(value) else str(value)
    return step, subprocess.run(["bash", "-c", step["run"]], cwd=tree, env=env,
                                capture_output=True, text=True)


def contract_consumer() -> pathlib.Path:
    tree = pathlib.Path(tempfile.mkdtemp())
    (tree / "tests" / "contracts").mkdir(parents=True)
    return tree


def test_bump_test_steps_are_not_a_deployment() -> None:
    """Every step that runs the consumer's Go tests blanks the pod's deployment signal.

    Only "Test against the new pin" carried KUBERNETES_SERVICE_HOST: "", so on the ARC pool every
    consumer with a contract router passed its unit tests and then failed the contract gate with
    "is deployed and has no SERVICE_TOKEN_SECRET" (platform-shared-go v1.60.0, run 35189576191).
    The assertion is over every `go test` step, so a gate added later inherits the requirement.
    """
    print("\nbump-module-pin: a runner pod is not a deployment for the tests it runs")

    test_steps = [s for s in bump_steps() if "go test" in (s.get("run") or "")]
    check("the unit and contract steps are the ones that run go test",
          sorted(s["name"] for s in test_steps), ["Contract gate", "Test against the new pin"])
    for step in test_steps:
        env = step.get("env") or {}
        check(f"{step['name']}: KUBERNETES_SERVICE_HOST is blanked in the step env",
              ("KUBERNETES_SERVICE_HOST" in env, env.get("KUBERNETES_SERVICE_HOST")), (True, ""))
        check(f"{step['name']}: ENVIRONMENT is not set to a deployed value by the step",
              str(env.get("ENVIRONMENT", "")).lower() in ("", "local", "test"), True)

    # The control: on pod env with the step env withheld, the fake consumer must fail, or the
    # passes below prove nothing about the blanking.
    tree = contract_consumer()
    _, proc = run_bump_step("Contract gate", tree, RUNNER_POD_ENV, apply_step_env=False)
    check("control: without the step env, a router test on a runner pod fails as deployed",
          (proc.returncode != 0, "is deployed and has no SERVICE_TOKEN_SECRET" in proc.stdout),
          (True, True))

    for name in ("Test against the new pin", "Contract gate"):
        tree = contract_consumer()
        _, proc = run_bump_step(name, tree, RUNNER_POD_ENV)
        calls = (tree / "go.calls").read_text(encoding="utf-8") if (tree / "go.calls").exists() else ""
        check(f"{name}: on a runner pod the consumer's tests run and pass",
              (proc.returncode, "test " in calls), (0, True))

    tree = contract_consumer()
    _, proc = run_bump_step("Contract gate", tree, RUNNER_POD_ENV)
    check("Contract gate: it runs the contract suite, not something else",
          "test ./tests/contracts/... -count=1" in (tree / "go.calls").read_text(encoding="utf-8"),
          True)

    # Hosted runners have no KUBERNETES_SERVICE_HOST at all; the step must behave the same.
    tree = contract_consumer()
    _, proc = run_bump_step("Contract gate", tree, {})
    check("Contract gate: off-cluster (hosted runner) it passes too", proc.returncode, 0)


def main() -> int:
    print("release cascade: dependency DAG derivation")
    test_fan_out_downward()
    test_shared_library_fan_out()
    test_cycle_is_withheld()
    test_pins_upward()
    test_pin_policy()
    test_bump_regenerates_openapi_contract()
    test_bump_test_steps_are_not_a_deployment()

    print()
    if failures:
        print(f"::error::{len(failures)} assertion(s) failed: {', '.join(failures)}")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
