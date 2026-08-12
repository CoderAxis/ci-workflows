#!/usr/bin/env python3
"""Self-test for the documentation-only fast path.

Two halves, and both are necessary.

THE CLASSIFIER, against real git repositories built in a temporary directory.
A synthetic list of filenames would not exercise the part most likely to break,
which is the range computation: what `before` means on a push, what it means
when there is no `before`, and what a multi-commit push looks like when only its
head commit is documentation. Every case here builds commits and asks the script
what it makes of them.

THE WIRING, against .github/workflows/service-ci.yaml itself. A perfect
classifier wired to nothing is the failure this repository keeps rediscovering -
the operationId guard that existed centrally and was connected to no job, the
absorbed workflows that ran in parallel with the central one. So the structural
assertions pin the whole contract: every expensive job is gated, every
documentation gate is NOT, and `docs-only` is named in the needs of the deploy
aggregation and the notifier so a broken classifier cannot skip every gate in
the pipeline and then let a digest be pinned from the result.

The bias under test is one-directional. A wrong `false` wastes a pipeline; a
wrong `true` puts unreviewed code on main. Every ambiguous case below therefore
asserts `false`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CLASSIFIER = REPO / "scripts" / "docs-only-diff.py"
WORKFLOW = REPO / ".github" / "workflows" / "service-ci.yaml"

ZERO_SHA = "0" * 40

FAILURES: list[str] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# ---------------------------------------------------------------------------
# A real git repository to diff
# ---------------------------------------------------------------------------

class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@example.test")
        self.git("config", "user.name", "CI Self Test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def write(self, rel: str, text: str = "x\n") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def base_repo(root: Path) -> tuple[Repo, str]:
    """A repository with the shape a caller has, and its first commit."""
    r = Repo(root)
    r.write("README.md", "# service\n")
    r.write("go.mod", "module example.test\n\ngo 1.25\n")
    r.write("internal/handler.go", "package internal\n")
    r.write("docs/openapi.json", '{"openapi":"3.0.0"}\n')
    r.write("docs/adr/ADR_INDEX.md", "# index\n")
    r.write(".github/workflows/ci.yaml", "name: CI\non: {push: {}}\n")
    return r, r.commit("initial")


def classify(repo: Repo, event_name: str, payload: dict) -> tuple[bool, str]:
    """Run the classifier the way the workflow runs it; return (docs_only, output)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        event_path = fh.name
    proc = subprocess.run(
        [sys.executable, str(CLASSIFIER),
         "--repo-root", str(repo.root),
         "--event-name", event_name,
         "--event-path", event_path],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    Path(event_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        return False, f"classifier exited {proc.returncode}\n{out}"
    return ("docs_only=true" in out), out


def push(before: str, after: str, **extra) -> dict:
    payload = {"before": before, "after": after,
               "created": False, "deleted": False, "forced": False}
    payload.update(extra)
    return payload


def pull_request(base: str, head: str) -> dict:
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


# ---------------------------------------------------------------------------
# The five cases the change was asked to cover
# ---------------------------------------------------------------------------

def test_documentation_only_diff_is_documentation() -> None:
    """The whole point: markdown, and nothing else, takes the fast path."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("README.md", "# service\n\nA new paragraph.\n")
        r.write("docs/runbook.md", "# runbook\n")
        after = r.commit("docs: describe the runbook")
        ok, out = classify(r, "push", push(before, after))
        expect(ok, f"a markdown-only push must be documentation;\n{out}")


def test_mixed_diff_is_not_documentation() -> None:
    """One Go file alongside the markdown runs everything."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("README.md", "# service\n\nupdated\n")
        r.write("internal/handler.go", "package internal\n\nfunc New() {}\n")
        after = r.commit("docs and code")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"a diff containing a .go file must run everything;\n{out}")
        expect("internal/handler.go" in out,
               f"the reason must name the path that disqualified the diff;\n{out}")


def test_workflow_file_only_diff_is_not_documentation() -> None:
    """A .github/ change is the one thing that must never take the fast path.

    It is the file that decides which gates exist. A fast path that reads it as
    documentation would let someone weaken the pipeline in a commit the pipeline
    declines to inspect.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write(".github/workflows/ci.yaml", "name: CI\non: {push: {}}\n# changed\n")
        after = r.commit("ci: tweak the caller")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"a workflow-file diff must run everything;\n{out}")


def test_markdown_under_dot_github_is_not_documentation() -> None:
    """Not even a .md file, if it lives under .github/."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write(".github/PULL_REQUEST_TEMPLATE.md", "# template\n")
        after = r.commit("chore: add a PR template")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok,
               f".github/**/*.md must not count as documentation;\n{out}")


