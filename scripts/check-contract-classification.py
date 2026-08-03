#!/usr/bin/env python3
"""Enforce that a repository's published contracts match its declared transport.

The invariant, in one line:

    A repository publishes an OpenAPI document IF AND ONLY IF its catalog entry
    declares interfaces.rest.

Both halves matter, and only one of them was ever checked. service-ci.yaml gates the
`contract` and `openapi-generator` jobs on `has_rest`, which is derived from the catalog -
so a service that declares no REST interface has its spec validated by nothing, regenerated
by nothing, and drift-checked by nothing. Thirteen services in this fleet are in exactly that
position: they ship docs/openapi.json, they declare only gRPC (or nothing), and no CI lane
has ever read the document they publish. The gate that would have caught it is the one
nobody wrote, because `has_rest == false` reads as "not applicable" rather than as "this
repo must therefore have no spec".

The other half fails the opposite way. A service that declares interfaces.rest and then
deletes or never generates its spec silently loses its entire HTTP lane - both jobs skip on
a missing file rather than failing on it.

WHY THE CATALOG DECIDES AND THE FILESYSTEM DOES NOT. controls/api-contract.yaml gates its
own controls with `applies_when: http-api`, defined as "the repo carries docs/openapi.json".
That is a second classification mechanism, and it disagrees with this one by construction: a
gRPC-only service holding a stale spec is judged an HTTP service by that rule and a gRPC
service by service-ci.yaml. Presence-based classification is also escapable in the direction
that matters - delete the file and the controls stop applying. The catalog is the single
declared answer to "what does this service expose", it is reviewable, and it is already what
CI resolves identity from.

RATCHET. The twelve existing violations are a program of work, not a PR, so
controls/contract-classification-allowlist.yaml freezes them with a recorded disposition -
whether the fix is to declare the interface or to delete the document. An allowlisted repo
reports its finding as a notice and passes. Anything not in that file fails, so a thirteenth
cannot appear while the twelve are worked off, and removing an entry is a reviewable act.

    ./scripts/check-contract-classification.py --repo . --service order-service --has-rest true
    ./scripts/check-contract-classification.py --catalog docs/core-docs/catalog/services \
        --workspace .                                    # fleet-wide audit, for local use

Exit 0 when the invariant holds (or is allowlisted), 1 on a violation, 2 on bad inputs.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a policy failure
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = SELF_REPO / "controls" / "contract-classification-allowlist.yaml"

# The one published document. Deliberately a single fixed path rather than a glob: the fleet
# had four rival spec files in one repo before API-0004, and a gate that accepts "any spec-ish
# file" cannot tell the published contract from a leftover.
SPEC_PATH = "docs/openapi.json"

# What an allowlist entry must say it is going to do about itself. Recorded rather than
# free-text so the remaining work can be counted per disposition instead of read.
DISPOSITIONS = {
    # The service does serve a real HTTP API; the catalog is wrong and must declare it. This
    # is the answer whenever the spec describes routes the service genuinely mounts.
    "declare-rest",
    # No meaningful HTTP API remains, or what remains is health, metrics or webhooks - which
    # are operational endpoints, not a published contract. The document goes.
    "delete-spec",
}


def load_allowlist(path: Path) -> dict:
    """Return {service: entry}, or exit 2 if the file is unreadable or malformed.

    A missing allowlist is an empty allowlist, which holds every repo to the invariant. That
    is the safe direction: the failure mode of a gate that cannot find its exemption list
    should be too strict, never silently permissive.
    """
    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"::error::cannot read the allowlist at {path}: {exc}")
        raise SystemExit(2)

    entries = {}
    for item in doc.get("allow") or []:
        svc = item.get("service")
        disp = item.get("disposition")
        if not svc:
            print(f"::error::{path}: an allow entry has no `service`")
            raise SystemExit(2)
        if disp not in DISPOSITIONS:
            print(
                f"::error::{path}: {svc} has disposition '{disp}', which is not one of "
                f"{sorted(DISPOSITIONS)}. An exemption has to say what will resolve it, "
                f"or it is permanent by default."
            )
            raise SystemExit(2)
        if not item.get("reason"):
            print(f"::error::{path}: {svc} has no `reason`; an unexplained exemption cannot be reviewed.")
            raise SystemExit(2)
        entries[svc] = item
    return entries


def judge(service: str, declared_rest: bool, has_spec: bool) -> tuple[str, str]:
    """Return (verdict, message). Verdict is one of ok, orphan-spec, missing-spec."""
    if declared_rest and has_spec:
        return "ok", f"{service}: declares interfaces.rest and publishes {SPEC_PATH}."
    if not declared_rest and not has_spec:
        return "ok", f"{service}: declares no REST interface and publishes no OpenAPI document."
    if not declared_rest and has_spec:
        return "orphan-spec", (
            f"{service} publishes {SPEC_PATH} but its catalog entry declares no "
            f"interfaces.rest. Because both the contract and the generator jobs gate on "
            f"has_rest, that document is validated by nothing and regenerated by nothing - "
            f"it is published, unread, and free to drift from whatever the service actually "
            f"serves. Either declare interfaces.rest in the catalog (if the service really "
            f"does serve a public HTTP API) or delete {SPEC_PATH} (if it does not; health, "
            f"metrics and webhook endpoints are operational surfaces, not an API contract)."
        )
    return "missing-spec", (
        f"{service} declares interfaces.rest in the catalog but publishes no {SPEC_PATH}. "
        f"The contract and generator jobs both skip a missing file rather than failing on "
        f"it, so this repo has no HTTP lane at all despite claiming an HTTP contract. "
        f"Either generate and commit the spec, or drop interfaces.rest from the catalog."
    )


def report(service: str, verdict: str, message: str, allow: dict) -> bool:
    """Print the finding. Return True if it should fail the build."""
    if verdict == "ok":
        print(f"  ok        {message}")
        return False

    entry = allow.get(service)
    if entry is None:
        print(f"::error::{message}")
        return True

    # Allowlisted. Still printed in full, because an exemption that prints nothing is an
    # exemption nobody revisits.
    print(
        f"::notice::{service}: {verdict}, allowlisted pending '{entry['disposition']}'. "
        f"{entry['reason']}"
    )
    return False


def catalog_entry_for(catalog_dir: Path, service: str) -> dict | None:
    path = catalog_dir / f"{service}.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def deployable_root(workspace: Path, entry: dict) -> Path | None:
    """Locate the deployable repo's working tree, or None if it is not checked out.

    Only used by the fleet-wide audit. In CI the repo under test is $PWD and no search
    happens, which is the point: a search that guesses wrong in CI would judge one service
    by another's files.
    """
    dep = (entry.get("deployable_repository") or {}).get("name")
    if not dep:
        return None
    for pattern in (f"services/*/{dep}", f"gateways/{dep}", f"services/{dep}"):
        for hit in glob.glob(str(workspace / pattern)):
            return Path(hit)
    return None


def run_single(repo: Path, service: str, declared_rest: bool, allow: dict) -> int:
    has_spec = (repo / SPEC_PATH).is_file()
    verdict, message = judge(service, declared_rest, has_spec)
    print(f"Contract classification for {service} "
          f"(declared rest={declared_rest}, {SPEC_PATH}={'present' if has_spec else 'absent'})")
    return 1 if report(service, verdict, message, allow) else 0


def run_fleet(catalog_dir: Path, workspace: Path, allow: dict) -> int:
    specs = sorted(catalog_dir.glob("*.yaml"))
    if not specs:
        print(f"::error::no catalog entries under {catalog_dir}")
        return 2

    failed = 0
    checked = 0
    skipped = []
    for path in specs:
        entry = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        service = entry.get("name") or path.stem
        root = deployable_root(workspace, entry)
        if root is None:
            skipped.append(service)
            continue
        declared_rest = bool((entry.get("interfaces") or {}).get("rest"))
        has_spec = (root / SPEC_PATH).is_file()
        verdict, message = judge(service, declared_rest, has_spec)
        checked += 1
        if report(service, verdict, message, allow):
            failed += 1

    print()
    print(f"{checked} service(s) checked, {failed} violation(s), {len(skipped)} not checked out.")
    if skipped:
        # Named, not counted. A fleet audit that silently skips half the fleet reads as a
        # pass over the whole of it.
        print(f"  not checked out: {', '.join(skipped)}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, help="repository root to check (CI mode)")
    ap.add_argument("--service", help="catalog name of the repo under test (CI mode)")
    ap.add_argument("--has-rest", choices=["true", "false"],
                    help="catalog-derived interfaces.rest, from resolve-service-identity (CI mode)")
    ap.add_argument("--catalog", type=Path, help="catalog services directory (fleet audit mode)")
    ap.add_argument("--workspace", type=Path, default=Path("."),
                    help="root holding the checked-out repos (fleet audit mode)")
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = ap.parse_args()

    allow = load_allowlist(args.allowlist)

    if args.catalog:
        return run_fleet(args.catalog, args.workspace, allow)

    # CI mode requires all three, and says which is missing. Defaulting has-rest to false
    # would turn a mis-wired workflow into a demand that every repo delete its spec.
    missing = [n for n, v in (("--repo", args.repo), ("--service", args.service),
                              ("--has-rest", args.has_rest)) if not v]
    if missing:
        print(f"::error::CI mode needs {', '.join(missing)} (or use --catalog for a fleet audit)")
        return 2

    return run_single(args.repo, args.service, args.has_rest == "true", allow)


if __name__ == "__main__":
    sys.exit(main())
