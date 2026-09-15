#!/usr/bin/env python3
"""Self-check for API-0007's reference artifact and its expand/contract window.

    python3 scripts/test_api_contract_canonical_components.py

controls/common-v1-components.json used to name exactly one contracts vintage, so moving the
projection (a new ErrorCode value, a PaginationMeta presence change) turned every service that
publishes common.v1 components red at once: the control, the module floor and each spec had to
move in one moment nobody could coordinate. The file may now carry one more accepted set - `next`
while the fleet bumps, `previous` for the grace after promotion - and this suite holds the rules
that keep that window from weakening the gate:

  - a spec passes only by reproducing ONE accepted set whole; a per-component mix fails and the
    failure names both sets
  - a single-set file (every file before this change, and raw emitter output) reads exactly as
    before, message for message
  - the floor must name an accepted set that is not `next`
  - the window is opened, promoted and closed by the checker's own flags, and closing it leaves
    the bytes the emitter prints for the new vintage, so the artifact stays generated

Fixtures are synthetic and built in tempfile, never the committed artifact's current contents,
because that file is expected to change shape during a real window and a test pinned to it
would start failing for the reason it exists. The one test that does read it asserts only that
it is valid and canonically serialised. check-api-contract.py is imported by file path so the
functions under test are the ones CI runs.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CHECKER_PATH = HERE / "check-api-contract.py"
CONTROLS_PATH = HERE.parent / "controls" / "api-contract.yaml"
MODULE = "github.com/coderaxis/platform-contracts-go"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_api_contract", CHECKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve the defining module through sys.modules; register before exec.
    sys.modules["check_api_contract"] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_checker()

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


# ── fixtures: two vintages that differ the way the response-format change does ──────────────

def components_v070() -> dict:
    return {
        "common.v1.ErrorCode": {"type": "string",
                                "enum": ["ERROR_CODE_UNSPECIFIED", "INVALID_INPUT", "RESOURCE_NOT_FOUND"]},
        "common.v1.ErrorResponse": {"type": "object", "additionalProperties": False,
                                    "properties": {"code": {"$ref": "#/components/schemas/common.v1.ErrorCode"}}},
        "common.v1.PaginationMeta": {"type": "object", "properties": {
            "totalItems": {"type": "integer", "format": "int64"},
            "hasNext": {"type": "boolean"}}},
    }


def components_v080() -> dict:
    """PaginationMeta narrows totalItems (C1) and ErrorCode gains a value, in ONE vintage."""
    comps = components_v070()
    comps["common.v1.PaginationMeta"]["properties"]["totalItems"]["format"] = "int32"
    comps["common.v1.ErrorCode"]["enum"].append("METHOD_NOT_ALLOWED")
    return comps


def emitted(version: str, components: dict) -> dict:
    """What emit-canonical-components prints: one set, its source, and the comment."""
    return {"_comment": "Canonical common.v1 OpenAPI components, projected from the proto SSOT. "
                        "Generated - do not edit by hand.",
            "components": components,
            "source": {"module": MODULE, "version": version}}


def with_next(current: dict, nxt: dict) -> dict:
    return dict(current, next={"components": nxt["components"], "source": nxt["source"]})


def sets_of(doc: dict) -> list:
    sets, reason = m.parse_reference(doc)
    if sets is None:
        raise AssertionError(f"fixture reference is invalid: {reason}")
    return sets


def published(components: dict, **extra) -> dict:
    return {**json.loads(json.dumps(components)), **extra}


V070 = emitted("v0.7.0", components_v070())
V080 = emitted("v0.8.0", components_v080())


# ── the committed artifact ───────────────────────────────────────────────────────────────────

def test_committed_reference_is_valid_and_canonical():
    raw = m.CANONICAL_COMPONENTS.read_text(encoding="utf-8")
    doc = json.loads(raw)
    sets, reason = m.parse_reference(doc)
    expect(sets is not None, f"controls/common-v1-components.json must be a valid reference: {reason}")
    # The window tools write through _reference_json. If the committed file is not already in
    # that serialisation, the first --stage-next would reformat all of it and bury the real diff.
    expect(m._reference_json(doc) == raw,
           "controls/common-v1-components.json is not in the emitter's canonical serialisation "
           "(sorted keys, 2-space indent, trailing newline); regenerate it rather than editing it")


# ── matching: single set reads as before ─────────────────────────────────────────────────────

def test_single_set_reference_reads_exactly_as_before():
    sets = sets_of(V070)
    expect([s.role for s in sets] == ["current"], f"one set, and it is current: {sets}")

    f = m.compare_to_accepted_sets(published(components_v070()), sets)
    expect(f.ok and f.count == 0 and f.evidence == "common.v1 components match the v0.7.0 proto projection",
           f"a matching spec keeps the pre-window evidence text: {f}")

    stale = published(components_v070())
    stale["common.v1.ErrorCode"]["enum"] = stale["common.v1.ErrorCode"]["enum"][:-1]
    del stale["common.v1.PaginationMeta"]
    f = m.compare_to_accepted_sets(stale, sets)
    expect(not f.ok and f.count == 2, f"one differing and one missing component count 2: {f}")
    expect(f.evidence == "2 component(s) stale against v0.7.0", f"single-set evidence unchanged: {f.evidence}")
    expect(f.details == ["common.v1.ErrorCode: differs from the v0.7.0 proto projection",
                         "common.v1.PaginationMeta: missing from the spec but present in v0.7.0"],
           f"single-set detail lines unchanged: {f.details}")


def test_a_service_may_still_publish_other_common_v1_messages():
    # DTOs reach common.v1 messages outside the envelope (CursorQueryRequest, CountResult).
    # The reference has no opinion on those, before or after a window opens.
    spec = published(components_v070(), **{"common.v1.CountResult": {"type": "object"}})
    for sets in (sets_of(V070), sets_of(with_next(V070, V080))):
        f = m.compare_to_accepted_sets(spec, sets)
        expect(f.ok, f"an extra common.v1 component is not judged: {f}")


# ── matching: two sets ───────────────────────────────────────────────────────────────────────

def test_either_whole_set_passes_and_says_which():
    sets = sets_of(with_next(V070, V080))
    expect([s.label for s in sets] == ["next v0.8.0", "current v0.7.0"], f"newest first: {sets}")

    f = m.compare_to_accepted_sets(published(components_v070()), sets)
    expect(f.ok and "current v0.7.0" in f.evidence and "next v0.8.0 also accepted" in f.evidence,
           f"a spec still on current passes during the window, naming both sets: {f.evidence}")

    f = m.compare_to_accepted_sets(published(components_v080()), sets)
    expect(f.ok and "next v0.8.0 proto projection" in f.evidence and "current v0.7.0 also accepted" in f.evidence,
           f"a spec already on next passes, naming both sets: {f.evidence}")


def test_a_per_component_mix_fails_and_names_both_sets():
    # PaginationMeta from next, ErrorCode from current: each component matches SOME accepted
    # set, and no platform-contracts-go release produces that pair.
    sets = sets_of(with_next(V070, V080))
    mix = published(components_v070())
    mix["common.v1.PaginationMeta"] = components_v080()["common.v1.PaginationMeta"]
    f = m.compare_to_accepted_sets(mix, sets)

    expect(not f.ok, f"a mix of two accepted vintages MUST fail: {f}")
    expect(f.count == 1, f"the count is the distance to the nearest set: {f}")
    expect("mix proto vintages" in f.evidence and "next v0.8.0" in f.evidence and "current v0.7.0" in f.evidence,
           f"the evidence must call it a mix and name both sets: {f.evidence}")
    expect("common.v1.ErrorCode: matches the current v0.7.0 proto projection but differs from next v0.8.0" in f.details,
           f"each component must say which set it matches: {f.details}")
    expect("common.v1.PaginationMeta: matches the next v0.8.0 proto projection but differs from current v0.7.0" in f.details,
           f"each component must say which set it matches: {f.details}")


def test_a_component_neither_set_produced_is_not_called_a_mix():
    sets = sets_of(with_next(V070, V080))
    spec = published(components_v080())
    spec["common.v1.ErrorResponse"]["properties"]["code"] = {"type": "string"}  # hand-edited
    f = m.compare_to_accepted_sets(spec, sets)
    expect(not f.ok and "match no accepted proto projection" in f.evidence,
           f"a hand-edited component matches nothing: {f.evidence}")
    expect("nearest is next v0.8.0" in f.evidence, f"the nearest set is named: {f.evidence}")
    expect("common.v1.ErrorResponse: differs from the next v0.8.0 and current v0.7.0 proto projections" in f.details,
           f"the detail names both sets it differs from: {f.details}")


def test_a_component_only_next_defines():
    # A vintage may ADD a component. A spec on current never published it, and is judged on
    # current's names only; a spec on next must carry it.
    nxt = emitted("v0.8.0", dict(components_v080(),
                                 **{"common.v1.CursorPaginationMeta": {"type": "object"}}))
    sets = sets_of(with_next(V070, nxt))
    expect(m.compare_to_accepted_sets(published(components_v070()), sets).ok,
           "a spec on current need not carry a component only next defines")
    expect(m.compare_to_accepted_sets(published(nxt["components"]), sets).ok,
           "a spec on next carrying the new component passes")
    f = m.compare_to_accepted_sets(published(components_v080()), sets)
    expect(not f.ok and "common.v1.CursorPaginationMeta: missing from the spec but present in next v0.8.0" in f.details,
           f"a spec on next without next's new component is incomplete: {f}")


def test_previous_is_accepted_after_promotion():
    promoted = dict(V080, previous={"components": V070["components"], "source": V070["source"]})
    sets = sets_of(promoted)
    expect([s.label for s in sets] == ["current v0.8.0", "previous v0.7.0"], f"newest first: {sets}")
    f = m.compare_to_accepted_sets(published(components_v070()), sets)
    expect(f.ok and "previous v0.7.0" in f.evidence, f"a straggler on previous still passes: {f.evidence}")


# ── reference validation ─────────────────────────────────────────────────────────────────────

def test_reference_validation():
    def rejects(doc, fragment, why):
        sets, reason = m.parse_reference(doc)
        expect(sets is None and fragment in (reason or ""), f"{why}: got sets={sets} reason={reason!r}")

    rejects(dict(with_next(V070, V080), previous={"components": components_v070(),
                                                  "source": {"module": MODULE, "version": "v0.6.0"}}),
            "carries both", "next and previous together are three vintages")
    rejects(with_next(V070, emitted("v0.7.0", components_v080())), "not newer",
            "next at the same version as current")
    rejects(dict(V080, previous={"components": components_v070(),
                                 "source": {"module": MODULE, "version": "v0.9.0"}}),
            "not older", "previous newer than current")
    rejects(with_next(V070, emitted("v0.8.0", components_v070())), "byte-identical",
            "a window that changes nothing")
    rejects(with_next(V070, emitted("v0.0.0-20260915000000-0123456789ab", components_v080())),
            "stable vX.Y.Z", "a pseudo-version cannot be pinned by a service")
    other = emitted("v0.8.0", components_v080())
    other["source"]["module"] = "github.com/example/fork"
    rejects(with_next(V070, other), "same module", "sets from two modules")
    rejects(dict(V070, nxet=V080), "unknown member", "a misspelt next must not be ignored")
    rejects(dict(V070, components={}), "no components", "an empty set would pass everything")
    rejects(with_next(V070, dict(V080, components={})), "next set has no components",
            "an empty next set would pass everything")

    legacy = dict(V070, source={})
    sets, reason = m.parse_reference(legacy)
    expect(sets is not None and sets[0].version == "unknown",
           f"a single set keeps its old tolerance for an unrecorded version: {reason}")


# ── the floor ────────────────────────────────────────────────────────────────────────────────

def floors(version) -> dict:
    return {"floors": {MODULE: {"min": version, "reason": "fixture"}}}


def test_floor_must_name_an_accepted_set_that_is_not_next():
    single = sets_of(V070)
    expect(m.floor_problems(floors("v0.7.0"), single) == [], "single set: floor == version holds")
    expect(m.floor_problems({"floors": {MODULE: "v0.7.0"}}, single) == [],
           "a bare version string is a floor too")
    p = m.floor_problems(floors("v0.8.0"), single)
    expect(len(p) == 1 and "v0.8.0" in p[0] and "v0.7.0" in p[0],
           f"single set: a mismatch names both versions: {p}")
    expect(len(m.floor_problems({"floors": {}}, single)) == 1, "no contracts floor at all is a problem")

    window = sets_of(with_next(V070, V080))
    expect(m.floor_problems(floors("v0.7.0"), window) == [], "during expand the floor stays at current")
    p = m.floor_problems(floors("v0.8.0"), window)
    expect(len(p) == 1 and "staged next" in p[0] and "--promote-next" in p[0],
           f"a floor on next fails the repos the window lets bump one at a time: {p}")
    for between in ("v0.7.5", "v0.6.0", "v0.9.0"):
        p = m.floor_problems(floors(between), window)
        expect(len(p) == 1 and "next v0.8.0" in p[0] and "current v0.7.0" in p[0],
               f"floor {between} names no accepted set, and the message names both: {p}")

    grace = sets_of(dict(V080, previous={"components": V070["components"], "source": V070["source"]}))
    expect(m.floor_problems(floors("v0.7.0"), grace) == [], "after promotion the floor may still name previous")
    expect(m.floor_problems(floors("v0.8.0"), grace) == [], "after promotion the floor may name current")


# ── the window, through the real CLI ─────────────────────────────────────────────────────────

def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER_PATH), *map(str, args)],
                          capture_output=True, text=True)


def test_window_lifecycle_ends_on_the_emitters_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref, new = tmp / "ref.json", tmp / "emitted.json"
        ref.write_text(m._reference_json(V070), encoding="utf-8")
        new.write_text(m._reference_json(V080), encoding="utf-8")
        floor_old, floor_new = tmp / "floor-old.yaml", tmp / "floor-new.yaml"
        floor_old.write_text(json.dumps(floors("v0.7.0")), encoding="utf-8")
        floor_new.write_text(json.dumps(floors("v0.8.0")), encoding="utf-8")

        def labels():
            return [s.label for s in sets_of(json.loads(ref.read_text(encoding="utf-8")))]

        p = run("--components", ref, "--stage-next", new)
        expect(p.returncode == 0 and labels() == ["next v0.8.0", "current v0.7.0"],
               f"stage-next opens the window: rc={p.returncode} {p.stdout}{p.stderr}")
        expect(run("--components", ref, "--verify-floor", floor_old).returncode == 0,
               "floor at current holds during expand")
        expect(run("--components", ref, "--verify-floor", floor_new).returncode == 1,
               "floor at next fails during expand")
        p = run("--components", ref, "--stage-next", new)
        expect(p.returncode == 1 and "already carries `next`" in p.stdout,
               f"a second window cannot open over the first: {p.stdout}")

        p = run("--components", ref, "--promote-next")
        expect(p.returncode == 0 and labels() == ["current v0.8.0", "previous v0.7.0"],
               f"promote-next makes next current and keeps the old set as previous: {p.stdout}{p.stderr}")
        expect(run("--components", ref, "--verify-floor", floor_old).returncode == 0,
               "promotion alone breaks nobody: the floor may still name previous")

        p = run("--components", ref, "--drop-set", "previous")
        expect(p.returncode == 0 and labels() == ["current v0.8.0"], f"drop previous closes it: {p.stdout}")
        expect(ref.read_bytes() == new.read_bytes(),
               "a closed window must leave exactly the bytes the emitter printed for the new vintage")
        expect(run("--components", ref, "--verify-floor", floor_old).returncode == 1,
               "once previous is gone a floor still on it is refused")
        expect(run("--components", ref, "--verify-floor", floor_new).returncode == 0,
               "and the raised floor holds")


def test_abandoning_next_restores_the_original_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref, new = tmp / "ref.json", tmp / "emitted.json"
        ref.write_text(m._reference_json(V070), encoding="utf-8")
        new.write_text(m._reference_json(V080), encoding="utf-8")
        original = ref.read_bytes()
        run("--components", ref, "--stage-next", new)
        p = run("--components", ref, "--drop-set", "next")
        expect(p.returncode == 0 and ref.read_bytes() == original,
               f"--drop-set next undoes --stage-next byte for byte: {p.stdout}{p.stderr}")


def test_window_tools_refuse_invalid_transitions():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref = tmp / "ref.json"
        ref.write_text(m._reference_json(V070), encoding="utf-8")
        original = ref.read_bytes()

        p = run("--components", ref, "--promote-next")
        expect(p.returncode == 1 and "no `next`" in p.stdout, f"nothing to promote: {p.stdout}")
        p = run("--components", ref, "--drop-set", "previous")
        expect(p.returncode == 1 and "no `previous`" in p.stdout, f"nothing to drop: {p.stdout}")

        older = tmp / "older.json"
        older.write_text(m._reference_json(emitted("v0.6.0", components_v080())), encoding="utf-8")
        p = run("--components", ref, "--stage-next", older)
        expect(p.returncode == 1 and "not newer" in p.stdout, f"next must be newer: {p.stdout}")

        not_raw = tmp / "reference.json"
        not_raw.write_text(m._reference_json(with_next(emitted("v0.8.0", components_v080()),
                                                       emitted("v0.9.0", components_v070()))),
                           encoding="utf-8")
        p = run("--components", ref, "--stage-next", not_raw)
        expect(p.returncode == 1 and "raw emit-canonical-components output" in p.stdout,
               f"only raw emitter output is staged: {p.stdout}")
        expect(ref.read_bytes() == original, "a refused transition writes nothing")

        broken = tmp / "broken.json"
        broken.write_text(m._reference_json(dict(V070, components={})), encoding="utf-8")
        p = run("--components", broken, "--drop-set", "next")
        expect(p.returncode == 2, f"an invalid reference is never edited: rc={p.returncode} {p.stdout}")


def _service(root: pathlib.Path, components: dict) -> pathlib.Path:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "openapi.json").write_text(json.dumps(
        {"openapi": "3.0.3", "info": {"title": "fixture", "version": "1.0.0"}, "paths": {},
         "components": {"schemas": components}}), encoding="utf-8")
    return root


def test_end_to_end_gate_with_a_window_open():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref = tmp / "ref.json"
        ref.write_text(m._reference_json(with_next(V070, V080)), encoding="utf-8")

        mix = published(components_v070())
        mix["common.v1.ErrorCode"] = components_v080()["common.v1.ErrorCode"]
        cases = {"on-current": (components_v070(), False),
                 "on-next": (components_v080(), False),
                 "mixed": (mix, True)}
        for name, (components, should_fail) in cases.items():
            p = run(_service(tmp / name, components), "--controls", CONTROLS_PATH,
                    "--components", ref, "--fail-on", "major")
            fired = "::error::[API-0007]" in p.stdout
            expect(fired == should_fail,
                   f"end-to-end {name}: API-0007 fired={fired}, want {should_fail}\n{p.stdout}{p.stderr}")
            if should_fail:
                expect("next v0.8.0" in p.stdout and "current v0.7.0" in p.stdout,
                       f"end-to-end {name}: the CI output must name both accepted sets\n{p.stdout}")

        invalid = tmp / "invalid.json"
        invalid.write_text(m._reference_json(with_next(V070, emitted("v0.8.0", components_v070()))),
                           encoding="utf-8")
        p = run(_service(tmp / "any", components_v070()), "--controls", CONTROLS_PATH,
                "--components", invalid)
        expect(p.returncode == 2 and "byte-identical" in p.stdout,
               f"an invalid reference refuses to run rather than pass: rc={p.returncode} {p.stdout}")


# ── run everything ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()

    if FAILURES:
        print(f"api-contract API-0007 reference: FAILED ({len(FAILURES)} assertion(s))")
        for msg in FAILURES:
            print(f"  ::error:: {msg}")
        return 1

    print(f"api-contract API-0007 reference: OK ({len(tests)} test function(s)) - one whole "
          "accepted set passes, a mix fails naming both, single-set files read as before, the "
          "floor names an accepted non-next set, and a window closes on the emitter's bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
