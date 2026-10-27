#!/usr/bin/env python3
"""The fleet scripts read the hub the same way in both of its layouts.

The microservices hub is moving from category-first paths (services/<domain>/<repo>, gateways/,
shared/, ..., ci-workflows) to owner-first ones (core/<category>/... for the product, one
<client>/<category>/... folder per client), a few repositories at a time. A moved repository
leaves a relative compat link at its old path, and those links are going away. Every script that
reads the hub therefore has to find a repository at its real path, count a link and its target
once, and keep naming paths the legacy way, because that is how control files and reports spell
them.

Each scenario is built four ways - legacy only, owner-first only, mixed (the cores moved with
compat links, the client repositories not yet), and with a further client folder (coderaxis/) -
and a script must say exactly the same thing about all of them as it says about the legacy tree.
Covered: hub_layout.py itself, check-log-record-kinds.py, check-contract-classification.py,
check-log-schema.py's path resolution, check-docs-governance.py's control lookup,
hooks/install-ihq-guard.sh and bootstrap-frontend-tree.mjs. check-service-ports.py is covered in
test_service_ports.py, beside the rest of its suite.

Run: python3 scripts/test_hub_layout.py
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import hub_layout  # noqa: E402

FAILURES: list[str] = []
_TREES: list[tempfile.TemporaryDirectory] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def tmpdir() -> pathlib.Path:
    holder = tempfile.TemporaryDirectory()
    _TREES.append(holder)
    return pathlib.Path(holder.name).resolve()


def write(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def link(root: pathlib.Path, legacy: str, target: str) -> None:
    """A relative compat link, the way the hub makes them."""
    path = root / legacy
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(os.path.relpath(root / target, path.parent), target_is_directory=True)


# --- building one tree in each layout ----------------------------------------------------------

def repo_of(rel: str) -> str:
    """The repository a legacy path belongs to: services/<domain>/<repo>, else <category>/<repo>."""
    parts = rel.split("/")
    if parts[0] == "ci-workflows":
        return "ci-workflows"
    return "/".join(parts[:3] if parts[0] == "services" else parts[:2])


def owner_of(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1]
    for client in ("inboxxhq", "coderaxis"):
        if name.startswith(client + "-"):
            return client
    return hub_layout.CORE


def hub(tree: dict[str, str], layout: str) -> pathlib.Path:
    """`tree`, written in legacy spelling, laid out as:

    legacy       every repository at its category-first path
    owner-first  every repository under core/ or its client folder, no links
    mixed        the cores moved to core/ with a compat link left behind, InboxxHQ's repositories
                 not moved yet, and coderaxis/ - a client that never had a legacy path - only in
                 its own folder
    """
    root = tmpdir()
    links = set()
    for rel, text in tree.items():
        repo = repo_of(rel)
        owner = owner_of(repo)
        moved = (layout == "owner-first"
                 or (layout == "mixed" and owner in (hub_layout.CORE, "coderaxis")))
        if layout == "mixed" and owner == hub_layout.CORE:
            links.add(repo)
        write(root, {(f"{owner}/{rel}" if moved else rel): text})
    for repo in sorted(links):
        link(root, repo, f"{hub_layout.CORE}/{repo}")
    return root


LAYOUTS = ("legacy", "owner-first", "mixed")


# --- hub_layout.py -------------------------------------------------------------------------------

root = write(tmpdir(), {
    "core/services/org/org-core/go.mod": "",
    "core/ci-workflows/tools/x": "",
    "inboxxhq/gateways/inboxxhq-org-bff/go.mod": "",
    "coderaxis/tools/coderaxis-cli/go.mod": "",
    "services/ai/inboxxhq-ai-service/go.mod": "",
    "ci-workflows/tools/uuidscan/go.mod": "",            # a repository with a tools/ of its own
    "_archive/services/org/org-core-old/go.mod": "",
    ".worktrees/services/org/org-core-wt/go.mod": "",
    "notes/readme.md": "",
    "frontend/frontend-core/package.json": "",
})
link(root, "linked-client", "inboxxhq")
expect([p.name for p in hub_layout.owner_folders(root)] == ["core", "coderaxis", "inboxxhq"],
       f"owner_folders: core first, then each client by name, and nothing else; got "
       f"{[p.name for p in hub_layout.owner_folders(root)]}")

expect(hub_layout.detect_layout(write(tmpdir(), {"services/a/b/x.go": ""})) == "monorepo",
       "detect_layout: a category at the root is the legacy hub")
expect(hub_layout.detect_layout(write(tmpdir(), {"core/services/a/b/x.go": "",
                                                 "inboxxhq/services/a/c/x.go": ""})) == "monorepo",
       "detect_layout: core/<category> is the owner-first hub")
expect(hub_layout.detect_layout(write(tmpdir(), {"internal/services/x.go": "",
                                                 "cmd/tools/main.go": ""})) == "repo",
       "detect_layout: a single repository with internal/services/ must stay `repo`")
expect(hub_layout.detect_layout(write(tmpdir(), {"docs/x.md": ""}),
                                ("services", "tools")) == "repo",
       "detect_layout: only the caller's categories count, so a repo's docs/ is not a hub")
expect(hub_layout.detect_layout(write(tmpdir(), {"core/docs/x.md": ""}), ("services",)) == "repo",
       "detect_layout: core/ holding none of the caller's categories is not a hub")

for layout in LAYOUTS:
    root = hub({"shared/platform-shared-go/go.mod": "module x\n",
                "services/org/org-core/go.mod": "",
                "services/org/inboxxhq-org-service/go.mod": "",
                "services/org/inboxxhq-org-service/.hidden/go.mod": "",
                "gateways/inboxxhq-org-bff/go.mod": "",
                "ci-workflows/controls/log-schema.yaml": ""}, layout)
    h = hub_layout.Hub(root)
    got = h.resolve("shared/platform-shared-go/go.mod")
    expect(got.read_text() == "module x\n" and h.logical(got) == "shared/platform-shared-go/go.mod",
           f"Hub.resolve ({layout}): must find the moved file and map it back; got {got}")
    expect(h.resolve("docs/core-docs/missing.md") == root / "docs/core-docs/missing.md",
           f"Hub.resolve ({layout}): an absent path must come back in its legacy spelling")
    expect(h.logical(h.resolve("ci-workflows/controls/log-schema.yaml"))
           == "ci-workflows/controls/log-schema.yaml",
           f"Hub.resolve ({layout}): ci-workflows sits at core/ci-workflows once moved")
    repos = [h.logical(p) for p in h.glob("services/*/*")]
    expect(repos == ["services/org/inboxxhq-org-service", "services/org/org-core"],
           f"Hub.glob ({layout}): each repository once, legacy spelling, legacy order; got {repos}")
    hidden = [h.logical(p) for p in h.glob("services/org/*/.hidden", include_hidden=False)]
    expect(hidden == ["services/org/inboxxhq-org-service/.hidden"],
           f"Hub.glob ({layout}): a literal dot name still matches; got {hidden}")
    dotted = [h.logical(p) for p in h.glob("services/org/inboxxhq-org-service/*",
                                            include_hidden=False)]
    expect(dotted == ["services/org/inboxxhq-org-service/go.mod"],
           f"Hub.glob ({layout}): include_hidden=False must keep glob.glob's rule; got {dotted}")
    files = sorted(h.logical(p) for p in h.rglob(("services", "gateways", "shared"), "go.mod"))
    expect(files == ["gateways/inboxxhq-org-bff/go.mod",
                     "services/org/inboxxhq-org-service/.hidden/go.mod",
                     "services/org/inboxxhq-org-service/go.mod", "services/org/org-core/go.mod",
                     "shared/platform-shared-go/go.mod"],
           f"Hub.rglob ({layout}): every file once, through no compat link; got {files}")

legacy = write(tmpdir(), {f"services/d{i}/r{j}/x.go": "" for i in range(3) for j in range(3)}
               | {"gateways/g/x.go": "", "shared/s/x.go": ""})
before = [p for base in ("services", "gateways", "shared") for p in (legacy / base).rglob("*.go")]
expect(list(hub_layout.Hub(legacy).rglob(("services", "gateways", "shared"), "*.go")) == before,
       "Hub.rglob: a legacy-only tree must be walked in exactly the order the scripts walked it")
expect(hub_layout.Hub(legacy).glob("services/*/*") == sorted(legacy.glob("services/*/*")),
       "Hub.glob: a legacy-only tree must come back exactly as sorted(root.glob(pattern))")

h = hub_layout.Hub(hub({"services/org/org-core/x.go": ""}, "owner-first"))
try:
    h.logical(pathlib.Path("/elsewhere/x.go"))
    expect(False, "Hub.logical: a path outside the root must raise ValueError")
except ValueError:
    pass

root = write(tmpdir(), {"core/ci-workflows/scripts/x.py": "", "core/services/org/org-core/x": "",
                        ".generated-sources/ci-workflows/scripts/x.py": ""})
link(root, "ci-workflows", "core/ci-workflows")
for script, want in ((root / "core/ci-workflows/scripts/x.py", root),
                     (root / "ci-workflows/scripts/x.py", root),
                     (root / ".generated-sources/ci-workflows/scripts/x.py",
                      root / ".generated-sources")):
    expect(hub_layout.default_root(script) == want,
           f"default_root({script}) should be {want}, got {hub_layout.default_root(script)}")


# --- check-log-record-kinds.py -------------------------------------------------------------------

LRK_CONTROL = yaml.safe_dump({
    "records": [{"name": "http_request_completed", "constructor": "HTTPRequestCompleted"},
                {"name": "grpc_call_completed", "constructor": "GRPCCallCompleted"}],
    "adoption": {"pending": ["grpc_call_completed"],
                 "event_key_allowlist": ["services/org/inboxxhq-org-service/internal/webhook.go"]},
    "sole_implementations": {"emitters": [{
        "record": "http_request_completed",
        "owner": "shared/platform-shared-go/platform/ginmiddleware",
        "name_pattern": r"\w*(AccessLog|RequestLog|LoggingMiddleware|LoggerMiddleware)\w*"}],
        "allowlist": []},
})

LRK_TREE = {
    "ci-workflows/controls/log-schema.yaml": LRK_CONTROL,
    # The catalogue's own package, exempt from every rule, and the middleware's owner.
    "shared/platform-shared-go/logging/records.go":
        'package logging\n\nvar raw = map[string]any{"event_name": "x"}\n',
    "shared/platform-shared-go/platform/ginmiddleware/access_log.go":
        "package ginmiddleware\n\nfunc AccessLogMiddleware() {}\n",
    # The only emitter of http_request_completed: if it is not read, the ratchet fails.
    "services/org/org-core/application/emit.go":
        "package app\n\nfunc f() { logging.Emit(ctx, logging.HTTPRequestCompleted{}) }\n",
    # Allowlisted by its legacy path, which must keep matching wherever the file sits.
    "services/org/inboxxhq-org-service/internal/webhook.go":
        'package http\n\nvar m = map[string]string{"event": "ping"}\n',
    "gateways/inboxxhq-org-bff/internal/frame.go":
        'package ws\n\nvar m = map[string]string{"event": "frame"}\n',
    "tools/inboxxhq-cli/internal/mw.go": "package mw\n\nfunc RequestLogger() {}\n",
    # Never read: tests, vendored code.
    "services/org/org-core/application/emit_test.go":
        'package app\n\nvar m = map[string]int{"event": 1}\n',
    "services/org/org-core/vendor/x/y.go": 'package y\n\nvar m = map[string]int{"event": 1}\n',
}
CODERAXIS_GO = {"services/crm/coderaxis-crm-service/internal/h.go":
                'package h\n\nvar m = map[string]string{"event": "lead"}\n'}


def record_kinds(root: pathlib.Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(HERE / "check-log-record-kinds.py"),
                           "--repo-root", str(root), *args],
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


reference = record_kinds(hub(LRK_TREE, "legacy"))
expect(reference[0] == 1
       and "gateways/inboxxhq-org-bff/internal/frame.go:3: `event`" in reference[1]
       and "tools/inboxxhq-cli/internal/mw.go:3: RequestLogger re-implements" in reference[1]
       and "webhook.go" not in reference[1] and "has no emitter" not in reference[1]
       and reference[1].count("::error::") == 2,
       f"record kinds: the legacy reference must find exactly its two defects; got {reference}")
for layout in LAYOUTS[1:]:
    got = record_kinds(hub(LRK_TREE, layout))
    expect(got == reference, f"record kinds ({layout}): must say exactly what it says about the "
                             f"legacy tree.\n    legacy: {reference}\n    {layout}: {got}")

with_client = record_kinds(hub(LRK_TREE | CODERAXIS_GO, "legacy"))
expect("services/crm/coderaxis-crm-service/internal/h.go:3" in with_client[1],
       f"record kinds: the coderaxis reference must carry the coderaxis finding; got {with_client}")
for layout in LAYOUTS[1:]:
    got = record_kinds(hub(LRK_TREE | CODERAXIS_GO, layout))
    expect(got == with_client, f"record kinds ({layout} + coderaxis/): a client folder must be "
                               f"read and reported by legacy path.\n    legacy: {with_client}\n"
                               f"    {layout}: {got}")

# Repo layout reads one checkout and is unchanged by where it sits.
for layout in LAYOUTS:
    root = hub(LRK_TREE, layout)
    repo = hub_layout.Hub(root).resolve("gateways/inboxxhq-org-bff")
    got = record_kinds(repo, "--layout", "repo",
                       "--control", str(hub_layout.Hub(root).resolve(
                           "ci-workflows/controls/log-schema.yaml")))
    expect(got[0] == 1 and "inboxxhq-org-bff/internal/frame.go:3" in got[1]
           and got[1].count("::error::") == 1 and "has no emitter" not in got[1],
           f"record kinds ({layout}): repo layout must read one repository as before; got {got}")


# --- check-contract-classification.py ------------------------------------------------------------

def catalog_entry(name: str, repo: str, rest: bool) -> str:
    return yaml.safe_dump({"name": name, "deployable_repository": {"name": repo},
                           "interfaces": {"rest": {"base_path": "/"}} if rest else {}})


CC_TREE = {
    "docs/core-docs/catalog/services/org-service.yaml":
        catalog_entry("org-service", "inboxxhq-org-service", True),
    "docs/core-docs/catalog/services/org-bff.yaml":
        catalog_entry("org-bff", "inboxxhq-org-bff", False),
    "docs/core-docs/catalog/services/auth-core.yaml": catalog_entry("auth-core", "auth-core", True),
    "docs/core-docs/catalog/services/ghost-service.yaml":
        catalog_entry("ghost-service", "inboxxhq-ghost-service", True),
    "services/org/inboxxhq-org-service/docs/openapi.json": "{}\n",
    "gateways/inboxxhq-org-bff/docs/openapi.json": "{}\n",
    "services/auth/auth-core/README.md": "core, no spec\n",
    # A dot-directory is not a checkout: ghost-service stays "not checked out".
    "services/.wt-ghost/inboxxhq-ghost-service/docs/openapi.json": "{}\n",
}
CODERAXIS_CC = {
    "docs/core-docs/catalog/services/crm-service.yaml":
        catalog_entry("crm-service", "coderaxis-crm-service", True),
    "services/crm/coderaxis-crm-service/docs/openapi.json": "{}\n",
}


def classification(root: pathlib.Path) -> tuple[int, str]:
    catalog = hub_layout.Hub(root).resolve("docs/core-docs/catalog/services")
    proc = subprocess.run([sys.executable, str(HERE / "check-contract-classification.py"),
                           "--catalog", str(catalog), "--workspace", str(root),
                           "--allowlist", str(root / "no-allowlist.yaml")],
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


reference = classification(hub(CC_TREE, "legacy"))
expect(reference[0] == 1
       and "3 service(s) checked, 2 violation(s), 1 not checked out" in reference[1]
       and "not checked out: ghost-service" in reference[1],
       f"classification: the legacy reference is wrong; got {reference}")
for layout in LAYOUTS[1:]:
    got = classification(hub(CC_TREE, layout))
    expect(got == reference, f"classification ({layout}): must judge the same repositories.\n"
                             f"    legacy: {reference}\n    {layout}: {got}")
with_client = classification(hub(CC_TREE | CODERAXIS_CC, "legacy"))
expect("4 service(s) checked" in with_client[1],
       f"classification: the coderaxis reference must check crm-service; got {with_client}")
for layout in LAYOUTS[1:]:
    got = classification(hub(CC_TREE | CODERAXIS_CC, layout))
    expect(got == with_client, f"classification ({layout} + coderaxis/): a deployable in a client "
                               f"folder must be found.\n    legacy: {with_client}\n"
                               f"    {layout}: {got}")


# --- check-log-schema.py: where the three descriptions are read from --------------------------

log_schema = load("check_log_schema", "check-log-schema.py")


class _Stop(Exception):
    pass


def log_schema_paths(root: pathlib.Path) -> dict[str, pathlib.Path]:
    """The three paths main() reads with only --repo-root given; nothing is parsed or run."""
    seen: dict[str, pathlib.Path] = {}

    def control(path):
        seen["control"] = path
        return {}

    def module(path):
        seen["module"] = path
        return {}

    def standard(path):
        seen["standard"] = path
        raise _Stop

    log_schema.load_control = control
    log_schema.dump_go_registry = module
    log_schema.load_standard_version = standard
    argv, sys.argv = sys.argv, ["check-log-schema.py", "--repo-root", str(root)]
    try:
        log_schema.main()
    except _Stop:
        pass
    finally:
        sys.argv = argv
    return seen


LS_TREE = {"ci-workflows/controls/log-schema.yaml": "c",
           "docs/core-docs/standards/observability/log-schema-standard.md": "s",
           "shared/platform-shared-go/go.mod": "m"}
for layout in LAYOUTS:
    seen = log_schema_paths(hub(LS_TREE, layout))
    files = {"control": seen.get("control"), "standard": seen.get("standard"),
             "module": seen["module"] / "go.mod" if "module" in seen else None}
    got = {k: p.read_text() if p and p.exists() else None for k, p in files.items()}
    expect(got == {"control": "c", "module": "m", "standard": "s"},
           f"check-log-schema ({layout}): --repo-root must find all three; got {seen}")
seen = log_schema_paths(tmpdir())
expect(str(seen.get("control", "")).endswith("ci-workflows/controls/log-schema.yaml"),
       f"check-log-schema: an absent control must be named by its legacy path; got {seen}")


# --- check-docs-governance.py: the log-schema control, found from a docs checkout ----------------

docs_governance = load("check_docs_governance", "check-docs-governance.py")
FIXTURE = HERE / "testdata" / "docs-governance-fixture"
HOMES = ["docs/core-docs/standards/observability/log-schema-standard.md"]


def homes(docs_root: pathlib.Path):
    shutil.copytree(FIXTURE, docs_root, dirs_exist_ok=True)
    return docs_governance.DocsRepo(docs_root, {}, dt.date(2026, 10, 27), 0).log_schema_homes


control = yaml.safe_dump({"normative_homes": HOMES})
root = write(tmpdir(), {"ci-workflows/controls/log-schema.yaml": control})
expect(homes(root / "docs/core-docs") == HOMES,
       "docs governance: legacy ci-workflows must be found")
# The case this exists for: ci-workflows moved to core/ and its compat link removed, core-docs
# still at its legacy path. Before, the control was not found and DOC-0025 silently skipped.
root = write(tmpdir(), {"core/ci-workflows/controls/log-schema.yaml": control,
                        "core/services/org/org-core/go.mod": ""})
expect(homes(root / "docs/core-docs") == HOMES,
       "docs governance: core/ci-workflows must be found from a legacy docs/core-docs once the "
       "ci-workflows compat link is gone")
expect(homes(root / "core/docs/core-docs") == HOMES,
       "docs governance: core/ci-workflows must be found from core/docs/core-docs")
expect(homes(tmpdir() / "core-docs") is None,
       "docs governance: no control anywhere must stay None (a skip), not an empty list")


# --- hooks/install-ihq-guard.sh ------------------------------------------------------------------

FAKE_IHQ = "#!/bin/sh\necho \"fake ihq $*\"\n"
for layout, hook, cli in (
        ("legacy", "ci-workflows/hooks", "tools/inboxxhq-cli"),
        ("owner-first", "core/ci-workflows/hooks", "inboxxhq/tools/inboxxhq-cli")):
    root = tmpdir()
    (root / hook).mkdir(parents=True)
    shutil.copy2(REPO / "hooks" / "install-ihq-guard.sh", root / hook)
    write(root, {f"{cli}/ihq": FAKE_IHQ, "core/services/org/org-core/go.mod": ""})
    (root / cli / "ihq").chmod(0o755)
    proc = subprocess.run(["bash", str(root / hook / "install-ihq-guard.sh"), "--dry-run"],
                          capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(root)})
    expect(proc.returncode == 0 and "fake ihq git hooks install --fleet --dry-run" in proc.stdout,
           f"install-ihq-guard.sh ({layout}): must find the hub and ihq at {cli}; got "
           f"{proc.returncode} {proc.stdout} {proc.stderr}")


# --- bootstrap-frontend-tree.mjs -----------------------------------------------------------------

node = shutil.which("node")
if node is None:
    print("note: node is not installed; bootstrap-frontend-tree.mjs not exercised")
else:
    for layout, scripts in (("legacy", "ci-workflows/scripts"),
                            ("owner-first", "core/ci-workflows/scripts")):
        root = write(tmpdir(), {"frontend/frontend-core/shared-packages.manifest.json":
                                '{"packages": {}, "localDev": {"symlinks": []}}\n'})
        (root / scripts).mkdir(parents=True)
        shutil.copy2(HERE / "bootstrap-frontend-tree.mjs", root / scripts)
        proc = subprocess.run([node, str(root / scripts / "bootstrap-frontend-tree.mjs")],
                              capture_output=True, text=True, check=False)
        expect(proc.returncode == 0 and f"(cd {root / 'frontend'} &&" in proc.stdout,
               f"bootstrap-frontend-tree.mjs ({layout}): must treat {root} as the tree; got "
               f"{proc.returncode} {proc.stdout} {proc.stderr}")


if FAILURES:
    print(f"hub layout tests: {len(FAILURES)} FAILURE(S)")
    for failure in FAILURES:
        print(f"  - {failure}")
    raise SystemExit(1)
print("hub layout tests: all assertions passed")
