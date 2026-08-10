#!/usr/bin/env python3
"""Fail when the record-kind catalogue and the fleet disagree.

``check-log-schema.py`` is the sibling of this script and it checks a different thing. Every
comparison it makes is between two *descriptions* of the schema: the control file against the Go
registry against the standard's frontmatter. That is necessary and it is not sufficient, and the
gap between the two is not theoretical - it is the reason this file exists.

Under schema v1 the vocabulary declared eleven record kinds. Two had emitters. Nine could be
selected on by a dashboard and produced by nothing. Simultaneously, six kinds that the vocabulary
had never heard of were being emitted under the key ``event`` - including the gRPC access record on
roughly 27 service roots - and because ``event`` is not in Alloy's promotion allowlist, the kind of
the platform's second most numerous record could not be filtered without parsing every line.

Neither fact failed a build, and neither could have, because nothing in CI had ever read a service.
The registry was diffed against itself.

This script reads the fleet. It enforces five things:

  1. **One discriminator.** ``event_name`` names a record kind. ``event`` does not, and a map
     literal using it as a key fails unless the file is on the control file's allowlist for the
     genuine other meanings - a provider's webhook payload, a websocket frame type.

  2. **Kinds are emitted through the catalogue.** A call site does not set ``event_name`` by hand.
     It constructs one of the typed records in ``platform-shared-go/logging`` and passes it to
     ``logging.Emit``, so the fields a kind requires are supplied by the compiler rather than
     remembered by the author (Core ADR-0104 D3).

  3. **The adoption ratchet only tightens.** ``adoption.pending`` in the control file lists the
     kinds the fleet does not emit yet. A kind on the list that gains a call site must leave the
     list; a kind off the list with no call site fails. The list may only shrink.

  4. **One implementation of each access log.** The middleware that writes a record per request is
     the code most often copied rather than imported, and a copy is not caught by rules 1-3 while it
     stays unreferenced. ``sole_implementations`` names the package that owns each; a matching
     function declared anywhere else fails, called or not.

  5. **One logger interface.** An interface named ``*Logger`` must not declare a log-level method
     that takes no ``context.Context``. This rule is about a record's *correlation fields*, which
     rules 1-4 say nothing about: a line written through a context-less logger satisfies the schema
     field for field and arrives in Loki with no ``trace_id`` and no ``correlation_id``, so it cannot
     be joined to the request that produced it. Twenty-two such interfaces were in use when this rule
     was written, four of them inside ``platform-shared-go`` itself, each documented as
     "intentionally tiny so services can adapt". What they produced was five copies of the same
     adapter in five repositories, bridging to an ``auditevent`` logger field that no code ever read
     (Core ADR-0101 D2). A ``type Logger = logging.Logger`` alias is not a declaration and passes -
     aliasing is the fix.

TWO LAYOUTS, ONE CHECKER. The trees above live in one working copy on a workstation and in one
repository each in CI, where ``service-ci.yaml`` checks out a single service. The rules that read a
file work in both. Rule 3 does not: "no emitter anywhere in the fleet" is not a question a single
repository can answer, and asking it there would fail every service for the kinds it does not
happen to emit. So the ratchet runs where the fleet is visible and announces itself skipped where
it is not, rather than being quietly approximated.

Usage:

    python3 ci-workflows/scripts/check-log-record-kinds.py
    python3 ci-workflows/scripts/check-log-record-kinds.py --repo-root /path/to/monorepo
    python3 ci-workflows/scripts/check-log-record-kinds.py --repo-root . --layout repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it; local runs get a usable message
    sys.exit("::error::check-log-record-kinds.py requires PyYAML (pip install pyyaml)")

CONTROL_PATH = Path("ci-workflows/controls/log-schema.yaml")

# The package that owns the catalogue. Its own files are exempt from every rule below: it is where
# event_name is legitimately written, and where the Record implementations live.
LOGGING_PACKAGE = Path("shared/platform-shared-go/logging")

# Trees that are scanned. Anything outside them is not a platform service.
SCAN_ROOTS = ("services", "gateways", "shared", "tools")

# Directories that never contain a platform emitter.
#
# Any dot-prefixed component goes too, and that one is load-bearing rather than tidiness. The
# workflows in this repository check tooling out INTO the repository under test - `.archcheck`,
# `.gateway-tools`, and `.log-record-tools` for this very script - and ci-workflows ships Go
# fixtures under `fixtures/` that are deliberately non-conforming. Without this, every service in
# the fleet would fail on the checker's own test data the moment the checker was wired up.
SKIP_DIRS = {"node_modules", "vendor", "testdata", "archived", "docs", "generated"}

# Rule 5. An interface whose name ends in Logger, and a method on it named for a log level. The
# `params` group is what decides: the canonical shape leads with a context, a narrower duplicate
# does not.
LOGGER_INTERFACE = re.compile(r"^\s*type\s+(?P<name>\w*Logger)\s+interface\s*\{", re.M)
LOGGER_METHOD = re.compile(
    r"^(?P<name>Log(?:Debug|Info|Warn|Error)(?:Ctx)?|Debug|Info|Warn|Error)"
    r"\s*\((?P<params>[^)]*)\)")


def skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(p in SKIP_DIRS or p.startswith(".") for p in rel_parts)

# `"event":` or `"event" :` used as a map key.
EVENT_KEY = re.compile(r'"event"\s*:')

# A record kind written by hand rather than constructed: `"event_name":` or `FieldEventName:`.
RAW_EVENT_NAME = re.compile(r'"event_name"\s*:|logging\.FieldEventName\s*:|FieldEventName\s*:')


def cannot_run(message: str) -> None:
    """Exit 2: the checker could not do its job.

    Deliberately not exit 1 and deliberately not exit 0. A gate that finds nothing because it was
    pointed at the wrong tree reports exactly what a clean repository reports, and the fleet has
    already been taught what that costs - `check-log-schema.py` passed for the life of schema v1
    while nine of eleven declared kinds had no emitter, because it was only ever comparing two
    descriptions. The same distinction is why the architecture checker reserves 2.
    """
    print(f"::error::{message}")
    sys.exit(2)


def fail(messages: list[str]) -> None:
    for message in messages:
        print(f"::error::{message}")
    print(f"\ncheck-log-record-kinds: {len(messages)} disagreement(s) between the record-kind "
          f"catalogue and the fleet.")
    print("The catalogue is in ci-workflows/controls/log-schema.yaml; the emitters are the typed "
          "records in platform-shared-go/logging. See Core ADR-0104.")
    sys.exit(1)


def load_control(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"::error::log schema control file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        sys.exit(f"::error::{path}: expected a mapping at the top level")
    return data


def detect_layout(root: Path) -> str:
    """`monorepo` when the tree holds many repositories, `repo` when it is one."""
    return "monorepo" if any((root / r).is_dir() for r in SCAN_ROOTS) else "repo"


def go_files(root: Path, layout: str) -> list[Path]:
    bases = [root / r for r in SCAN_ROOTS if (root / r).is_dir()] if layout == "monorepo" else [root]
    out: list[Path] = []
    for base in bases:
        for path in base.rglob("*.go"):
            if skipped(path.relative_to(root).parts):
                continue
            if path.name.endswith("_test.go"):
                continue
            if path.name.endswith(".pb.go") or path.name.endswith("_grpc.pb.go"):
                continue
            out.append(path)
    return out


def path_matches(rel: str, entry: str) -> bool:
    """Does the scanned file `rel` denote the control file's `entry`?

    Entries are written monorepo-relative, because that is the form a reviewer can locate. Under a
    single-repo checkout the same file is missing the `services/<domain>/` prefix, so the entry is
    matched by its tail. The repository name is part of that tail either way - see `logical` - so
    two services holding an identically named file at an identical internal path stay distinct, and
    allowlisting one does not allowlist the other.
    """
    return rel == entry or entry.endswith("/" + rel)


def package_prefixes(owner: str) -> list[str]:
    """The owner package path, then its tails, for matching under either layout.

    Two components is the floor. A bare trailing name like `grpccommon` would exempt any repository
    that happened to contain a directory of that name, which is the sort of accidental hole that
    leaves a gate reporting green for the case it was written to catch.
    """
    parts = [p for p in owner.strip("/").split("/") if p]
    return ["/".join(parts[i:]) for i in range(max(len(parts) - 1, 1))]


def under_package(rel: str, owner: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in package_prefixes(owner))


def logical(path: Path, root: Path, prefix: str) -> str:
    """The path to report and to match control-file entries against.

    In monorepo layout that is simply the path from the root, which is what the control file
    records. In repo layout the checkout has been stripped of everything above it, so the
    repository's own directory name is put back: `actions/checkout` names it after the repository,
    which restores the one component that makes `internal/adapters/.../webhook_handler.go`
    attributable to a service rather than to six of them.
    """
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        return str(path)
    return f"{prefix}/{rel}" if prefix else rel


def in_logging_package(rel: str) -> bool:
    return under_package(rel, str(LOGGING_PACKAGE))


def check_discriminator(corpus: list[tuple[str, str]], allowlist: set[str]) -> list[str]:
    """Rule 1 and 2: one discriminator, and it is not written by hand."""
    problems: list[str] = []
    for rel, text in corpus:
        if in_logging_package(rel):
            continue
        allowed = any(path_matches(rel, entry) for entry in allowlist)

        for number, line in enumerate(text.splitlines(), start=1):
            # A line comment is documentation, not an emitter. Worth the two lines: the realtime
            # gateway documents its websocket frame shape as a JSON literal in a comment, and
            # flagging that taught the reader the rule is approximate, which is how a check starts
            # being worked around instead of read.
            if line.lstrip().startswith("//"):
                continue
            if EVENT_KEY.search(line) and not allowed:
                problems.append(
                    f"{rel}:{number}: `event` is not a record-kind discriminator. The catalogue "
                    f"uses `event_name`, which Alloy promotes to structured metadata; `event` stays "
                    f"in the line body where a query has to parse every line to reach it. Emit a "
                    f"catalogued record via logging.Emit, or add this file to "
                    f"adoption.event_key_allowlist if the key means something else here")
            if RAW_EVENT_NAME.search(line):
                problems.append(
                    f"{rel}:{number}: event_name is set by hand. A record kind is produced by its "
                    f"typed constructor in platform-shared-go/logging and passed to logging.Emit, "
                    f"so the fields the kind requires are supplied by the compiler instead of "
                    f"remembered (Core ADR-0104 D3)")
    return problems


def check_logger_interfaces(corpus: list[tuple[str, str]]) -> list[str]:
    """Rule 5: one logger interface on the platform.

    Core ADR-0101 made logging.Logger canonical, and the fleet still ended up with five logger
    interfaces, because nothing stopped a package from declaring its own. Four of them lived in
    platform-shared-go itself - grpcauth.Logger, grpcsecurity.Logger, auditevent.Logger,
    events.ConsumerLogger - each justified as "intentionally tiny so services can adapt".

    What that produced is the reason this rule exists. Every one of the four omitted the context
    parameter, so a record written through them carried no trace_id, no correlation_id and no
    org_id. Services could not pass the canonical logger to some of them, so they wrote adapters:
    auditevent.Logger alone collected five near-identical LoggerAdapter copies in five
    repositories, and one service wrapped an adapter in a second adapter. The emitter never called
    the logger.

    The signal is a method named for a log level that does NOT take a context. Info(ctx, ...) is
    the canonical shape and passes; Info(msg, fields) or LogInfo(...) is a narrower duplicate. A
    `type Logger = logging.Logger` alias is not an interface declaration and does not match, which
    is deliberate - aliasing is the fix this rule wants.
    """
    problems: list[str] = []
    for rel, text in corpus:
        if in_logging_package(rel):
            continue
        for match in LOGGER_INTERFACE.finditer(text):
            name = match.group("name")
            body, _ = interface_body(text, match.end())
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                method = LOGGER_METHOD.match(stripped)
                if not method or "context.Context" in method.group("params"):
                    continue
                number = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{rel}:{number}: `{name}` re-declares the logger interface, and "
                    f"`{method.group('name')}` on it takes no context. A record written through it "
                    f"cannot carry trace_id, correlation_id or org_id, and callers holding the "
                    f"canonical logger have to write an adapter to reach it - which is how this "
                    f"platform acquired five logger interfaces and five copies of the same adapter. "
                    f"Use logging.Logger, or alias it: `type {name} = logging.Logger` "
                    f"(Core ADR-0101 D2)")
                break
    return problems


def interface_body(text: str, start: int) -> tuple[str, int]:
    """The source between an interface's braces, and the index just past the closing one."""
    depth, index = 1, start
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    return text[start:index - 1], index


