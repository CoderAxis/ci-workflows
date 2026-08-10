#!/usr/bin/env python3
"""Fail when the log record contract disagrees with itself.

The contract exists in three places, and it has to:

  * ``docs/core-docs/standards/observability/log-schema-standard.md`` - the normative document.
    People read this one.
  * ``ci-workflows/controls/log-schema.yaml`` - the machine-readable twin. Grafana Alloy derives
    its structured-metadata allowlist from it, the docs governance checker reads its
    ``normative_homes``, and the compliance evidence pack reads its PII classes.
  * ``shared/platform-shared-go/logging/schema.go`` - the registry the Go logger actually enforces
    at runtime, mirrored because a published Go module cannot read a YAML file in another
    repository at init time.

Three copies is the correct design and also the exact hazard ADR-0101 was written about. The
previous log schema lived in one place, a Markdown table, and had never matched a single emitted
line: it required ``timestamp`` and ``message`` while every service emitted logrus's default
``time`` and ``msg``. Nothing compared the two, so nothing said so, for the life of the platform.

This script is that comparison. It runs ``logschema dump`` against the real package and diffs the
result against the control file, so a field can only be added, renamed, retyped, reclassified or
retiered in both at once. It also checks the version triple: the ``version`` in the control file,
the ``log_schema_version`` in the standard's frontmatter, and ``SchemaVersion`` in Go are one
number on the wire and must be one number in the tree.

Usage:

    python3 ci-workflows/scripts/check-log-schema.py
    python3 ci-workflows/scripts/check-log-schema.py --repo-root /path/to/monorepo

The three paths can also be given individually, because in CI the three repositories are checked
out side by side rather than in the monorepo layout:

    python3 check-log-schema.py \\
        --control controls/log-schema.yaml \\
        --standard ../core-docs/standards/observability/log-schema-standard.md \\
        --go-module ../platform-shared-go
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it; local runs get a usable message
    sys.exit("::error::check-log-schema.py requires PyYAML (pip install pyyaml)")

CONTROL_PATH = Path("ci-workflows/controls/log-schema.yaml")
STANDARD_PATH = Path("docs/core-docs/standards/observability/log-schema-standard.md")
GO_MODULE_PATH = Path("shared/platform-shared-go")
DUMP_PACKAGE = "./cmd/logschema"

# Properties compared per field. `notes`, `enum` and `example` are documentation and live only in
# the control file: the Go mirror does not carry them and should not, because a Go constant is not
# where a reviewer looks for the reason a field exists.
FIELD_PROPERTIES = ("type", "presence", "pii", "loki", "owner")


def fail(messages: list[str]) -> None:
    for message in messages:
        print(f"::error::{message}")
    print(f"\ncheck-log-schema: {len(messages)} disagreement(s) between the Go registry, the "
          f"control file and the standard.")
    print("Both halves change in the same pull request. See ADR-0101 D3.")
    sys.exit(1)


def load_control(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"::error::log schema control file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        sys.exit(f"::error::{path}: expected a mapping at the top level")
    return data


def load_standard_version(path: Path) -> tuple:
    """Return (version|None, error|None) from the standard's frontmatter."""
    if not path.exists():
        return None, f"{path} not found; the machine twin has no human original"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^log_schema_version:\s*(\d+)\s*$", text, re.MULTILINE)
    if not match:
        return None, (f"{path}: no `log_schema_version` in frontmatter; the emitted "
                      f"schema_version has no declared human source")
    return int(match.group(1)), None


