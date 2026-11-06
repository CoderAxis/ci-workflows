#!/usr/bin/env python3
"""Self-test for the routing of container tests in service-ci.yaml.

Build and test runs on self-hosted pools with no Docker daemon. A test that starts its own
container fails there with "rootless Docker not found", which is what notification-core's
partition-worker test did on every run after 2026-10-31. scripts/docker-test-packages.sh finds
the packages whose tests do that; Build and test runs them without the integration tag, and the
docker-tests job runs them with it on a GitHub-hosted runner.

Three halves, all against fixtures/docker-test-packages, a module whose container libraries are
local stubs so that nothing here needs the network:

  THE DETECTOR. Which packages it names under which tags, including a test that reaches the
  daemon only through a helper package, from an external test package.

  THE STEPS, executed. The run blocks are read out of service-ci.yaml and run as bash against the
  fixture, so what is tested is the shell the pipeline runs, not a copy of it. The coverage step
  given no list - which is what it did before this change, and what a deployable still gets -
  must fail the way the pool did; given the detector's list it must pass, keep the Postgres-only
  integration tests and the container packages' unit tests in the coverage profile, and refuse a
  container test that is not behind the tag.

  THE WIRING. The docker-tests job is on a hosted runner, reads Build and test's list, runs only
  for libraries, and is watched by notify and artifact.

Run from the repository root:  python3 scripts/test_docker_test_packages.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "service-ci.yaml"
SCRIPT = REPO / "scripts" / "docker-test-packages.sh"
FIXTURE = REPO / "fixtures" / "docker-test-packages"
MOD = "example.com/dockerfixture"

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def go_env(**extra: str) -> dict:
    env = dict(os.environ)
    # Offline: the stubs are `replace`d to local directories, so a lookup elsewhere is a bug.
    env.update({"GOWORK": "off", "GOFLAGS": "-mod=mod", "GOPROXY": "off"})
    env.pop("FIXTURE_DOCKER", None)
    env.update(extra)
    return env


def detect(tags: str, cwd: Path = FIXTURE) -> tuple[int, list[str], str]:
    proc = subprocess.run(["bash", str(SCRIPT), tags], cwd=cwd, env=go_env(),
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout.split(), proc.stderr


def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def step(job: str, name: str) -> dict:
    for s in jobs()[job].get("steps") or []:
        if s.get("name") == name:
            return s
    raise KeyError(f"service-ci.yaml job {job} has no step named {name!r}")


def run_step(job: str, name: str, workdir: Path, **env: str) -> tuple[int, str, dict]:
    """Run one step's `run:` block the way GitHub runs `shell: bash`, returning its outputs."""
    env.setdefault("RUNNER_TEMP", str(workdir.parent / "runner_temp"))
    script = workdir.parent / "step.sh"
    script.write_text(step(job, name)["run"], encoding="utf-8")
    outputs = workdir.parent / "github_output"
    outputs.write_text("", encoding="utf-8")
    summary = workdir.parent / "github_step_summary"
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=workdir,
        env=go_env(GITHUB_OUTPUT=str(outputs), GITHUB_STEP_SUMMARY=str(summary), **env),
        capture_output=True, text=True)
    parsed = {}
    for line in outputs.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        parsed[key] = value
    return proc.returncode, proc.stdout + proc.stderr, parsed


CENTRAL = "ci-workflows-central"


def checkout(tmp: Path) -> Path:
    """The fixture as a service checkout, with the central scripts where Build and test moves
    them: under RUNNER_TEMP, outside the workspace."""
    work = tmp / "repo"
    shutil.copytree(FIXTURE, work)
    scripts = tmp / "runner_temp" / CENTRAL / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    return work


def coverage_of(work: Path) -> dict[str, float]:
    proc = subprocess.run(["go", "tool", "cover", "-func=coverage.out"], cwd=work, env=go_env(),
                          capture_output=True, text=True)
    out: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1].endswith("%"):
            out[f"{parts[0].rsplit('/', 1)[-1].split(':')[0]}:{parts[1]}"] = float(parts[-1][:-1])
    return out


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

def test_the_detector_names_container_tests_behind_the_tag() -> None:
    code, pkgs, err = detect("integration")
    expect(code == 0, f"detector failed on the fixture: {err}")
    expect(pkgs == [f"{MOD}/containers", f"{MOD}/viahelper"],
           f"with -tags integration the detector must name exactly the package that imports a "
           f"container library and the one that reaches the daemon through a helper; got {pkgs}")


def test_postgres_only_and_unit_tests_are_not_named() -> None:
    _, pkgs, _ = detect("integration")
    for clean in ("plain", "pgonly"):
        expect(f"{MOD}/{clean}" not in pkgs,
               f"{clean} starts no container and must stay on the pool; got {pkgs}")
    code, untagged, err = detect("")
    expect(code == 0 and untagged == [],
           f"without the tag no fixture test starts a container; got {untagged} {err}")