def test_multi_commit_range_with_code_in_an_earlier_commit() -> None:
    """The head commit is documentation; an earlier commit in the push is not.

    This is the shape a fast path keyed on HEAD gets wrong, and it is not exotic:
    it is what every ordinary `git push` of two commits looks like.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("internal/handler.go", "package internal\n\nfunc Sneak() {}\n")
        r.commit("feat: add a handler")
        r.write("README.md", "# service\n\nnote\n")
        after = r.commit("docs: a note")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok,
               f"code in an earlier commit of the range must run everything;\n{out}")
        expect("internal/handler.go" in out,
               f"the reason must name the earlier commit's code path;\n{out}")


def test_undeterminable_range_runs_everything() -> None:
    """Every shape of "there is no range I can trust" answers false."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("README.md", "# service\n\nnote\n")
        after = r.commit("docs: a note")

        cases = {
            "all-zero before-SHA (first push of a ref)":
                ("push", push(ZERO_SHA, after)),
            "created flag set":
                ("push", push(before, after, created=True)),
            "deleted flag set":
                ("push", push(before, after, deleted=True)),
            "forced push":
                ("push", push(before, after, forced=True)),
            "before-SHA absent from the payload":
                ("push", {"after": after}),
            "before-SHA not present in the clone":
                ("push", push("a" * 40, after)),
            "empty event payload":
                ("push", {}),
            "workflow_dispatch has no range":
                ("workflow_dispatch", {}),
            "schedule has no range":
                ("schedule", {}),
            "an event this pipeline has not met":
                ("repository_dispatch", {}),
        }
        for label, (event, payload) in cases.items():
            ok, out = classify(r, event, payload)
            expect(not ok, f"{label}: must run everything;\n{out}")


# ---------------------------------------------------------------------------
# Shaping a diff to look like documentation
# ---------------------------------------------------------------------------

def test_a_rename_from_go_to_markdown_is_not_documentation() -> None:
    """`git mv internal/handler.go docs/handler.md` deletes a source file.

    With git's default rename detection, `--name-only` prints only the
    DESTINATION of a rename, so this diff would present as one markdown path.
    `--no-renames` reports it as a delete and an add, and the delete is a .go.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.git("mv", "internal/handler.go", "docs/handler.md")
        after = r.commit("docs: move the handler into the docs tree")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"a rename out of .go must run everything;\n{out}")
        expect("internal/handler.go" in out,
               f"the deleted source path must be named;\n{out}")


def test_markdown_under_testdata_is_not_documentation() -> None:
    """A .md in testdata/ is an input a test reads, not documentation."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("internal/testdata/golden.md", "# expected output\n")
        after = r.commit("test: update the golden file")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"testdata/**/*.md must run everything;\n{out}")


def test_embedded_markdown_is_not_documentation() -> None:
    """A .md compiled into the binary by //go:embed is program input.

    Its content is asserted on by tests and served by the binary, so editing it
    is a code change wearing a documentation extension.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("internal/help/help.go",
                "package help\n\nimport _ \"embed\"\n\n"
                "//go:embed usage.md\nvar Usage string\n")
        r.write("internal/help/usage.md", "usage: thing\n")
        r.commit("feat: embed the usage text")
        before = r.head()
        r.write("internal/help/usage.md", "usage: something else entirely\n")
        after = r.commit("docs: reword the usage text")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"an embedded .md must run everything;\n{out}")


def test_embedded_directory_tree_is_not_documentation() -> None:
    """`//go:embed all:content` covers every file beneath it, .md included."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("internal/site/site.go",
                "package site\n\nimport \"embed\"\n\n"
                "//go:embed all:content\nvar FS embed.FS\n")
        r.write("internal/site/content/index.md", "# home\n")
        r.commit("feat: embed the content tree")
        before = r.head()
        r.write("internal/site/content/index.md", "# home page\n")
        after = r.commit("docs: retitle the home page")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"a .md inside an embedded tree must run everything;\n{out}")


