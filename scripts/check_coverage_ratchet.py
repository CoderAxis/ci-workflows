#!/usr/bin/env python3
"""Coverage ratchet (CI). Run from a repo root.

The tier threshold says what a repo's coverage OUGHT to be. For a repo that is
a long way below it, a gate that only compares against the target fails on
every commit and therefore constrains nothing: the number is free to fall as
long as it was already failing. This turns the shortfall into a recorded floor
that can only move upward.

    coverage.floor.json   the repo's current floor, committed and reviewable

Rules, given a measured percentage and the tier threshold:

  * at or above the threshold -- the repo has cleared the bar and must not keep
    a floor. A floor left behind after a repo passes is a trapdoor: coverage
    could later fall from 80% to a stale 40% floor and the gate would allow it.
    --write deletes the file.

  * below the threshold with no floor file -- fail, exactly as before. Debt is
    admitted deliberately, in a diff someone approves; it is not something a
    repo can drift into.

  * below the floor -- fail. This is the whole point of the mechanism.

  * meaningfully above the floor -- fail, asking for the floor to be raised, so
    an improvement is locked in rather than left as headroom to lose again.
    --write raises it.

  * within the band around the floor -- pass, and report the distance still to
    go.

The band matters. Coverage moves a little for reasons that are not changes in
testing (a table-driven case added to an existing test, map iteration order in
a loop over fixtures), and a gate that fires on 0.1% would be edited out of the
pipeline within a week.

Usage:
    python3 scripts/check_coverage_ratchet.py --pct 50.9 --threshold 70
    python3 scripts/check_coverage_ratchet.py --pct 50.9 --threshold 70 --write
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

FLOOR_FILE = "coverage.floor.json"

# Below the floor by more than this is a regression; above it by more than RAISE is an improvement
# worth recording. Between the two the run passes untouched.
SLACK = 0.5
RAISE = 2.0


def _fail(msg: str) -> None:
    print(f"::error::{msg}")


class FloorError(Exception):
    """The floor file exists but cannot be trusted, which is not the same as absent."""


def load_floor(path: str) -> tuple[float | None, dict]:
    if not os.path.exists(path):
        return None, {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise FloorError(f"{path} is present but unreadable: {exc}") from exc
    if not isinstance(data, dict) or "floor" not in data:
        raise FloorError(f"{path} must be a JSON object with a 'floor' key")
    try:
        floor = float(data["floor"])
    except (TypeError, ValueError) as exc:
        raise FloorError(f"{path} has a non-numeric floor: {data['floor']!r}") from exc
    if not 0.0 <= floor <= 100.0:
        raise FloorError(f"{path} floor {floor} is not a percentage")
    # A floor of zero passes every run, so on its own it is a green check that means "this repo has
    # no tests" without saying so. Requiring the sentence to be written makes the claim visible in
    # review and in the file, rather than inferable only by noticing the number.
    if floor == 0.0 and not str(data.get("reason", "")).strip():
        raise FloorError(
            f"{path} records a floor of 0, which cannot fail. Say why in 'reason' — a repo with no "
            f"tests should be readable as such from the file, not from the absence of a number."
        )
    return floor, data


def write_floor(path: str, pct: float, existing: dict, reason: str) -> None:
    payload = {
        "floor": round(pct, 1),
        "recorded": datetime.date.today().isoformat(),
        # Carried across writes so the justification is not lost each time the floor moves. A floor
        # with no reason is indistinguishable from one nobody has looked at.
        "reason": existing.get("reason") or reason,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def evaluate(pct: float, threshold: float, path: str, write: bool) -> int:
    try:
        floor, existing = load_floor(path)
    except FloorError as exc:
        _fail(str(exc))
        return 1

    if pct >= threshold:
        if floor is None:
            print(f"coverage {pct}% meets the {threshold}% required for this tier")
            return 0
        if write:
            os.remove(path)
            print(f"coverage {pct}% cleared the {threshold}% target; removed {path}")
            return 0
        _fail(
            f"coverage {pct}% has cleared the {threshold}% target, so {path} must go. "
            f"A floor left behind lets coverage fall back to {floor}% unnoticed."
        )
        return 1

    if floor is None:
        if write:
            write_floor(path, pct, existing, "recorded by the coverage ratchet")
            print(f"recorded a floor of {round(pct, 1)}% in {path}")
            return 0
        _fail(
            f"coverage {pct}% is below the {threshold}% required for this tier. "
            f"If the shortfall is accepted for now, record it: "
            f"python3 scripts/check_coverage_ratchet.py --pct {pct} --threshold {threshold} --write"
        )
        return 1

    if pct < floor - SLACK:
        _fail(
            f"coverage fell to {pct}% from a recorded floor of {floor}%. "
            f"The floor only moves upward: restore the coverage or explain, in the same change, "
            f"why the tests that used to cover this code no longer need to."
        )
        return 1

    if pct > floor + RAISE:
        if write:
            write_floor(path, pct, existing, "recorded by the coverage ratchet")
            print(f"raised the floor in {path} to {round(pct, 1)}%")
            return 0
        _fail(
            f"coverage improved to {pct}% against a floor of {floor}%. Raise it so the improvement "
            f"cannot be lost again: "
            f"python3 scripts/check_coverage_ratchet.py --pct {pct} --threshold {threshold} --write"
        )
        return 1

    print(
        f"coverage {pct}% holds the recorded floor of {floor}% "
        f"({round(threshold - pct, 1)} points below the {threshold}% target for this tier)"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pct", type=float, required=True, help="measured coverage percentage")
    ap.add_argument("--threshold", type=float, required=True, help="the tier's required percentage")
    ap.add_argument("--floor-file", default=FLOOR_FILE)
    ap.add_argument("--write", action="store_true", help="record or clear the floor instead of failing")
    args = ap.parse_args()
    return evaluate(args.pct, args.threshold, args.floor_file, args.write)


if __name__ == "__main__":
    sys.exit(main())
