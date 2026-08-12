#!/usr/bin/env python3
"""Self-test for the UUID version-policy gate.

Two things are pinned here, and the second matters more than the first.

RECALL: every shape that actually shipped is asserted by control id AND line, so
a refactor that keeps the checker exiting non-zero for the wrong reason still
fails. The shapes are the three real regressions -- a UUIDv5 supplied as an
outbox event_id (voice-gateway, fixed in 92a7df2), the mustUUIDv7 stub that
ignored its arguments (eleven repositories), and a v4 supplied as an event id
spelled `ID` and identifiable only by its type's EventID() accessor
(identity-core, fixed in 315de91) -- plus the inverse direction and the ways a
declaration can itself be wrong.

PRECISION: the conformant fixture must produce ZERO findings. It is a catalogue
of the places a non-v7 UUID is legitimately fine: the ADR-0035 Idempotency-Key
header, request/correlation ids the standard declines to version, external vendor
ids, well-known seeded constants, a deriver that falls back to a fresh id on
empty input, a declared exception, a test file, and -- the one that keeps the
accessor inference from becoming the 613-finding version of this gate -- domain
entities whose plain `ID` field is minted with a v4. Any finding there is a false
positive, and a gate with false positives gets switched off, which is worse than
no gate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "check-uuid-version-policy.py"
SCANNER_SRC = REPO / "tools" / "uuidscan"
VIOLATING = REPO / "fixtures" / "uuid-policy-violating"
CONFORMANT = REPO / "fixtures" / "uuid-policy-conformant"

FAILURES: list[str] = []
_BIN: str | None = None


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def scanner_bin() -> str:
    """Build uuidscan once. It is stdlib-only, so this needs no module proxy."""
    global _BIN
    if _BIN:
        return _BIN
    go = shutil.which("go")
    if not go:
        print("::error::no Go toolchain on PATH; the uuid-policy gate is a go/ast pass "
              "and cannot be self-tested without one")
        raise SystemExit(2)
    out = Path(tempfile.mkdtemp(prefix="uuidscan-")) / "uuidscan"
    env = dict(os.environ, GOWORK="off", GOFLAGS="-mod=mod")
    proc = subprocess.run([go, "build", "-o", str(out), "."], cwd=str(SCANNER_SRC),
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"::error::uuidscan does not build: {proc.stderr.strip()}")
        raise SystemExit(2)
    _BIN = str(out)
    return _BIN


def run(root: Path, fmt: str = "json") -> tuple[int, dict, str]:
    env = dict(os.environ, UUIDSCAN_BIN=scanner_bin())
    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--repo-root", str(root), "--format", fmt],
        capture_output=True, text=True, env=env,
    )
    payload: dict = {}
    if fmt == "json" and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {}
    return proc.returncode, payload, proc.stdout + proc.stderr


def sites(payload: dict) -> set:
    return {(f["control"], f["file"], f["line"]) for f in payload.get("findings", [])}


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def test_violating_fixture_fails() -> None:
    code, payload, raw = run(VIOLATING)
    expect(code == 1, f"violating fixture must exit 1, got {code}\n{raw}")
    got = sites(payload)

    # The regression that shipped: a deterministic v5 supplied as event_id, in
    # three flavours -- inline through a helper, through a local variable, and
    # through a function in another package of the same repository.
    for line in (22, 34, 45):
        expect(("UUID-0001", "internal/events/emitter.go", line) in got,
               f"UUID-0001 must flag internal/events/emitter.go:{line} (v5 into event_id)")

    # The inverse direction: a fresh value on the deterministic dedup key.
    expect(("UUID-0002", "internal/keys/keys.go", 27) in got,
           "UUID-0002 must flag internal/keys/keys.go:27 (fresh v7 into idempotency_key)")

    # The mustUUIDv7 stub: parameters declared, none read, fresh id per call.
    expect(("UUID-0003", "internal/ids/stub.go", 11) in got,
           "UUID-0003 must flag the mustUUIDv7 stub at internal/ids/stub.go:11")

    # The wrapper form, where the parameter IS read and the lie is one hop away.
    expect(("UUID-0004", "internal/ids/stub.go", 17) in got,
           "UUID-0004 must flag DeterministicCallID at internal/ids/stub.go:17")

    # An undeclared derivation that reaches no documented sink.
    expect(("UUID-0005", "internal/undeclared/undeclared.go", 15) in got,
           "UUID-0005 must flag the undeclared derivation at internal/undeclared/undeclared.go:15")


def test_event_id_is_identified_by_its_accessor_not_its_name() -> None:
    """The gap that let the live defect through. identity-core spells the outbox
    event id `ID`, so a scan matching field NAMES alone produced no sink fact for
    the entire repository and reported OK. A field is the event id when its
    declaring type has an EventID() method returning it -- including when the
    literal is built one package away from the type, which is where the real
    handler built it."""
    _, payload, _ = run(VIOLATING)
    got = sites(payload)
    for f, line, why in (
        ("internal/creation/handler.go", 22, "the literal is built in another package of the module"),
        ("internal/identity/events.go", 39, "the value arrives through a local variable"),
    ):
        expect(("UUID-0001", f, line) in got,
               f"UUID-0001 must flag {f}:{line} - a v4 into a field the type's EventID() "
               f"accessor returns ({why})")
    messages = [f["message"] for f in payload.get("findings", [])
                if f["file"] == "internal/creation/handler.go"]
    expect(any("EventID()" in m for m in messages),
           "the finding must name the accessor that identified the field, or a reviewer "
           f"cannot tell why `ID` was judged an event id: {messages}")


def test_a_plain_entity_id_is_not_an_event_id() -> None:
    """The negative half, and the one that decides whether this gate survives.
    Configuring `ID` as a sink field name was measured at 613 findings across the
    fleet, almost all of them entity ids on domain structs where a v4 violates
    nothing. The catalog says that volume is what gets a gate switched off, so a
    struct with an ID field and no EventID() accessor must be invisible."""
    catalog = CONFORMANT / "internal/catalog/product.go"
    expect(catalog.exists(),
           "the conformant fixture must keep domain entities minting a v4 `ID`, or this "
           "test stops testing anything")
    expect("uuid.New()" in catalog.read_text(encoding="utf-8"),
           "the entity fixture must keep minting its ID with a v4")
    _, payload, _ = run(CONFORMANT)
    flagged = [f"{f['control']} {f['file']}:{f['line']}" for f in payload.get("findings", [])]
    expect(not flagged,
           "an ID field on a type with no EventID() accessor is entity identity and no "
           f"document versions it; flagged: {flagged}")


def test_a_conformant_accessor_sink_is_recognised_and_silent() -> None:
    """A rule that only ever fires proves nothing about why it fires. The
    conformant fixture carries the same accessor shape as the violating one with a
    v7 in the field, so the inference has to reach it and then find it clean."""
    fixture = CONFORMANT / "internal/identity/events.go"
    text = fixture.read_text(encoding="utf-8")
    expect("func (e IdentityCreatedEvent) EventID() uuid.UUID" in text,
           "the conformant fixture must keep an event id identified only by its accessor")
    _, payload, _ = run(CONFORMANT)
    expect(not any(f["file"] == "internal/identity/events.go"
                   for f in payload.get("findings", [])),
           "a v7 in an accessor-identified event id is exactly what ADR-0071 asks for")


def test_declaration_mechanism_polices_itself() -> None:
    _, payload, _ = run(VIOLATING)
    got = sites(payload)
    f = "internal/declarations/declarations.go"
    for line, why in ((13, "a marker beside no deterministic constructor is stale"),
                      (21, "an unsanctioned reason token is not a declaration"),
                      (27, "a v3 declaration beside a v5 constructor is wrong"),
                      (33, "a reason cited against the wrong ADR is wrong")):
        expect(("UUID-0006", f, line) in got, f"UUID-0006 must flag {f}:{line} - {why}")


def test_unwaivable_controls_are_enforced() -> None:
    _, payload, _ = run(VIOLATING)
    stages = {f["control"]: f["stage"] for f in payload.get("findings", [])}
    for cid in ("UUID-0001", "UUID-0002", "UUID-0003", "UUID-0004", "UUID-0006"):
        expect(stages.get(cid) == "enforce",
               f"{cid} is a MUST in an accepted ADR and must be at stage enforce, "
               f"got {stages.get(cid)!r}")
    expect(stages.get("UUID-0005") == "warn",
           "UUID-0005 is the declaration backlog and must start at stage warn so "
           "introducing the gate does not fail the fleet on pre-existing debt")


# ---------------------------------------------------------------------------
# Precision: the false-positive pin
# ---------------------------------------------------------------------------

def test_conformant_fixture_is_silent() -> None:
    code, payload, raw = run(CONFORMANT)
    expect(code == 0, f"conformant fixture must exit 0, got {code}\n{raw}")
    found = payload.get("findings", [])
    expect(not found,
           "conformant fixture must produce NO findings; each one is a false positive: "
           + "; ".join(f"{f['control']} {f['file']}:{f['line']}" for f in found))


def test_prose_quoting_a_constructor_is_not_a_constructor() -> None:
    """The fixed voice-gateway emitter quotes both uuid.NewSHA1 and
    uuid.Must(uuid.NewV7()) in a doc comment explaining the bug it removed. This
    asserts the difference between the gate and a grep."""
    emitter = CONFORMANT / "internal/events/emitter.go"
    text = emitter.read_text(encoding="utf-8")
    quoted = [i + 1 for i, line in enumerate(text.splitlines())
              if line.lstrip().startswith("//") and
              ("uuid.NewSHA1" in line or "uuid.Must(uuid.NewV7())" in line)]
    expect(bool(quoted),
           "the conformant emitter must keep quoting a constructor in prose, or this "
           "test stops testing anything")
    _, payload, _ = run(CONFORMANT)
    flagged = {f["line"] for f in payload.get("findings", [])
               if f["file"] == "internal/events/emitter.go"}
    expect(not (flagged & set(quoted)),
           f"a constructor named in a comment must never be a finding; lines {quoted} "
           f"are prose but {sorted(flagged & set(quoted))} were flagged")


def test_test_files_are_not_scanned() -> None:
    fixture = CONFORMANT / "internal/events/emitter_test.go"
    expect(fixture.exists(), "the conformant fixture must keep a _test.go that would "
                             "violate if scanned")
    _, payload, _ = run(CONFORMANT)
    expect(not any(f["file"].endswith("_test.go") for f in payload.get("findings", [])),
           "test files mint arbitrary UUIDs and must not be scanned")


# ---------------------------------------------------------------------------
# Operability
# ---------------------------------------------------------------------------

def test_repo_without_go_is_skipped_not_failed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "README.md").write_text("no go here\n", encoding="utf-8")
        code, _, raw = run(Path(tmp), fmt="text")
        expect(code == 0, f"a repository with no Go source must exit 0, got {code}\n{raw}")
        expect("skipping" in raw, f"the skip must be stated, got: {raw!r}")


def test_empty_scan_of_a_go_repo_cannot_report_a_pass() -> None:
    """A Go repository whose scan yields nothing is a broken checker, not a clean
    repository. Exit 2 keeps the two apart."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "go.mod").write_text("module example.com/empty\n\ngo 1.22\n",
                                          encoding="utf-8")
        code, _, raw = run(Path(tmp), fmt="text")
        expect(code == 2, f"a Go repo with no parseable files must exit 2, got {code}\n{raw}")


