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
  - the floor is never above the current set (so never on `next`), and a floor below it is only
    stale, so closing a window never waits on a fleet-wide floor raise
  - a vintage is a (platform-contracts-go, platform-shared-go) pair: a projection change that
    ships in shared-go alone can be staged once both sets record their projector
  - the window is opened, promoted and closed by the checker's own flags, and closing it leaves
    the bytes the emitter prints for the new vintage, so the artifact stays generated
  - against the last release, the artifact moves by exactly one window step (a promote or a close
    may be rolled back with git revert) and the floor never falls unless the rollback is
    declared, so a hand-added `previous` or a lowered floor that is well-formed on its own still
    fails - on every later commit until it is fixed, and in release.yaml before @v1 moves

Fixtures are synthetic and built in tempfile, never the committed artifact's current contents,
because that file is expected to change shape during a real window and a test pinned to it
would start failing for the reason it exists. The one test that does read it asserts only that
it is valid and canonically serialised. check-api-contract.py is imported by file path so the
functions under test are the ones CI runs.
"""

from __future__ import annotations

import importlib.util
import json
import os
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
RENAMED_COMMENT = ("Canonical common.v1 OpenAPI components, projected from the proto SSOT. Generated - do "
                   "not edit by hand. Regenerate with `go run <package>@<tag>`.")


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
    rejects(dict(V070, next={"components": components_v080(), "source": V080["source"], "sorce": {}}),
            "next set carries unknown member(s) ['sorce']", "a misspelt member inside a set must not be ignored")
    sets, reason = m.parse_reference(dict(V070, next=dict(V080, _comment="the emitter's comment")))
    expect(sets is not None, f"a set may carry the emitter's comment for its vintage: {reason}")
    rejects(with_next(V070, dict(V080, components={})), "next set has no components",
            "an empty next set would pass everything")

    legacy = dict(V070, source={})
    sets, reason = m.parse_reference(legacy)
    expect(sets is not None and sets[0].version == "unknown",
           f"a single set keeps its old tolerance for an unrecorded version: {reason}")


# ── the floor ────────────────────────────────────────────────────────────────────────────────

def floors(version, **extra) -> dict:
    return {"floors": {MODULE: {"min": version, "reason": "fixture", **extra}}}


def grace_of(current: dict, previous: dict) -> dict:
    return dict(current, previous={"components": previous["components"], "source": previous["source"]})


def test_floor_is_never_above_current():
    single = sets_of(V070)
    expect(m.floor_problems(floors("v0.7.0"), single) == [], "single set: floor == version holds")
    expect(m.floor_problems({"floors": {MODULE: "v0.7.0"}}, single) == [],
           "a bare version string is a floor too")
    p = m.floor_problems(floors("v0.8.0"), single)
    expect(len(p) == 1 and "v0.8.0" in p[0] and "v0.7.0" in p[0] and "above current" in p[0],
           f"single set: a floor above the reference names both versions: {p}")
    expect(m.floor_problems(floors("v0.6.0"), single) == [],
           "a floor below current is stale, not a disagreement: API-0007 still fails old specs")
    expect(len(m.floor_problems({"floors": {}}, single)) == 1, "no contracts floor at all is a problem")
    unknown = sets_of(dict(V070, source={}))
    expect(len(m.floor_problems(floors("v0.7.0"), unknown)) == 1
           and m.floor_problems(floors("unknown"), unknown) == [],
           "versions that cannot be ordered must be equal, as before windows existed")

    window = sets_of(with_next(V070, V080))
    expect(m.floor_problems(floors("v0.7.0"), window) == [], "during expand the floor stays at current")
    p = m.floor_problems(floors("v0.8.0"), window)
    expect(len(p) == 1 and "staged next v0.8.0" in p[0] and "--promote-next" in p[0],
           f"a floor on next fails the repos the window lets bump one at a time: {p}")
    for above in ("v0.7.5", "v0.9.0"):
        p = m.floor_problems(floors(above), window)
        expect(len(p) == 1 and "next v0.8.0" in p[0] and "current v0.7.0" in p[0],
               f"floor {above} is above current, and the message names both sets: {p}")
    expect(m.floor_problems(floors("v0.6.0"), window) == [], "a stale floor stays valid in a window")

    grace = sets_of(grace_of(V080, V070))
    expect(m.floor_problems(floors("v0.7.0"), grace) == [], "after promotion the floor may still name previous")
    expect(m.floor_problems(floors("v0.8.0"), grace) == [], "after promotion the floor may name current")

    closed = sets_of(V080)
    expect(m.floor_problems(floors("v0.7.0"), closed) == [],
           "closing a window does not force the fleet-wide floor up: the old floor is only stale")


# ── the projector: platform-shared-go is half of a vintage ───────────────────────────────────

def projected(doc: dict, shared_go: str) -> dict:
    out = json.loads(json.dumps(doc))
    out["source"]["projector"] = {"module": "github.com/coderaxis/platform-shared-go", "version": shared_go}
    return out


def test_a_projection_change_in_shared_go_alone_can_be_staged():
    # Meta's `required` list moves in platform-shared-go with no contracts release: the emitter
    # prints the same source.version for both sets, and only the projector orders them.
    cur = projected(V070, "v1.60.0")
    nxt = projected(emitted("v0.7.0", components_v080()), "v1.61.0")
    sets = sets_of(with_next(cur, nxt))
    expect([s.label for s in sets] == ["next v0.7.0 via platform-shared-go v1.61.0",
                                       "current v0.7.0 via platform-shared-go v1.60.0"],
           f"labels carry the projector when it is recorded: {[s.label for s in sets]}")
    expect(m.floor_problems(floors("v0.7.0"), sets) == [],
           "a floor at the shared contracts release names current, not only next")
    f = m.compare_to_accepted_sets(published(components_v080()), sets)
    expect(f.ok and "next v0.7.0 via platform-shared-go v1.61.0" in f.evidence,
           f"a spec on the new projection passes and says which pair: {f.evidence}")

    sets, reason = m.parse_reference(with_next(V070, emitted("v0.7.0", components_v080())))
    expect(sets is None and "not newer" in reason and "source.projector" in reason,
           f"equal contracts releases with no projector recorded cannot be ordered: {reason}")
    sets, reason = m.parse_reference(with_next(V070, nxt))
    expect(sets is None and "source.projector" in reason,
           f"the projector has to be recorded on BOTH sets to order them: {reason}")
    sets, reason = m.parse_reference(with_next(projected(V070, "v1.61.0"), projected(
        emitted("v0.7.0", components_v080()), "v1.61.0")))
    expect(sets is None and "not newer" in reason, f"the same pair twice is not a window: {reason}")
    sets, reason = m.parse_reference(grace_of(projected(emitted("v0.7.0", components_v080()), "v1.61.0"), cur))
    expect(sets is not None, f"after promotion, previous is the older projector of the same release: {reason}")


def test_the_projector_may_not_move_against_the_contracts_release():
    sets, reason = m.parse_reference(with_next(projected(V070, "v1.61.0"), projected(V080, "v1.60.0")))
    expect(sets is None and "drops projection rules" in (reason or ""),
           f"a newer contracts release projected by an older shared-go is refused: {reason}")
    sets, reason = m.parse_reference(with_next(projected(V070, "v1.60.0"), projected(V080, "(devel)")))
    expect(sets is None and "not a stable vX.Y.Z tag" in (reason or ""),
           f"`go run ./...` in a checkout records (devel), which no service can pin: {reason}")
    sets, reason = m.parse_reference(projected(V070, "(devel)"))
    expect(sets is not None, f"a single set keeps its tolerance for an unpinnable projector: {reason}")
    sets, reason = m.parse_reference(with_next(V070, projected(V080, "v1.61.0")))
    expect(sets is not None, "a newer contracts release orders the sets even when only one records a projector")
    bad = projected(V070, "v1.60.0")
    bad["source"]["projector"] = "v1.60.0"
    sets, reason = m.parse_reference(bad)
    expect(sets is None and "source.projector" in (reason or ""), f"a malformed projector is refused: {reason}")


# ── transitions against the base commit ──────────────────────────────────────────────────────

def moved(base_doc, head_doc):
    return m.transition_problems(None if base_doc is None else sets_of(base_doc), sets_of(head_doc))


def minus_enum_values(doc: dict, n: int, version: str) -> dict:
    comps = json.loads(json.dumps(doc["components"]))
    comps["common.v1.ErrorCode"]["enum"] = comps["common.v1.ErrorCode"]["enum"][:-n]
    return emitted(version, comps)


def test_each_window_step_is_one_allowed_transition():
    window, grace = with_next(V070, V080), grace_of(V080, V070)
    for base, head, fragment, why in (
        (V070, V070, "unchanged", "no change"),
        (V070, window, "staged next v0.8.0", "stage"),
        (window, V070, "abandoned", "abandon"),
        (window, grace, "promoted next to current v0.8.0", "promote"),
        (grace, V080, "window is closed", "close"),
        (None, V070, "new in this change", "an artifact with no base"),
        (dict(V070, _comment="reworded"), V070, "unchanged", "the comment is not a set"),
    ):
        problems, summary = moved(base, head)
        expect(problems == [] and fragment in summary, f"{why}: problems={problems} summary={summary!r}")


def test_a_promote_or_a_close_rolls_back_as_one_step():
    window, grace = with_next(V070, V080), grace_of(V080, V070)
    closed_previous = sets_of(grace)[1]  # what the release before the close carried as previous

    # Reverting a promote accepts the same two sets: nobody's verdict changes.
    problems, summary = moved(grace, window)
    expect(problems == [] and "demoted current v0.8.0 back to next beside v0.7.0" in summary,
           f"a reverted promote is a demote: {problems} {summary!r}")

    # Reverting a close readmits the set the close dropped, as the release before it carried it.
    problems, summary = m.transition_problems(sets_of(V080), sets_of(grace), closed_previous)
    expect(problems == [] and "reopened previous v0.7.0" in summary,
           f"a reverted close reopens previous: {problems} {summary!r}")
    for closed, fragment, why in (
        (None, "no release since v0.8.0 became current carried a previous set", "with no grace in history"),
        (sets_of(grace_of(V080, minus_enum_values(V070, 1, "v0.7.0")))[1], "carried previous v0.7.0",
         "when history names a different set"),
    ):
        problems, _ = m.transition_problems(sets_of(V080), sets_of(grace), closed)
        expect(len(problems) == 1 and "is not the set a close could have dropped" in problems[0]
               and fragment in problems[0], f"a reopen {why} fails and says why: {problems}")
    problems, _ = m.transition_problems(sets_of(V080), sets_of(grace_of(V080, minus_enum_values(V070, 2, "v0.4.0"))),
                                        closed_previous)
    expect(len(problems) == 1 and "is not the set a close could have dropped" in problems[0],
           f"history does not authorise reopening an OLDER set: {problems}")

    # A promote rolled back without keeping the promoted set accepted does fail those specs.
    problems, _ = moved(grace, with_next(V070, emitted("v0.9.0", published(components_v080(), **{"common.v1.X": {}}))))
    expect(len(problems) == 1 and "without keeping v0.8.0 accepted" in problems[0],
           f"dropping the promoted set is named as what fails specs: {problems}")


def test_a_forged_previous_and_other_multi_step_changes_fail():
    window, grace = with_next(V070, V080), grace_of(V080, V070)
    # The review's reproduction: previous = current minus two ErrorCode values, labelled older.
    forged = grace_of(V070, minus_enum_values(V070, 2, "v0.4.0"))
    for base, head, fragment, why in (
        (V070, forged, "is not the set a close could have dropped (no release since v0.7.0 became current",
         "a hand-added previous readmits an old projection"),
        (window, grace_of(V080, minus_enum_values(V070, 1, "v0.6.0")), "was not the current set on the base",
         "a promote that swaps in a different previous"),
        (grace, grace_of(V080, minus_enum_values(V070, 1, "v0.6.0")), "was not the current set on the base",
         "swapping previous for a different older set"),
        (V070, V080, "replaced in place", "regenerating the artifact over itself is a flag day"),
        (window, emitted("v0.7.0", published(components_v070(), **{"common.v1.Extra": {"type": "object"}})),
         "replaced in place", "an in-place regenerate during a window discards next"),
        # A stage must leave current byte-identical; this one also regenerates current in place.
        (V070, with_next(emitted("v0.7.0", published(components_v070(),
                                                      **{"common.v1.Extra": {"type": "object"}})), V080),
         "replaced in place", "a stage that also regenerates current is a flag day"),
        (V070, grace, "the base never accepted it as next", "stage and promote in one change"),
        (window, V080, "promote and close in one change", "promote and close in one change"),
        (grace, V070, "rolled back to previous", "rolling current back fails specs already on it"),
        (grace, with_next(V080, emitted("v0.9.0", published(components_v080(), **{"common.v1.X": {}})))
         , "dropped and `next` staged", "close and stage in one change"),
        (window, with_next(V070, emitted("v0.9.0", published(components_v080(), **{"common.v1.X": {}})))
         , "next was replaced", "replacing next instead of abandoning it first"),
    ):
        problems, summary = moved(base, head)
        expect(len(problems) == 1 and fragment in problems[0] and "not one window step" in problems[0],
               f"{why}: problems={problems} summary={summary!r}")


def test_a_relabel_corrects_provenance_and_never_moves_it_backwards():
    problems, summary = moved(V070, emitted("v0.38.0", components_v070()))
    expect(problems == [] and "relabelled current v0.7.0 -> v0.38.0" in summary,
           f"the same components under the release that really produced them: {problems} {summary!r}")
    problems, summary = moved(V070, projected(V070, "v1.60.0"))
    expect(problems == [] and "components unchanged" in summary, f"recording the projector: {problems}")
    problems, _ = moved(V070, emitted("v0.4.0", components_v070()))
    expect(len(problems) == 1 and "backwards" in problems[0], f"a relabel downwards: {problems}")
    problems, _ = moved(projected(V070, "v1.60.0"), V070)
    expect(len(problems) == 1 and "loses its recorded projector" in problems[0],
           f"dropping the recorded projector: {problems}")


def test_the_floor_only_rises_unless_a_rollback_is_declared():
    for base, head, ok, fragment, why in (
        ("v0.7.0", "v0.7.0", True, "unchanged", "unchanged"),
        ("v0.7.0", "v0.8.0", True, "raised v0.7.0 -> v0.8.0", "a raise"),
        ("v0.7.0", "v0.4.0", False, "lowers", "the review's downgrade: the floor lowered to a forged previous"),
        ("v0.8.0", "v0.7.0", False, "lowered_from: v0.8.0", "grace: lowering a raised floor back to previous"),
    ):
        problems, summary = m.floor_transition_problems(floors(base), floors(head))
        expect((problems == []) == ok and fragment in (summary if ok else problems[0]),
               f"{why}: problems={problems} summary={summary!r}")

    problems, summary = m.floor_transition_problems(floors("v0.8.0"), floors("v0.7.0", lowered_from="v0.8.0"))
    expect(problems == [] and "declared with lowered_from" in summary,
           f"a rollback declared in the reviewed file passes: {problems}")
    problems, _ = m.floor_transition_problems(floors("v0.8.0", lowered_from="v0.8.0"),
                                              floors("v0.7.0", lowered_from="v0.8.0"))
    expect(len(problems) == 1, "a declaration left over from an earlier rollback does not authorise a new one")
    problems, _ = m.floor_transition_problems(floors("v0.8.0"), floors("v0.7.0", lowered_from="v0.9.0"))
    expect(len(problems) == 1, "the declaration must name the floor being lowered")
    problems, _ = m.floor_transition_problems({"floors": {MODULE: "v0.8.0"}}, {"floors": {MODULE: "v0.7.0"}})
    expect(len(problems) == 1 and "min: v0.7.0" in problems[0], f"bare-string floors lower too: {problems}")
    problems, summary = m.floor_transition_problems(None, floors("v0.4.0"))
    expect(problems == [], "no floor on the base: nothing to hold the head to")


# ── the window, through the real CLI ─────────────────────────────────────────────────────────

def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER_PATH), *map(str, args)],
                          capture_output=True, text=True)


def test_window_lifecycle_ends_on_the_emitters_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref, new = tmp / "ref.json", tmp / "emitted.json"
        ref.write_text(m._reference_json(V070), encoding="utf-8")
        # The emitter's comment text changes between the two releases (the committed one still
        # names `go run ./...`), and a closed window must end on the NEW release's bytes anyway.
        new.write_text(m._reference_json(dict(V080, _comment=RENAMED_COMMENT)), encoding="utf-8")
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
        grace = json.loads(ref.read_text(encoding="utf-8"))
        expect(grace.get("_comment") == RENAMED_COMMENT and grace["previous"].get("_comment") == V070["_comment"],
               f"each comment travels with its set through a promote: {grace.get('_comment')!r}")
        expect(run("--components", ref, "--verify-floor", floor_old).returncode == 0,
               "promotion alone breaks nobody: the floor may still name previous")

        p = run("--components", ref, "--drop-set", "previous")
        expect(p.returncode == 0 and labels() == ["current v0.8.0"], f"drop previous closes it: {p.stdout}")
        expect(ref.read_bytes() == new.read_bytes(),
               "a closed window must leave exactly the bytes the emitter printed for the new vintage")
        p = run("--components", ref, "--verify-floor", floor_old)
        expect(p.returncode == 0 and "stale floor" in p.stdout,
               f"closing the window does not force the fleet-wide floor up: {p.stdout}")
        expect(run("--components", ref, "--verify-floor", floor_new).returncode == 0,
               "and a raised floor holds")

        # The next migration is not blocked on the fleet's pins: with the floor still at the old
        # vintage, a second window opens as soon as the first one is closed.
        newer = tmp / "emitted-v0.9.0.json"
        newer.write_text(m._reference_json(emitted("v0.9.0", dict(
            components_v080(), **{"common.v1.CursorPaginationMeta": {"type": "object"}}))), encoding="utf-8")
        p = run("--components", ref, "--stage-next", newer)
        expect(p.returncode == 0 and labels() == ["next v0.9.0", "current v0.8.0"],
               f"a second window opens over a stale floor: {p.stdout}{p.stderr}")
        expect(run("--components", ref, "--verify-floor", floor_old).returncode == 0,
               "and the stale floor stays valid while it is open")


def test_abandoning_next_restores_the_original_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        ref, new = tmp / "ref.json", tmp / "emitted.json"
        ref.write_text(m._reference_json(V070), encoding="utf-8")
        new.write_text(m._reference_json(dict(V080, _comment=RENAMED_COMMENT)), encoding="utf-8")
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


# ── the base commit, through the real CLI and real git ───────────────────────────────────────

class ControlsRepo:
    """A repository holding the two files the transition check reads, the way this one does."""

    def __init__(self, root: pathlib.Path, reference: dict, floor: dict) -> None:
        self.root = root
        root.mkdir(parents=True)
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "ci@example.test"),
                     ("config", "user.name", "CI Self Test"), ("config", "commit.gpgsign", "false")):
            self.git(*args)
        self.write(reference, floor)
        self.commit("base")

    @property
    def reference(self) -> pathlib.Path:
        return self.root / "controls" / "common-v1-components.json"

    @property
    def floors(self) -> pathlib.Path:
        return self.root / "controls" / "module-floors.yaml"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self, reference: dict, floor: dict) -> None:
        self.reference.parent.mkdir(parents=True, exist_ok=True)
        self.reference.write_text(m._reference_json(reference), encoding="utf-8")
        self.floors.write_text(json.dumps(floor), encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def release(self, version: str, rev: str = "HEAD") -> None:
        """Tag `rev` the way release.yaml does: an annotated vX.Y.Z tag (v1 moves beside it)."""
        self.git("tag", "-a", version, "-m", version, rev)
        self.git("tag", "-f", "-a", "v1", "-m", f"Moving major tag: {version}", f"{version}^{{commit}}")

    def verify(self, *extra, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CHECKER_PATH), "--components", str(self.reference),
                               "--verify-floor", str(self.floors), *map(str, extra)],
                              capture_output=True, text=True, env=env)


def event_env(tmp: pathlib.Path, name: str, payload: dict) -> dict:
    path = tmp / f"event-{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {**os.environ, "GITHUB_EVENT_NAME": name, "GITHUB_EVENT_PATH": str(path)}


def test_the_reviews_downgrade_passes_alone_and_fails_against_the_base():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V070, floors("v0.7.0"))
        base = repo.git("rev-parse", "HEAD")
        repo.write(grace_of(V070, minus_enum_values(V070, 2, "v0.4.0")), floors("v0.4.0"))

        alone = repo.verify()
        expect(alone.returncode == 0,
               f"on its own the forged file is well-formed; that is why the base is needed: {alone.stdout}")
        p = repo.verify("--base", base)
        expect(p.returncode == 1 and "is not the set a close could have dropped" in p.stdout
               and "lowers the github.com/coderaxis/platform-contracts-go floor from v0.7.0 to v0.4.0" in p.stdout,
               f"against the base, both the forged previous and the lowered floor fail: {p.stdout}{p.stderr}")

        repo.write(with_next(V070, V080), floors("v0.7.0"))
        p = repo.verify("--base", base)
        expect(p.returncode == 0 and "staged next v0.8.0" in p.stdout and "floor unchanged at v0.7.0" in p.stdout,
               f"a real stage passes and says what moved: {p.stdout}{p.stderr}")

        p = repo.verify("--base", "0123456789abcdef0123456789abcdef01234567")
        expect(p.returncode == 2 and "cannot read the base commit" in p.stdout,
               f"an unreadable base fails closed: {p.stdout}{p.stderr}")


def test_without_a_release_the_base_is_resolved_from_the_github_event():
    # This repository has no release tag, as in a fork or a fresh clone made without tags, so
    # there is no last release to hold the change against and the event names the base.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", grace_of(V080, V070), floors("v0.8.0"))
        main_tip = repo.git("rev-parse", "HEAD")
        origin = tmp / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo.root), str(origin)], check=True)
        repo.git("remote", "add", "origin", str(origin))
        repo.git("fetch", "-q", "origin")
        repo.git("switch", "-q", "-c", "lower-the-floor")
        repo.write(grace_of(V080, V070), floors("v0.7.0"))
        head = repo.commit("lower the floor back to previous")

        # A pull_request checkout is the merge commit; its base is pull_request.base.sha.
        env = event_env(tmp, "pull_request", {"pull_request": {"base": {"sha": main_tip}, "head": {"sha": head}}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 1 and "lowers" in p.stdout and "lowered_from: v0.8.0" in p.stdout,
               f"pull_request: lowering a raised floor back to previous fails: {p.stdout}{p.stderr}")

        # A normal push is held against its before-SHA.
        env = event_env(tmp, "push", {"before": main_tip, "after": head, "created": False, "forced": False})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 1 and f"push before-SHA {main_tip[:12]}" in p.stdout,
               f"push: held against before: {p.stdout}{p.stderr}")

        # The first push of a branch has no before-SHA; it is held against the default branch.
        env = event_env(tmp, "push", {"before": "0" * 40, "after": head, "created": True,
                                      "repository": {"default_branch": "main"}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 1 and "merge base" in p.stdout and "origin/main" in p.stdout,
               f"push (created): held against the merge base with origin/main: {p.stdout}{p.stderr}")

        # A before-SHA this clone has but HEAD does not contain (a rewritten branch whose payload
        # still says forced=false) is not a base: the merge base is used instead.
        repo.git("switch", "-q", "-c", "elsewhere", main_tip)
        (repo.root / "README.md").write_text("a commit HEAD will not contain\n", encoding="utf-8")
        elsewhere = repo.commit("elsewhere")
        repo.git("switch", "-q", "lower-the-floor")
        env = event_env(tmp, "push", {"ref": "refs/heads/lower-the-floor", "before": elsewhere, "after": head,
                                      "created": False, "forced": False, "repository": {"default_branch": "main"}})
        p = repo.verify("--base-from-event", env=env)
        expect("push before-SHA" not in p.stdout and f"merge base {main_tip[:12]} with origin/main" in p.stdout,
               f"push (before not an ancestor of HEAD): held against the merge base: {p.stdout}{p.stderr}")
        env = event_env(tmp, "push", {"ref": "refs/heads/main", "before": elsewhere, "after": head,
                                      "created": False, "forced": False, "repository": {"default_branch": "main"}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 2 and "cannot be held against anything" in p.stdout,
               f"push to main (before not an ancestor of HEAD) fails closed: {p.stdout}{p.stderr}")

        # Declared in the reviewed file, the rollback passes.
        repo.write(grace_of(V080, V070), floors("v0.7.0", lowered_from="v0.8.0"))
        repo.commit("declare the rollback")
        env = event_env(tmp, "pull_request", {"pull_request": {"base": {"sha": main_tip}}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 0 and "declared with lowered_from" in p.stdout,
               f"a declared rollback passes: {p.stdout}{p.stderr}")

        # A force push to the default branch has no before-SHA and nothing to fall back to.
        env = event_env(tmp, "push", {"ref": "refs/heads/main", "before": main_tip, "after": head,
                                      "forced": True, "repository": {"default_branch": "main"}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 2 and "cannot be held against anything" in p.stdout,
               f"push (forced, to main) fails closed: {p.stdout}{p.stderr}")

        # No change under review: only the floor rule applies. A missing base where one is
        # required fails closed.
        p = repo.verify("--base-from-event", env=event_env(tmp, "schedule", {}))
        expect(p.returncode == 0 and "describes no change" in p.stdout, f"schedule: {p.stdout}{p.stderr}")
        p = repo.verify("--base-from-event", env=event_env(tmp, "pull_request", {"pull_request": {}}))
        expect(p.returncode == 2 and "no base SHA" in p.stdout, f"pull_request without a base: {p.stdout}")


def test_a_pull_request_is_judged_against_main_now_not_its_branch_point():
    # main made two window steps after this branch was cut. The pull request itself touches
    # neither file, so it must pass. Its merge base would charge both steps to it.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V070, floors("v0.7.0"))
        branch_point = repo.git("rev-parse", "HEAD")
        repo.git("switch", "-q", "-c", "unrelated")
        (repo.root / "README.md").write_text("unrelated\n", encoding="utf-8")
        pr_head = repo.commit("unrelated change")
        repo.git("switch", "-q", "main")
        repo.write(with_next(V070, V080), floors("v0.7.0"))
        repo.commit("stage")
        repo.write(grace_of(V080, V070), floors("v0.7.0"))
        main_now = repo.commit("promote")
        repo.git("merge", "-q", "--no-ff", "--no-edit", "unrelated")  # what a pull_request checks out

        env = event_env(tmp, "pull_request", {"pull_request": {"base": {"sha": main_now}, "head": {"sha": pr_head}}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 0 and "accepted sets unchanged" in p.stdout,
               f"the pull request changed nothing about the reference: {p.stdout}{p.stderr}")
        p = repo.verify("--base", branch_point)
        expect(p.returncode == 1 and "the base never accepted it as next" in p.stdout,
               f"held against the branch point instead, main's two steps would read as this change's: {p.stdout}")


# ── the last release is the base ─────────────────────────────────────────────────────────────

def push_to_main(tmp: pathlib.Path, before: str, after: str) -> dict:
    return event_env(tmp, "push", {"ref": "refs/heads/main", "before": before, "after": after,
                                   "created": False, "forced": False,
                                   "repository": {"default_branch": "main"}})


def test_a_red_commit_is_not_released_inside_the_next_one():
    # The review's reproduction. main has no required checks and release.yaml releases a commit
    # once CI passes for THAT commit, so a forbidden change that turned CI red once must not read
    # as "unchanged" to the next, unrelated commit - which is what its before-SHA says.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V080, floors("v0.8.0"))
        released = repo.git("rev-parse", "HEAD")
        repo.release("v1.0.0")

        repo.write(V070, floors("v0.7.0"))
        downgrade = repo.commit("A: roll current back and lower the floor, undeclared")
        p = repo.verify("--base-from-event", env=push_to_main(tmp, released, downgrade))
        expect(p.returncode == 1 and "the last release v1.0.0" in p.stdout and "replaced in place" in p.stdout
               and "lowers" in p.stdout, f"A is red against the last release: {p.stdout}{p.stderr}")

        (repo.root / "README.md").write_text("an unrelated edit\n", encoding="utf-8")
        unrelated = repo.commit("B: unrelated")
        p = repo.verify("--base", downgrade)
        expect(p.returncode == 0 and "accepted sets unchanged" in p.stdout,
               f"against its before-SHA, B reads as unchanged - the gap: {p.stdout}{p.stderr}")
        p = repo.verify("--base-from-event", env=push_to_main(tmp, downgrade, unrelated))
        expect(p.returncode == 1 and "the last release v1.0.0" in p.stdout and "replaced in place" in p.stdout
               and "lowers" in p.stdout and "is the last release, and every change since it counts" in p.stdout,
               f"B stays red until A is fixed, because the fleet has not received A: {p.stdout}{p.stderr}")

        # The fix is a plain revert of A, which the before-SHA would read as a forbidden step of its own.
        repo.git("revert", "--no-edit", downgrade)
        fixed = repo.git("rev-parse", "HEAD")
        p = repo.verify("--base", unrelated)
        expect(p.returncode == 1, f"against its before-SHA, the revert itself looks like a flag day: {p.stdout}")
        p = repo.verify("--base-from-event", env=push_to_main(tmp, unrelated, fixed))
        expect(p.returncode == 0 and "accepted sets unchanged" in p.stdout and "floor unchanged at v0.8.0" in p.stdout,
               f"against the last release, the revert passes: {p.stdout}{p.stderr}")


def test_deleting_the_artifact_then_adding_a_forged_one_is_held_against_what_came_before():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        forged = grace_of(V080, minus_enum_values(V070, 2, "v0.4.0"))
        for released in (True, False):
            repo = ControlsRepo(tmp / f"ci-workflows-{released}", V080, floors("v0.8.0"))
            if released:
                repo.release("v1.0.0")
            repo.reference.unlink()
            deleted = repo.commit("C: delete the artifact")
            repo.write(forged, floors("v0.8.0"))
            readded = repo.commit("D: add a forged one")

            p = repo.verify("--base", deleted)
            expect(p.returncode == 1 and "has no usable reference artifact" in p.stdout
                   and "is not the set a close could have dropped" in p.stdout and "new in this change" not in p.stdout,
                   f"a base without the artifact looks back to its last usable state (released={released}): "
                   f"{p.stdout}{p.stderr}")
            p = repo.verify("--base-from-event", env=push_to_main(tmp, deleted, readded))
            expect(p.returncode == 1 and "is not the set a close could have dropped" in p.stdout,
                   f"D pushed after C is red (released={released}): {p.stdout}{p.stderr}")


def test_a_window_step_waits_for_the_release_of_the_step_before_it():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V070, floors("v0.7.0"))
        repo.release("v1.9.0")
        repo.write(with_next(V070, V080), floors("v0.7.0"))
        stage = repo.commit("stage")
        repo.write(grace_of(V080, V070), floors("v0.7.0"))
        promote = repo.commit("promote")

        p = repo.verify("--base-from-event", env=push_to_main(tmp, stage, promote))
        expect(p.returncode == 1 and "the base never accepted it as next" in p.stdout
               and "If an earlier window step has landed but is not released yet" in p.stdout,
               f"a promote landing before its stage is released is two steps for one release: {p.stdout}{p.stderr}")

        # v1.10.0 sorts before v1.9.0 as a string; releases are ordered as versions.
        repo.release("v1.10.0", stage)
        p = repo.verify("--base-from-event", env=push_to_main(tmp, stage, promote))
        expect(p.returncode == 0 and "the last release v1.10.0" in p.stdout and "promoted next to current" in p.stdout,
               f"once the stage is released, the same promote is one step: {p.stdout}{p.stderr}")

        env = event_env(tmp, "pull_request", {"pull_request": {"base": {"sha": stage}, "head": {"sha": promote}}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 0 and "the last release v1.10.0" in p.stdout,
               f"a pull request is held against the same last release: {p.stdout}{p.stderr}")


def test_a_promote_or_a_close_rolls_back_with_git_revert():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V070, floors("v0.7.0"))
        repo.release("v1.0.0")
        emitted_v080 = tmp / "emitted-v0.8.0.json"
        emitted_v080.write_text(m._reference_json(dict(V080, _comment=RENAMED_COMMENT)), encoding="utf-8")

        def tool(*args):
            p = run("--components", repo.reference, *args)
            if p.returncode != 0:
                raise AssertionError(f"window tool {args} failed: {p.stdout}{p.stderr}")

        tool("--stage-next", emitted_v080)
        repo.release("v1.1.0", repo.commit("stage"))
        tool("--promote-next")
        promote = repo.commit("promote")
        repo.release("v1.2.0", promote)
        tool("--drop-set", "previous")
        close = repo.commit("close")
        repo.release("v1.3.0", close)

        repo.git("revert", "--no-edit", close)
        p = repo.verify("--base-from-event", env=push_to_main(tmp, close, repo.git("rev-parse", "HEAD")))
        expect(p.returncode == 0 and "reopened previous v0.7.0" in p.stdout
               and "the last release before the close (v1.2.0) carried previous v0.7.0" in p.stdout,
               f"a reverted close passes: previous is what the release before the close carried: "
               f"{p.stdout}{p.stderr}")

        repo.git("reset", "-q", "--hard", close)
        repo.write(grace_of(dict(V080, _comment=RENAMED_COMMENT), minus_enum_values(V070, 1, "v0.6.0")),
                   floors("v0.7.0"))
        p = repo.verify("--base-from-event", env=push_to_main(tmp, close, repo.commit("forged reopen")))
        expect(p.returncode == 1 and "is not the set a close could have dropped" in p.stdout
               and "carried previous v0.7.0" in p.stdout, f"a reopen history does not support fails: {p.stdout}{p.stderr}")

        # A stage opened and abandoned after the close does not hide the grace release behind it.
        repo.git("reset", "-q", "--hard", close)
        emitted_v090 = tmp / "emitted-v0.9.0.json"
        emitted_v090.write_text(m._reference_json(emitted("v0.9.0", published(
            components_v080(), **{"common.v1.X": {}}))), encoding="utf-8")
        tool("--stage-next", emitted_v090)
        repo.release("v1.4.0", repo.commit("stage v0.9.0"))
        tool("--drop-set", "next")
        abandon = repo.commit("abandon v0.9.0")
        repo.release("v1.5.0", abandon)
        repo.git("revert", "--no-edit", close)
        p = repo.verify("--base-from-event", env=push_to_main(tmp, abandon, repo.git("rev-parse", "HEAD")))
        expect(p.returncode == 0 and "(v1.2.0) carried previous v0.7.0" in p.stdout,
               f"a close reverted after a stage was abandoned still reopens: {p.stdout}{p.stderr}")

        # A promote reverted on a branch cut from the grace release. v1.3.0 is newer but not in it.
        repo.git("switch", "-q", "-c", "demote", "v1.2.0")
        repo.git("revert", "--no-edit", promote)
        env = event_env(tmp, "pull_request", {"pull_request": {"base": {"sha": promote}}})
        p = repo.verify("--base-from-event", env=env)
        expect(p.returncode == 0 and "demoted current v0.8.0 back to next beside v0.7.0" in p.stdout
               and "the last release v1.2.0 this ref contains (the newest release, v1.5.0, is not in it)" in p.stdout,
               f"a reverted promote passes: {p.stdout}{p.stderr}")


def test_a_projection_replaced_in_place_cannot_be_reopened_from_history():
    # Before windows existed every regeneration replaced current in place. The real history has
    # release v1.15.3 on contracts v0.4.0 and later releases on v0.7.0, with no release between
    # them carrying a grace set. Copying v1.15.3's current in as `previous` is not a rollback of a
    # close, however genuinely current it once was.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        repo = ControlsRepo(tmp / "ci-workflows", V070, floors("v0.7.0"))
        repo.release("v1.0.0")
        repo.write(V080, floors("v0.7.0"))
        flag_day = repo.commit("regenerate in place, as before windows")
        repo.release("v1.1.0", flag_day)
        repo.write(grace_of(V080, V070), floors("v0.7.0"))
        p = repo.verify("--base-from-event", env=push_to_main(tmp, flag_day, repo.commit("reopen v0.7.0")))
        expect(p.returncode == 1 and "no release since v0.8.0 became current carried a previous set" in p.stdout,
               f"a set that was current in an old release is not reopened without a grace release: {p.stdout}{p.stderr}")


def test_release_yaml_holds_the_release_against_the_tag_it_releases_from():
    # CI is not a required check on main, so this is the binding gate: nothing reaches @v1 unless
    # the delta from the last release makes one window step and does not silently lower the floor.
    wf = m.yaml.safe_load((HERE.parent / ".github" / "workflows" / "release.yaml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["release"]["steps"]

    def index(fragment: str) -> int:
        found = [i for i, s in enumerate(steps) if fragment in str(s.get("run", ""))]
        return found[0] if found else -1

    gate = index("scripts/check-api-contract.py --verify-floor controls/module-floors.yaml --base \"${PREV}\"")
    tag = index("git push --force origin v1")
    expect(0 <= gate < tag, f"release.yaml must run the transition check before it moves v1: gate={gate} tag={tag}")
    if gate >= 0:
        step = steps[gate]
        expect(step.get("env", {}).get("PREV") == "${{ steps.version.outputs.previous }}"
               and step.get("if") == "steps.version.outputs.release == 'true'",
               f"the gate holds the release against the tag it bumps from: {step}")
        expect(any("setup-python" in str(s.get("uses", "")) for s in steps[:gate]),
               "the runner's system Python refuses pip installs (PEP 668); set up Python before the gate")


def test_the_stale_vintage_mutation_cannot_collide_with_an_accepted_set():
    # ci.yaml's mutation test once dropped the last ErrorCode value from current. After a
    # migration that appended exactly one value - the usual contracts change - that fixture IS
    # previous, so API-0007 passed it and this repository's CI went red on the promote commit.
    nxt = json.loads(json.dumps(V070))
    nxt["components"]["common.v1.ErrorCode"]["enum"].append("METHOD_NOT_ALLOWED")
    nxt["source"]["version"] = "v0.8.0"
    sets = sets_of(grace_of(nxt, V070))

    dropped = published(nxt["components"])
    dropped["common.v1.ErrorCode"]["enum"] = dropped["common.v1.ErrorCode"]["enum"][:-1]
    expect(m.compare_to_accepted_sets(dropped, sets).ok,
           "the old fixture reproduces previous whole, which is why ci.yaml no longer uses it")

    for cset in sets:
        mutated = published(cset.components)
        mutated["common.v1.ErrorCode"]["enum"].append("API_0007_MUTATION_NOT_A_REAL_CODE")
        f = m.compare_to_accepted_sets(mutated, sets)
        expect(not f.ok, f"the sentinel mutation of {cset.label} matches no accepted set: {f}")


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
          "floor is never above current, a window closes on the emitter's bytes, and against "
          "the last release the artifact moves one step (a promote or close may be reverted), "
          "the floor never silently falls, and release.yaml holds @v1 to the same rule")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
