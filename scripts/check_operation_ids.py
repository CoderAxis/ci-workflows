#!/usr/bin/env python3
"""OperationId governance guard (ADR-0006).

`operationId`s are semver-governed public API identifiers: generated SDKs, API
catalogs, and observability keys depend on them, so a rename/removal is a breaking
change. This script is the CI enforcement point.

Two modes:

  --check   (CI gate) Validate the contract's operationIds:
              * every operation has an operationId
              * operationIds are unique
              * each matches the ADR-0006 convention
                (lowerCamelCase, bounded-context-first)
              * the spec's operationId set matches docs/openapi.operationids.lock.json
                (freshness) and the lock version matches info.version.

  --write   (make openapi-operationids) Regenerate the lockfile from the spec while
            enforcing semver against the *previous* committed lock:
              * removing/renaming an operationId requires a MAJOR info.version bump
              * adding an operationId requires at least a MINOR info.version bump
            The lock only moves when the version moves correctly, so the breaking
            -change rule is enforced at the moment the baseline changes. The lock is
            CODEOWNERS-guarded, so every change is also a reviewable diff.

The lock is an object-based registry so long-term governance questions stay
answerable ("when was this introduced?", "is it deprecated?", "who may call it?").
Each entry carries `operationId`, `since` (the info.version that introduced it),
`deprecated`, and `visibility` (public | partner | admin | internal). `since`,
`deprecated`, and any curated `visibility` override are preserved across
regenerations. The registry is machine-readable and validated against
`docs/openapi.operationids.schema.json`; the governance policy lives in ADR-0006.

Usage:
    python3 scripts/check_operation_ids.py --check
    python3 scripts/check_operation_ids.py --write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SPEC_PATH = Path("docs/openapi.json")
LOCK_PATH = Path("docs/openapi.operationids.lock.json")
SCHEMA_REF = "./openapi.operationids.schema.json"

# Bounded contexts allowed as the operationId prefix (ADR-0006).
BOUNDED_CONTEXTS = ("auth", "token", "identity", "platform", "session")
# Exposure tiers, from most to least exposed (ADR-0006). Carried in metadata, not
# in the operationId itself — so names stay resource-first and consistent.
VISIBILITIES = ("public", "partner", "admin", "internal")
NAME_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"::error::{msg}")
    sys.exit(1)


def derive_visibility(path: str) -> str:
    """Default exposure tier from the route prefix; may be overridden in the lock."""
    if path.startswith("/internal/platform/"):
        return "admin"
    if path.startswith("/internal/"):
        return "internal"
    return "public"


def load_json(path: Path):
    if not path.exists():
        die(f"{path} is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def spec_operation_ids(spec: dict) -> dict[str, tuple[str, str]]:
    """Return {operationId: (METHOD, path)} and validate structural rules."""
    out: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            oid = op.get("operationId")
            if not oid:
                errors.append(f"{method.upper()} {path}: missing operationId")
                continue
            if oid in out:
                errors.append(f"duplicate operationId {oid!r} ({method.upper()} {path})")
                continue
            if not NAME_RE.match(oid):
                errors.append(f"operationId {oid!r} is not lowerCamelCase")
            elif not any(
                oid == ctx or (oid.startswith(ctx) and oid[len(ctx)].isupper())
                for ctx in BOUNDED_CONTEXTS
            ):
                errors.append(
                    f"operationId {oid!r} does not start with a bounded context "
                    f"{BOUNDED_CONTEXTS}"
                )
            out[oid] = (method.upper(), path)
    if errors:
        for e in errors:
            print(f"::error::  - {e}")
        die("operationId structural validation failed")
    return out


def parse_semver(v: str) -> tuple[int, int, int]:
    core = str(v).split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        die(f"info.version {v!r} is not a valid semver")
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def lock_operations(lock: dict) -> dict[str, dict]:
    """Return {operationId: entry} from the object-based registry."""
    out: dict[str, dict] = {}
    for entry in lock.get("operations", []):
        if not isinstance(entry, dict) or "operationId" not in entry:
            die(f"malformed lock entry: {entry!r}")
        out[entry["operationId"]] = entry
    return out


def validate_lock_metadata(lock: dict) -> None:
    """Validate the registry's per-entry metadata (shape, enums, types)."""
    errors: list[str] = []
    for entry in lock.get("operations", []):
        oid = entry.get("operationId", "?")
        for field in ("operationId", "since", "deprecated", "visibility"):
            if field not in entry:
                errors.append(f"{oid}: missing required field {field!r}")
        vis = entry.get("visibility")
        if vis is not None and vis not in VISIBILITIES:
            errors.append(f"{oid}: visibility {vis!r} not in {VISIBILITIES}")
        if "deprecated" in entry and not isinstance(entry["deprecated"], bool):
            errors.append(f"{oid}: deprecated must be a boolean")
    if errors:
        for e in errors:
            print(f"::error::  - {e}")
        die("operationId registry metadata validation failed")