def test_catalog_is_valid_and_self_consistent() -> None:
    import yaml
    doc = yaml.safe_load((REPO / "controls" / "uuid-policy.yaml").read_text(encoding="utf-8"))
    ids = {c["id"] for c in doc["controls"]}
    for sink in doc["sinks"]:
        expect(sink["control"] in ids,
               f"sink {sink['field']} names unknown control {sink['control']}")
        expect(sink["requires"] not in sink["rejects"],
               f"sink {sink['field']} both requires and rejects {sink['requires']}")
    kinds = {c["kind"] for c in doc["constructors"]}
    for sink in doc["sinks"]:
        for k in [sink["requires"], *sink["rejects"]]:
            expect(k in kinds, f"sink {sink['field']} references unknown kind {k!r}")
    # The detector fires on `rejects`, so a kind missing from it is passed over in
    # silence however plainly `requires` contradicts it. That asymmetry is the
    # whole of how a v4 event_id went unnoticed: EventID required fresh-v7 and
    # rejected only the two deterministic kinds, while IdempotencyKey directly
    # below it already rejected random-v4. A sink requiring a fresh v7 admits
    # nothing else -- resolveEventID hard errors on every other version -- so
    # every other kind has to be listed.
    for sink in doc["sinks"]:
        if sink["requires"] != "fresh-v7":
            # idempotency_key deliberately does not reject deterministic-v3: a v3
            # is still derived from its inputs, which is the property ADR-0071
            # decision 2 is after, so it is a documentation question and not a
            # silently broken dedup key.
            continue
        for k in sorted(kinds - {"fresh-v7"}):
            expect(k in sink["rejects"],
                   f"sink {sink['field']} requires fresh-v7 but does not reject {k!r}, so a "
                   f"{k} value there is passed over in silence")
    for r in doc["reasons"]:
        for key in ("token", "adr", "description"):
            expect(bool(r.get(key)), f"reason {r.get('token')!r} is missing {key}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if FAILURES:
        print(f"uuid-version-policy self-test: FAILED ({len(FAILURES)} assertion(s))")
        for f in FAILURES:
            print(f"::error::{f}")
        return 1
    print(f"uuid-version-policy self-test: OK ({len(tests)} test(s)) - recall pinned on all three "
          f"shipped regressions, precision pinned at zero findings on the conformant fixture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