def test_gate_input_files_are_not_documentation() -> None:
    """Every file a gate reads as input, one commit at a time."""
    inputs = {
        "go.mod": "module example.test\n\ngo 1.25\n// changed\n",
        "go.sum": "example.test/x v1.0.0 h1:abc=\n",
        "sqlc.yaml": "version: '2'\n",
        "docs/openapi.json": '{"openapi":"3.1.0"}\n',
        "service.contract.yaml": "kind: service\n",
        "Dockerfile": "FROM scratch\n",
        "package-lock.json": '{"lockfileVersion":3}\n',
        "migrations/0001_init.sql": "CREATE TABLE t (id uuid);\n",
        "schema/schema.sql": "CREATE TABLE u (id uuid);\n",
        "docs/openapi.yaml": "openapi: 3.1.0\n",
        ".gitleaks.toml": "[allowlist]\n",
        "Makefile": "test:\n\t@true\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        r, _ = base_repo(Path(tmp) / "repo")
        for rel, text in inputs.items():
            before = r.head()
            r.write(rel, text)
            after = r.commit(f"touch {rel}")
            ok, out = classify(r, "push", push(before, after))
            expect(not ok, f"{rel} must not count as documentation;\n{out}")


def test_documentation_shapes_that_do_count() -> None:
    """The allowlist is narrow, but it is not empty."""
    allowed = {
        "docs/architecture/overview.md": "# overview\n",
        "docs/adr/ADR-0001-a-decision.md": "# ADR-0001\n",
        "CHANGELOG.md": "# changelog\n",
        "docs/images/diagram.png": "not really a png\n",
        "LICENSE": "MIT\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        r, _ = base_repo(Path(tmp) / "repo")
        for rel, text in allowed.items():
            before = r.head()
            r.write(rel, text)
            after = r.commit(f"docs: {rel}")
            ok, out = classify(r, "push", push(before, after))
            expect(ok, f"{rel} should count as documentation;\n{out}")


def test_image_outside_docs_is_not_documentation() -> None:
    """An .svg in a frontend tree is a shipped asset, not a diagram."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("src/assets/logo.svg", "<svg/>\n")
        after = r.commit("chore: new logo")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"an image outside docs/ must run everything;\n{out}")


def test_deleting_a_source_file_is_not_documentation() -> None:
    """Deletion is a change. `--name-only` lists it; the suffix still decides."""
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.git("rm", "-q", "internal/handler.go")
        r.write("README.md", "# service\n\nremoved the handler\n")
        after = r.commit("chore: drop the handler")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"deleting a .go file must run everything;\n{out}")


def test_code_changed_then_reverted_inside_one_push() -> None:
    """The net tree diff is documentation; a commit in the range was not.

    Defensible either way - the head tree is what gets tested, and it matches
    what `before` already carried - but the union reading is the conservative
    one, and this pins that the union reading is what happens.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.write("internal/handler.go", "package internal\n\nfunc Temp() {}\n")
        r.commit("feat: something")
        r.write("internal/handler.go", "package internal\n")
        r.commit("revert: undo it")
        r.write("README.md", "# service\n\nnote\n")
        after = r.commit("docs: a note")
        ok, out = classify(r, "push", push(before, after))
        expect(not ok,
               f"a code change reverted inside the same push must still run "
               f"everything;\n{out}")


def test_merge_commit_carrying_code_is_not_documentation() -> None:
    """A merge lists no files of its own in `git log --name-only`.

    The union reading alone would miss what the merge brought in, which is why
    the net tree diff is taken as well. This is the case that proves both
    readings are needed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, before = base_repo(Path(tmp) / "repo")
        r.git("checkout", "-q", "-b", "side")
        r.write("internal/side.go", "package internal\n")
        r.commit("feat: side work")
        r.git("checkout", "-q", "main")
        r.write("README.md", "# service\n\nnote\n")
        r.commit("docs: a note")
        r.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        after = r.head()
        ok, out = classify(r, "push", push(before, after))
        expect(not ok, f"a merge that brings in code must run everything;\n{out}")


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------

def test_pull_request_uses_the_merge_base() -> None:
    """A documentation branch stays documentation when the base moves under it.

    Diffing against base.sha rather than the merge base would attribute whatever
    landed on main since the branch forked to this pull request.
    """
    with tempfile.TemporaryDirectory() as tmp:
        r, fork_point = base_repo(Path(tmp) / "repo")
        r.git("checkout", "-q", "-b", "docs-branch")
        r.write("docs/guide.md", "# guide\n")
        head = r.commit("docs: a guide")
        r.git("checkout", "-q", "main")
        r.write("internal/other.go", "package internal\n\nfunc Other() {}\n")
        moved_base = r.commit("feat: unrelated work on main")

        ok, out = classify(r, "pull_request", pull_request(moved_base, head))
        expect(ok, f"a documentation PR must not inherit main's Go commit;\n{out}")
        expect(fork_point[:12] in out,
               f"the range should start at the merge base;\n{out}")


def test_pull_request_with_code_is_not_documentation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r, base = base_repo(Path(tmp) / "repo")
        r.git("checkout", "-q", "-b", "feature")
        r.write("internal/handler.go", "package internal\n\nfunc F() {}\n")
        r.write("README.md", "# service\n\nnote\n")
        head = r.commit("feat: a change with docs")
        ok, out = classify(r, "pull_request", pull_request(base, head))
        expect(not ok, f"a PR containing code must run everything;\n{out}")


def test_pull_request_with_unreachable_base_runs_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r, _ = base_repo(Path(tmp) / "repo")
        r.write("README.md", "# service\n\nnote\n")
        head = r.commit("docs: a note")
        ok, out = classify(r, "pull_request", pull_request("b" * 40, head))
        expect(not ok, f"an unreachable PR base must run everything;\n{out}")


# ---------------------------------------------------------------------------
# Operability
# ---------------------------------------------------------------------------

def test_a_non_git_directory_runs_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "plain"
        plain.mkdir()
        (plain / "README.md").write_text("# hi\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(CLASSIFIER), "--repo-root", str(plain),
             "--event-name", "push"],
            capture_output=True, text=True,
        )
        out = proc.stdout + proc.stderr
        expect(proc.returncode == 0, f"must not crash on a non-git tree;\n{out}")
        expect("docs_only=false" in out,
               f"a non-git tree must run everything;\n{out}")


def test_harness_checkout_inside_the_caller_is_not_read() -> None:
    """ci-workflows, checked out inside the caller, must not supply embed rules.

    The classifier scans the tree for //go:embed directives, and the reusable
    workflow puts this repository inside the tree being scanned - the recurring
    hazard here. This uses a real copy of the tree rather than a synthetic stub,
    because the path only varies for real.
    """
    with tempfile.TemporaryDirectory() as tmp:
        caller_root = Path(tmp) / "caller"
        r, before = base_repo(caller_root)
        r.write("docs/guide.md", "# guide\n")
        after = r.commit("docs: a guide")

        harness = caller_root / ".docs-only-tools"
        shutil.copytree(str(REPO), str(harness),
                        ignore=shutil.ignore_patterns(".git"))

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(push(before, after), fh)
            event_path = fh.name
        proc = subprocess.run(
            [sys.executable, str(harness / "scripts" / "docs-only-diff.py"),
             "--repo-root", str(caller_root),
             "--event-name", "push",
             "--event-path", event_path],
            capture_output=True, text=True,
        )
        Path(event_path).unlink(missing_ok=True)
        out = proc.stdout + proc.stderr
        expect("docs_only=true" in out,
               f"the harness checkout must not change the verdict;\n{out}")


# ---------------------------------------------------------------------------
# The wiring in service-ci.yaml
# ---------------------------------------------------------------------------

# Everything that reads source, builds, or ships. Each must be skipped when the
# diff is documentation, and each must therefore depend on the classifier.
GATED_JOBS = {
    "build-test", "codeql", "events", "seeding", "schema", "dockerfile",
    "structure", "vulnerabilities", "uuid-policy", "sqlc", "integration",
    "contract-classification", "gateway-baseline", "contract",
    "openapi-generator", "e2e", "source-invariants", "dependency-review",
    "artifact",
}

# The gates whose subject IS the documentation, plus the two that must run
# whatever the diff contains. Gating any of these would mean a documentation
# commit was never validated by anything - the opposite of the intent.
UNGATED_JOBS = {
    "resolve",            # identity; everything reads it
    "supported-language",  # trivial, and the guard against a silent green
    "standards",          # actionlint + gitleaks: a secret can be added in a .md
    "lanes",              # the caller is not running its own pipeline
    "contracts",          # ADR filenames, headings and index linkage
    "docs",               # the documentation standard itself
    "org-vocabulary",     # vocabulary, which markdown is full of
    "notify",             # always()
    "docs-only",          # the classifier
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _needs(job: dict) -> list[str]:
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def test_every_job_is_classified_as_gated_or_not() -> None:
    """No job may be added without deciding which side of the fast path it is on."""
    jobs = set(_workflow()["jobs"])
    unaccounted = jobs - GATED_JOBS - UNGATED_JOBS
    expect(not unaccounted,
           f"service-ci.yaml has job(s) this test does not classify: "
           f"{sorted(unaccounted)}. Add each to GATED_JOBS or UNGATED_JOBS.")
    missing = (GATED_JOBS | UNGATED_JOBS) - jobs
    expect(not missing,
           f"this test names job(s) service-ci.yaml no longer has: {sorted(missing)}")


def test_gated_jobs_depend_on_and_test_the_flag() -> None:
    jobs = _workflow()["jobs"]
    for name in sorted(GATED_JOBS & set(jobs)):
        job = jobs[name]
        expect("docs-only" in _needs(job),
               f"{name} must list docs-only in needs, or its `if:` reads an "
               f"empty string and the job runs regardless")
        cond = str(job.get("if", ""))
        expect("needs.docs-only.outputs.docs_only != 'true'" in cond,
               f"{name} must skip when the diff is documentation; if: {cond!r}")


def test_documentation_gates_are_not_gated() -> None:
    jobs = _workflow()["jobs"]
    for name in sorted(UNGATED_JOBS & set(jobs)):
        cond = str(jobs[name].get("if", ""))
        expect("docs_only" not in cond,
               f"{name} validates the thing a documentation commit changes and "
               f"must not be skipped by the fast path; if: {cond!r}")


def test_the_deploy_aggregation_survives_a_skipped_gate() -> None:
    """A skipped job must read as neither failure nor licence to deploy."""
    artifact = _workflow()["jobs"]["artifact"]
    cond = " ".join(str(artifact.get("if", "")).split())

    expect("!contains(needs.*.result, 'failure')" in cond,
           "artifact must aggregate on failure, so a `skipped` gate is tolerated")
    expect("!contains(needs.*.result, 'cancelled')" in cond,
           "artifact must still refuse a cancelled run")
    expect("success" not in cond,
           "artifact must not require `success`: every conditional gate reports "
           "`skipped` on the runs where it does not apply")
    expect("needs.docs-only.outputs.docs_only != 'true'" in cond,
           "artifact must not pin a digest for a documentation-only push: its "
           "gates were skipped, and skipped passes the failure aggregation")

    needs = _needs(artifact)
    expect("docs-only" in needs,
           "docs-only must be in artifact's needs. Every gated job depends on "
           "it, so a crash there skips them all - and skipped is tolerated "
           "above. Unnamed here, one broken classifier pins a digest from a "
           "tree no gate examined.")
    for gate in sorted(GATED_JOBS - {"artifact", "dependency-review",
                                     "contract-classification", "gateway-baseline",
                                     "uuid-policy", "source-invariants"}):
        expect(gate in needs, f"{gate} must remain in artifact's needs")


def test_the_notifier_watches_the_classifier() -> None:
    notify = _workflow()["jobs"]["notify"]
    expect("docs-only" in _needs(notify),
           "notify must watch docs-only: a classifier that fails takes every "
           "gate with it, and nobody would be told why the pipeline was empty")
    cond = " ".join(str(notify.get("if", "")).split())
    expect(cond.startswith("always()"),
           f"notify must use always(), not failure();  if: {cond!r}")


def test_no_workflow_level_path_filter_was_introduced() -> None:
    """The trap this change exists to avoid, pinned so nobody re-adds it.

    A workflow skipped by `paths-ignore:` reports no status at all, and a
    required check then waits forever for one that will never arrive.
    """
    doc = _workflow()
    triggers = doc.get(True, doc.get("on", {}))
    if isinstance(triggers, dict):
        for event, spec in triggers.items():
            if isinstance(spec, dict):
                for key in ("paths", "paths-ignore"):
                    expect(key not in spec,
                           f"service-ci.yaml must not carry `{key}:` on `{event}`: "
                           f"a skipped workflow reports no status and blocks every "
                           f"required check forever")


def test_the_classifier_job_publishes_the_flag() -> None:
    job = _workflow()["jobs"]["docs-only"]
    outputs = job.get("outputs") or {}
    expect("docs_only" in outputs,
           "the docs-only job must publish a docs_only output")
    steps = job.get("steps") or []
    expect(any("fetch-depth" in str((s.get("with") or {})) for s in steps),
           "the docs-only job must check out full history: the before-SHA of a "
           "push is exactly the object a shallow clone does not have")
    expect(any("docs-only-diff.py" in str(s.get("run", "")) for s in steps),
           "the docs-only job must run the central classifier")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001 - a raising test is a failing test
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"docs-only fast path self-test: FAILED ({len(FAILURES)} assertion(s))")
        for f in FAILURES:
            print(f"::error::{f}")
        return 1

    print(
        f"docs-only fast path self-test: OK ({len(tests)} test(s)) - range "
        f"computation pinned for push, pull_request and the events that carry no "
        f"range; documentation, mixed, workflow-only, multi-commit and "
        f"undeterminable diffs all verified; rename, testdata, go:embed, merge "
        f"and revert shapes all run everything; and the wiring in service-ci.yaml "
        f"is asserted job by job."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