def test_a_container_test_without_the_tag_is_named_without_it() -> None:
    _, pkgs, _ = detect("fixture_untagged_docker")
    expect(pkgs == [f"{MOD}/untagged"],
           f"a container test outside the integration tag must be found without it; got {pkgs}")


def test_a_broken_module_fails_rather_than_reading_as_none(tmp: Path) -> None:
    broken = tmp / "broken"
    broken.mkdir()
    (broken / "go.mod").write_text("module example.com/broken\n\ngo 1.22\n", encoding="utf-8")
    (broken / "b_test.go").write_text('package b\n\nimport "example.com/missing"\n', encoding="utf-8")
    code, pkgs, _ = detect("integration", cwd=broken)
    expect(code != 0, f"a go list failure must fail the detector, not report {pkgs or 'nothing'}")


# ---------------------------------------------------------------------------
# The steps, executed
# ---------------------------------------------------------------------------

def test_the_coverage_step_without_a_list_fails_like_the_pool(tmp: Path) -> None:
    """The defect: every tagged test on a runner without Docker, which is what a deployable
    still gets and what every library got before the list existed."""
    work = checkout(tmp)
    code, out, _ = run_step("build-test", "Tests with coverage", work,
                            GO_TAGS="integration", DOCKER_PACKAGES="", KUBERNETES_SERVICE_HOST="")
    expect(code != 0 and "rootless Docker not found" in out,
           f"with no list the container test must run and fail here (exit {code}):\n{out[-1500:]}")


def test_the_coverage_step_routes_the_container_packages(tmp: Path) -> None:
    work = checkout(tmp)
    code, out, found = run_step("build-test", "Find the tests that need Docker", work,
                                GO_TAGS="integration")
    expect(code == 0, f"detection step failed:\n{out}")
    expect(found.get("packages") == f"{MOD}/containers {MOD}/viahelper",
           f"detection step output: {found}")

    code, out, _ = run_step("build-test", "Tests with coverage", work, GO_TAGS="integration",
                            DOCKER_PACKAGES=found.get("packages", ""), KUBERNETES_SERVICE_HOST="")
    expect(code == 0, f"with the list the coverage step must pass on a runner without Docker:\n"
                      f"{out[-1500:]}")
    profile = (work / "coverage.out").read_text(encoding="utf-8").splitlines()
    expect(profile[:1] == ["mode: atomic"] and profile.count("mode: atomic") == 1,
           "the merged profile must carry exactly one mode line, first")

    cov = coverage_of(work)
    expect(cov.get("pgonly.go:Tagged") == 100.0,
           f"Postgres-only integration tests must still run in Build and test; {cov}")
    expect(cov.get("containers.go:Unit") == 100.0,
           f"a container package's unit tests must still count toward coverage; {cov}")
    expect(cov.get("containers.go:NeedsDocker") == 0.0,
           f"the container test must not have run on the pool; {cov}")


def test_a_container_test_outside_the_tag_is_refused_by_name(tmp: Path) -> None:
    work = checkout(tmp)
    tags = "integration,fixture_untagged_docker"
    _, _, found = run_step("build-test", "Find the tests that need Docker", work, GO_TAGS=tags)
    code, out, _ = run_step("build-test", "Tests with coverage", work, GO_TAGS=tags,
                            DOCKER_PACKAGES=found.get("packages", ""), KUBERNETES_SERVICE_HOST="")
    expect(code != 0 and f"{MOD}/untagged" in out and "without the integration build tag" in out,
           f"a container test outside the tag must be named, not left to fail on the daemon:\n"
           f"{out[-1500:]}")


def test_the_hosted_job_runs_the_container_tests(tmp: Path) -> None:
    work = checkout(tmp)
    pkgs = f"{MOD}/containers {MOD}/viahelper"
    code, out, _ = run_step("docker-tests", "Run the tests that start containers", work,
                            GO_TAGS="integration", DOCKER_PACKAGES=pkgs, FIXTURE_DOCKER="1")
    expect(code == 0 and "TestStartsAContainer" in out and "TestThroughTheHelper" in out,
           f"with a daemon the hosted step must run and pass both container tests:\n{out[-1500:]}")
    code, out, _ = run_step("docker-tests", "Run the tests that start containers", work,
                            GO_TAGS="integration", DOCKER_PACKAGES=pkgs)
    expect(code != 0, "without a daemon the hosted step must fail: it runs the container tests")