def dump_go_registry(module: Path) -> dict:
    if not (module / "go.mod").exists():
        sys.exit(f"::error::{module} is not a Go module")
    try:
        proc = subprocess.run(["go", "run", DUMP_PACKAGE, "dump"], cwd=module,
                              capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError:
        sys.exit("::error::go toolchain not found; check-log-schema.py needs it to read the "
                 "registry the logger actually enforces")
    if proc.returncode != 0:
        sys.exit(f"::error::`go run {DUMP_PACKAGE} dump` failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def compare_fields(control: dict, go: dict) -> list[str]:
    problems: list[str] = []
    control_fields = {f["name"]: f for f in control.get("fields") or []
                      if isinstance(f, dict) and "name" in f}
    go_fields = {f["name"]: f for f in go.get("fields") or []}

    # Collector-owned fields are declared in the control file and deliberately absent from Go:
    # the package must be structurally unable to emit `pod` or `cluster`. Their absence is the
    # design, so they are excluded from the "missing in Go" check and asserted absent instead.
    collector_owned = {name for name, f in control_fields.items() if f.get("owner") == "collector"}
    application_owned = set(control_fields) - collector_owned

    for name in sorted(application_owned - set(go_fields)):
        problems.append(f"field '{name}' is in {CONTROL_PATH} but not in logging/schema.go; the "
                        f"logger cannot emit a field it does not know about")
    for name in sorted(set(go_fields) - set(control_fields)):
        problems.append(f"field '{name}' is in logging/schema.go but not in {CONTROL_PATH}; Alloy "
                        f"and the evidence pack read the control file, so this field is invisible "
                        f"to both")
    for name in sorted(collector_owned & set(go_fields)):
        problems.append(f"field '{name}' is owner=collector but appears in logging/schema.go; a "
                        f"service that can emit it can disagree with the collector, and nothing "
                        f"resolves the conflict")

    for name in sorted(application_owned & set(go_fields)):
        for prop in FIELD_PROPERTIES:
            want, got = control_fields[name].get(prop), go_fields[name].get(prop)
            if want != got:
                problems.append(f"field '{name}': {prop} is {want!r} in {CONTROL_PATH} but "
                                f"{got!r} in logging/schema.go")
    return problems


def compare_redaction(control: dict, go: dict) -> list[str]:
    problems: list[str] = []
    redaction = control.get("redaction") or {}

    control_mask = sorted({key for keys in (redaction.get("mask") or {}).values()
                           for key in (keys or [])})
    go_mask = sorted(go.get("mask_keys") or [])
    for name in sorted(set(control_mask) - set(go_mask)):
        problems.append(f"redaction: '{name}' is mask-class in {CONTROL_PATH} but the Go logger "
                        f"has no masker for it, so it is emitted verbatim")
    for name in sorted(set(go_mask) - set(control_mask)):
        problems.append(f"redaction: the Go logger masks '{name}' but {CONTROL_PATH} does not "
                        f"declare it; the control file is what the compliance evidence pack reads")

    control_drop = sorted(redaction.get("drop") or [])
    go_drop = sorted(go.get("drop_keys") or [])
    for name in sorted(set(control_drop) - set(go_drop)):
        problems.append(f"redaction: '{name}' is drop-class in {CONTROL_PATH} but the Go logger "
                        f"does not drop it")
    for name in sorted(set(go_drop) - set(control_drop)):
        problems.append(f"redaction: the Go logger drops '{name}' but {CONTROL_PATH} does not "
                        f"declare it")

    # A key that is both masked and dropped has no defined outcome; the order of two map lookups
    # would decide it.
    for name in sorted(set(control_mask) & set(control_drop)):
        problems.append(f"redaction: '{name}' is declared both mask and drop; the classes are "
                        f"exclusive and the outcome would depend on lookup order")

    never_mask = set(redaction.get("never_mask") or [])
    for name in sorted(never_mask & set(go_mask)):
        problems.append(f"redaction: '{name}' is in never_mask but the Go logger masks it")
    return problems


def compare_events(control: dict, go: dict) -> list[str]:
    problems: list[str] = []
    control_events = [r["name"] for r in control.get("records") or []
                      if isinstance(r, dict) and "name" in r]
    go_events = go.get("event_names") or []
    for name in sorted(set(control_events) - set(go_events)):
        problems.append(f"event_name '{name}' is in {CONTROL_PATH} but has no constant in "
                        f"logging/schema.go")
    for name in sorted(set(go_events) - set(control_events)):
        problems.append(f"event_name '{name}' has a Go constant but is not a declared record kind "
                        f"in {CONTROL_PATH}; the vocabulary is closed so a dashboard can enumerate "
                        f"it")

    # Every field a record requires must exist in the registry, or the requirement is unmeetable.
    known = {f["name"] for f in control.get("fields") or [] if isinstance(f, dict)}
    for record in control.get("records") or []:
        if not isinstance(record, dict):
            continue
        for required in record.get("requires") or []:
            if required not in known:
                problems.append(f"record '{record.get('name')}' requires field '{required}', "
                                f"which is not in the registry")
    return problems


def compare_constructors(control: dict, go: dict) -> list[str]:
    """The catalogue's claim that a kind has an emitter, checked against the emitter.

    This is the half that did not exist before Core ADR-0104, and its absence is why nine of eleven
    kinds could sit in the vocabulary with no code able to produce them. Every comparison in the
    rest of this file is between two DESCRIPTIONS of the schema; these are between the description
    and the thing described.
    """
    problems: list[str] = []
    go_constructors = go.get("constructors") or {}
    go_record_fields = go.get("record_fields") or {}

    for record in control.get("records") or []:
        if not isinstance(record, dict):
            continue
        name = record.get("name")

        declared = record.get("constructor")
        if not declared:
            problems.append(f"record '{name}' declares no `constructor`; a kind with no emitter is "
                            f"a name a dashboard can select on and nothing can produce")
            continue

        built = go_constructors.get(name)
        if built is None:
            problems.append(f"record '{name}' is in {CONTROL_PATH} but no Go type in the logging "
                            f"catalogue emits it; add one implementing the closed Record interface")
            continue
        if built != declared:
            problems.append(f"record '{name}': {CONTROL_PATH} names constructor {declared!r} but "
                            f"the catalogue registers {built!r}")

        # `requires` is a promise about the emitted record, so it is checked against the fields the
        # constructor actually writes for a zero value -- that is, unconditionally. A field emitted
        # only when some optional input is non-nil does not satisfy a requirement, and reading it
        # off the zero value is what makes the difference detectable.
        emitted = set(go_record_fields.get(name) or [])
        for required in record.get("requires") or []:
            if required not in emitted:
                problems.append(f"record '{name}' requires '{required}' but {built} does not emit "
                                f"it unconditionally; a conditionally-present field satisfies the "
                                f"contract in the type and not in the record")

        if not record.get("requires"):
            problems.append(f"record '{name}' declares no required fields; a kind whose only "
                            f"content is the envelope cannot support an alert")

    for name in sorted(set(go_constructors) - {r.get("name") for r in control.get("records") or []
                                               if isinstance(r, dict)}):
        problems.append(f"the logging catalogue registers a constructor for '{name}', which is not "
                        f"a declared record kind in {CONTROL_PATH}")
    return problems


def compare_loki(control: dict) -> list[str]:
    """The Loki tiers on fields and the Alloy allowlists are two statements of one decision."""
    problems: list[str] = []
    loki = control.get("loki") or {}
    fields = {f["name"]: f for f in control.get("fields") or [] if isinstance(f, dict)}

    promoted = set(((loki.get("structured_metadata") or {}).get("from_log_line")) or [])
    tiered_meta = {name for name, f in fields.items()
                   if f.get("loki") == "meta" and f.get("owner") == "application"}
    for name in sorted(tiered_meta - promoted):
        problems.append(f"field '{name}' is loki: meta but is not in "
                        f"loki.structured_metadata.from_log_line; it would stay in the line body "
                        f"and need a parser to filter on")
    for name in sorted(promoted - tiered_meta):
        problems.append(f"'{name}' is promoted to structured metadata but is not an "
                        f"application-owned loki: meta field")

    # The collector-derived half of the same decision. These fields are not read out of the log
    # line -- they arrive as LABELS, from Kubernetes discovery or from the CRI envelope, and the
    # pipeline demotes them. Unchecked, this list is where an index label hides: `stream` was a
    # sixth index label in the shipped pipeline for exactly as long as nothing compared the two,
    # and it took an end-to-end push to a real Loki to find it.
    from_k8s = set(((loki.get("structured_metadata") or {}).get("from_kubernetes")) or [])
    collector_meta = {name for name, f in fields.items()
                      if f.get("loki") == "meta" and f.get("owner") == "collector"}
    for name in sorted(collector_meta - from_k8s):
        problems.append(f"field '{name}' is a collector-owned loki: meta field but is not in "
                        f"loki.structured_metadata.from_kubernetes; nothing demotes it, so it "
                        f"stays an index label and becomes a stream dimension")
    for name in sorted(from_k8s - collector_meta):
        problems.append(f"'{name}' is demoted by from_kubernetes but is not a collector-owned "
                        f"loki: meta field in the registry")

    # index_labels and the `loki: label` tier are the same statement twice.
    labels = set(loki.get("index_labels") or [])
    tiered_label = {name for name, f in fields.items() if f.get("loki") == "label"}
    for name in sorted(tiered_label - labels):
        problems.append(f"field '{name}' is loki: label but is not in loki.index_labels; it is "
                        f"declared a stream dimension and is not one")
    for name in sorted(labels - tiered_label):
        problems.append(f"'{name}' is in loki.index_labels but is not a loki: label field in the "
                        f"registry")
    for name in sorted(labels & from_k8s):
        problems.append(f"'{name}' is both an index label and demoted by from_kubernetes; the "
                        f"pipeline would create the stream dimension and then remove it")

    for name in sorted(labels & set(fields)):
        if fields[name].get("owner") != "collector":
            problems.append(f"'{name}' is a Loki index label but is not owner: collector; index "
                            f"labels describe the origin of a record and are set by the collector")
    return problems


def compare_versions(control: dict, go: dict, standard_version, standard_error) -> list[str]:
    problems: list[str] = []
    if standard_error:
        problems.append(standard_error)
    control_version = control.get("version")
    go_version = go.get("version")
    if control_version != go_version:
        problems.append(f"schema version: {CONTROL_PATH} says {control_version!r}, "
                        f"logging.SchemaVersion is {go_version!r}")
    if standard_version is not None and standard_version != control_version:
        problems.append(f"schema version: {STANDARD_PATH} frontmatter says {standard_version!r}, "
                        f"{CONTROL_PATH} says {control_version!r}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=None,
                        help="monorepo root; supplies the default for the three paths below")
    parser.add_argument("--control", default=None, help=f"path to {CONTROL_PATH.name}")
    parser.add_argument("--standard", default=None, help=f"path to {STANDARD_PATH.name}")
    parser.add_argument("--go-module", default=None,
                        help="path to the platform-shared-go module root")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[2]
    control_path = Path(args.control).resolve() if args.control else root / CONTROL_PATH
    standard_path = Path(args.standard).resolve() if args.standard else root / STANDARD_PATH
    module_path = Path(args.go_module).resolve() if args.go_module else root / GO_MODULE_PATH

    control = load_control(control_path)
    go = dump_go_registry(module_path)
    standard_version, standard_error = load_standard_version(standard_path)

    problems = (compare_versions(control, go, standard_version, standard_error)
                + compare_fields(control, go)
                + compare_redaction(control, go)
                + compare_events(control, go)
                + compare_constructors(control, go)
                + compare_loki(control))
    if problems:
        fail(problems)

    fields = len(go.get("fields") or [])
    events = len(go.get("event_names") or [])
    masked = len(go.get("mask_keys") or [])
    dropped = len(go.get("drop_keys") or [])
    print(f"check-log-schema: schema v{go.get('version')} is consistent across "
          f"{STANDARD_PATH}, {CONTROL_PATH} and logging/schema.go "
          f"({fields} application fields, {events} record kinds, {masked} masked keys, "
          f"{dropped} dropped keys).")


if __name__ == "__main__":
    main()