def check_sole_implementations(control: dict, corpus: list[tuple[str, str]]) -> list[str]:
    """Rule 4: shared access-log middleware is imported, never copied."""
    problems: list[str] = []
    block = control.get("sole_implementations") or {}
    emitters = block.get("emitters") if isinstance(block, dict) else None

    rules = [r for r in (emitters or [])
             if isinstance(r, dict) and r.get("name_pattern") and r.get("owner")]
    if not rules:
        return problems

    allowlist = set(block.get("allowlist") or [])

    compiled = [
        (re.compile(r"^func\s+(" + r["name_pattern"] + r")\s*\("), r["owner"], r.get("record", "?"))
        for r in rules
    ]

    for rel, text in corpus:
        if any(path_matches(rel, entry) for entry in allowlist):
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, owner, record in compiled:
                match = pattern.match(line)
                if not match or under_package(rel, owner):
                    continue
                problems.append(
                    f"{rel}:{number}: {match.group(1)} re-implements the {record} emitter that "
                    f"{owner} owns. Call the shared one instead - a copy drifts from the catalogue "
                    f"the moment either side changes, and an unreferenced copy drifts silently "
                    f"while its own tests keep passing (Core ADR-0104 D5)")
    return problems


def check_adoption(control: dict, corpus: list[tuple[str, str]]) -> list[str]:
    """Rule 3: the ratchet only tightens. Fleet-wide layout only - see the module docstring."""
    problems: list[str] = []
    records = [r for r in control.get("records") or [] if isinstance(r, dict)]
    adoption = control.get("adoption") or {}
    pending = set(adoption.get("pending") or [])

    declared = {r["name"] for r in records if "name" in r}
    for name in sorted(pending - declared):
        problems.append(f"adoption.pending lists '{name}', which is not a declared record kind")

    constructors = {r["name"]: r.get("constructor") for r in records if "name" in r}

    # A construction site is `logging.<Constructor>{`. The catalogue types are structs, so this is
    # how every legitimate emitter reaches one.
    corpus = [(rel, text) for rel, text in corpus if not in_logging_package(rel)]

    for name, constructor in sorted(constructors.items()):
        if not constructor:
            continue
        pattern = re.compile(r"\blogging\." + re.escape(constructor) + r"\s*\{")
        sites = [rel for rel, text in corpus if pattern.search(text)]

        if name in pending and sites:
            problems.append(
                f"record kind '{name}' is in adoption.pending but is now emitted by "
                f"{sites[0]}{' and others' if len(sites) > 1 else ''}. Remove it from the list: "
                f"the ratchet only shrinks")
        if name not in pending and not sites:
            problems.append(
                f"record kind '{name}' has no emitter anywhere in the fleet and is not in "
                f"adoption.pending. Either adopt it or declare the debt")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=None, help="tree to scan")
    parser.add_argument("--control", default=None, help=f"path to {CONTROL_PATH.name}")
    parser.add_argument("--layout", choices=("auto", "monorepo", "repo"), default="auto",
                        help="whether the tree holds many repositories or one (default: detect)")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[2]
    control_path = Path(args.control).resolve() if args.control else root / CONTROL_PATH
    if not root.is_dir():
        cannot_run(f"--repo-root is not a directory: {root}")

    layout = detect_layout(root) if args.layout == "auto" else args.layout
    control = load_control(control_path)
    paths = go_files(root, layout)

    # An empty scan is the one result that must never be reported as a pass. Under `repo` layout a
    # mistyped --repo-root produces exactly the output a conforming repository produces, and this
    # gate exists precisely because a check that reads nothing had been mistaken for a check that
    # found nothing for the whole life of schema v1.
    if not paths:
        cannot_run(f"no Go files under {root} (layout={layout}). A gate that reads nothing must "
                   f"not be indistinguishable from a gate that found nothing - check --repo-root "
                   f"and --layout")

    prefix = "" if layout == "monorepo" else root.name
    corpus: list[tuple[str, str]] = []
    for path in paths:
        try:
            corpus.append((logical(path, root, prefix),
                           path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    allowlist = set((control.get("adoption") or {}).get("event_key_allowlist") or [])
    problems = (check_discriminator(corpus, allowlist)
                + check_sole_implementations(control, corpus)
                + check_logger_interfaces(corpus))
    if layout == "monorepo":
        problems += check_adoption(control, corpus)
    if problems:
        fail(problems)

    records = [r for r in control.get("records") or [] if isinstance(r, dict)]
    pending = set((control.get("adoption") or {}).get("pending") or [])
    summary = (f"check-log-record-kinds: {len(corpus)} Go files carry one discriminator, "
               f"emitted through the catalogue and not re-implemented locally.")
    if layout == "monorepo":
        adopted = len(records) - len(pending)
        summary += (f" {adopted} of {len(records)} record kinds have an emitter, "
                    f"{len(pending)} declared pending.")
    else:
        summary += " Adoption ratchet skipped: it is a property of the fleet, not of one repository."
    print(summary)


if __name__ == "__main__":
    main()