def test_the_central_checkout_leaves_the_workspace(tmp: Path) -> None:
    """Tests that walk the repository tree read whatever sits in it; the central scripts must not."""
    work = tmp / "repo"
    (work / ".central" / "scripts").mkdir(parents=True)
    (work / ".central" / "scripts" / "probe.sh").write_text("true\n", encoding="utf-8")
    (tmp / "runner_temp").mkdir()  # a runner always has its RUNNER_TEMP
    code, out, _ = run_step("build-test", "Move the central checkers out of the workspace", work)
    expect(code == 0, f"the move step failed:\n{out}")
    expect(not (work / ".central").exists(), "the central checkout is still in the workspace")
    expect((tmp / "runner_temp" / CENTRAL / "scripts" / "probe.sh").is_file(),
           "the central checkout did not arrive under RUNNER_TEMP")


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------

def _needs(job: dict) -> list[str]:
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def test_build_test_publishes_the_list_from_its_detection_step() -> None:
    bt = jobs()["build-test"]
    expect(bt.get("runs-on") == "inboxxhq-ci-heavy", "Build and test stays on the heavy pool")
    expect((bt.get("outputs") or {}).get("docker_packages") == "${{ steps.docker.outputs.packages }}",
           "build-test must publish the detection step's list as docker_packages")
    names = [s.get("name") for s in bt["steps"]]
    detect_at = names.index("Find the tests that need Docker")
    expect(names.index("Check out the central checkers") < detect_at < names.index("Tests with coverage"),
           "the central checkout must precede detection, and detection the coverage run")
    move_at = names.index("Move the central checkers out of the workspace")
    expect(names.index("Check out the central checkers") + 1 == move_at,
           "the central checkout must be moved out of the workspace in the very next step")
    tests = [i for i, s in enumerate(bt["steps"]) if "go test" in str(s.get("run", ""))]
    expect(tests and move_at < min(tests),
           "the central checkout must leave the workspace before any step that runs tests")
    for s in bt["steps"][move_at + 1:]:
        body = str(s.get("run", "")) + str(s.get("with", ""))
        expect(".central/" not in body,
               f"step {s.get('name')!r} still reads .central/ inside the workspace")
    det = bt["steps"][detect_at]
    expect(det.get("id") == "docker", "the detection step's id must be docker")
    expect("repo_kind != 'deployable'" in str(det.get("if", "")),
           "detection must be skipped for a deployable, whose Build and test must not change")
    cov = step("build-test", "Tests with coverage")
    expect(cov.get("env", {}).get("DOCKER_PACKAGES") == "${{ steps.docker.outputs.packages }}",
           "the coverage run must read the detection step's list")


def test_the_docker_tests_job_is_hosted_and_library_only() -> None:
    job = jobs()["docker-tests"]
    expect(job.get("runs-on") == "ubuntu-latest",
           "docker-tests needs a Docker daemon, which only GitHub-hosted runners have")
    expect("build-test" in _needs(job), "docker-tests reads build-test's output")
    cond = " ".join(str(job.get("if", "")).split())
    expect("needs.build-test.outputs.docker_packages != ''" in cond,
           f"docker-tests must be skipped, taking no runner, when there is nothing to run; {cond!r}")
    expect("needs.resolve.outputs.repo_kind != 'deployable'" in cond,
           f"docker-tests is for libraries; a deployable has the integration job; {cond!r}")
    run = step("docker-tests", "Run the tests that start containers")
    env = run.get("env") or {}
    expect(env.get("DOCKER_PACKAGES") == "${{ needs.build-test.outputs.docker_packages }}",
           "the hosted run must take exactly the list Build and test ran without the tag")
    expect(env.get("GO_TAGS") == "${{ inputs.go_build_tags }}",
           "the hosted run must use the same build tags as Build and test")
    uses = [s.get("uses", "") for s in job["steps"]]
    expect(any("go-build-cache/restore" in u for u in uses)
           and any("go-build-cache/save" in u for u in uses),
           "docker-tests must restore and save the Go build cache: its minutes are billed")


def test_the_gate_is_watched() -> None:
    j = jobs()
    expect("docker-tests" in _needs(j["notify"]), "notify must watch docker-tests")
    expect("docker-tests" in _needs(j["artifact"]), "artifact must list docker-tests")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ran = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        ran += 1
        tmp = Path(tempfile.mkdtemp(prefix="docker-test-packages-"))
        try:
            fn(tmp) if fn.__code__.co_argcount == 1 else fn()
        # A missing job or step is a failure of that test, not the end of the run.
        except (KeyError, ValueError, OSError) as exc:
            FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if FAILURES:
        print(f"docker test routing self-test: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"docker test routing self-test: OK ({ran} test(s)) - the detector names container "
          "tests behind the tag, through helpers and external test packages, and fails on a "
          "broken module; the coverage step fails as the pool did without the list, and with it "
          "keeps Postgres-only and unit coverage while refusing an untagged container test; the "
          "hosted job runs them; and the wiring is asserted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