def do_check() -> None:
    spec = load_json(SPEC_PATH)
    lock = load_json(LOCK_PATH)
    ids = set(spec_operation_ids(spec))
    validate_lock_metadata(lock)
    lock_ids = set(lock_operations(lock))
    spec_version = str(spec.get("info", {}).get("version", ""))
    lock_version = str(lock.get("version", ""))

    if lock_version != spec_version:
        die(
            f"lock version {lock_version!r} != info.version {spec_version!r}; "
            "run `make openapi-operationids`"
        )

    added = sorted(ids - lock_ids)
    removed = sorted(lock_ids - ids)
    if added or removed:
        if added:
            print(f"::error::operationIds added but lock is stale: {added}")
        if removed:
            print(f"::error::operationIds removed but lock is stale: {removed}")
        die(
            "operationId set drifted from docs/openapi.operationids.lock.json; "
            "run `make openapi-operationids` and get the lock diff reviewed"
        )

    print(f"ok: {len(ids)} operationIds governed, lock in sync at v{spec_version}")


def do_write() -> None:
    spec = load_json(SPEC_PATH)
    spec_ops = spec_operation_ids(spec)
    ids = set(spec_ops)
    spec_version = str(spec.get("info", {}).get("version", ""))

    prev = json.loads(LOCK_PATH.read_text(encoding="utf-8")) if LOCK_PATH.exists() else {}
    prev_ops = lock_operations(prev)
    prev_ids = set(prev_ops)
    prev_version = str(prev.get("version", "0.0.0")) or "0.0.0"

    added = sorted(ids - prev_ids)
    removed = sorted(prev_ids - ids)

    if added or removed:
        pv = parse_semver(prev_version)
        sv = parse_semver(spec_version)
        if removed and not (sv[0] > pv[0]):
            print(f"::error::removed/renamed operationIds: {removed}")
            die(
                f"removing or renaming an operationId is a BREAKING change; bump "
                f"info.version MAJOR (from {prev_version}) before regenerating the lock"
            )
        if added and not (sv[:2] > pv[:2]):
            print(f"::error::added operationIds: {added}")
            die(
                f"adding an operationId requires at least a MINOR info.version bump "
                f"(from {prev_version}) before regenerating the lock"
            )

    operations = []
    for oid in sorted(ids):
        prior = prev_ops.get(oid)
        _, path = spec_ops[oid]
        operations.append(
            {
                "operationId": oid,
                # preserve the version that first introduced the op
                "since": str(prior.get("since", spec_version)) if prior else spec_version,
                "deprecated": bool(prior.get("deprecated", False)) if prior else False,
                # default the exposure tier from the route; keep a curated override
                "visibility": str(prior["visibility"])
                if prior and prior.get("visibility") in VISIBILITIES
                else derive_visibility(path),
            }
        )

    payload = {
        "$schema": SCHEMA_REF,
        "version": spec_version,
        "operations": operations,
    }
    LOCK_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if added:
        print(f"added: {added}")
    if removed:
        print(f"removed: {removed}")
    print(f"wrote {LOCK_PATH} with {len(ids)} operationIds at v{spec_version}")


def main() -> None:
    ap = argparse.ArgumentParser(description="OperationId governance guard (ADR-0006)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="CI gate: validate + freshness")
    g.add_argument("--write", action="store_true", help="regenerate the lockfile (semver-enforced)")
    args = ap.parse_args()
    if args.check:
        do_check()
    else:
        do_write()


if __name__ == "__main__":
    main()
