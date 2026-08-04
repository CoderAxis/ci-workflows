#!/usr/bin/env python3
"""Prove the CI and CLI RFC-0038 detectors agree, per control per repo.

Two implementations of one rule have already cost us once: the operationId rule
was enforced by a CI gate reading a declared lock and by `ihq validate` DERIVING
bounded contexts, and the two disagreed for weeks without anyone noticing,
because nothing ever asked them the same question about the same repository.

So this asks exactly that. For every repo, for each of the eight RFC-0038
controls, it records whether each implementation flagged it, and reports only the
disagreements. It deliberately compares the BOOLEAN verdict rather than finding
counts or messages: the two are allowed to phrase and group findings differently
(one may report a route, the other an operation), but they must not disagree on
whether a repository conforms, because that verdict is what gates a deploy.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CI = HERE / "check-api-contract.py"
CONTROLS = HERE.parent / "controls" / "api-contract.yaml"
# The CLI is built by the caller; its path is an argument because this script must not assume
# a checkout layout that only holds inside the monorepo.
IHQ = os.environ.get("IHQ_BIN", "ihq")

# The eight RFC-0038 controls, CI control id -> CLI check name.
PAIRS = {
    "API-0008": "conditional_requests",
    "API-0009": "cache_key_declared",
    "API-0010": "standard_ratelimit_fields",
    "API-0011": "patch_media_type",
    "API-0012": "idempotency_declared",
    "API-0013": "protocol_version_pinned",
    "API-0014": "trace_context_propagated",
    "API-0015": "security_response_headers",
}


def ci_verdicts(repo: Path) -> dict:
    """control id -> flagged?, from the CI implementation."""
    out = subprocess.run(
        [sys.executable, str(CI), str(repo), "--controls", str(CONTROLS),
         "--format", "json", "--fail-on", "critical"],
        capture_output=True, text=True, timeout=300)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"__error__": (out.stdout[-400:] + out.stderr[-400:]).strip()}

    verdicts = {}
    for c in doc.get("results", []):
        cid = c.get("control")
        if cid in PAIRS:
            # A control the repo is out of scope for is not a verdict of
            # "conforms" -- it is "not asked", and the CLI must also not ask.
            if c.get("result") in ("skip", "skipped", "n/a"):
                verdicts[cid] = None
            else:
                verdicts[cid] = bool(c.get("count") or 0)
    return verdicts


def cli_verdicts(repo: Path) -> dict:
    """control id -> flagged?, from the CLI implementation."""
    out = subprocess.run(
        [IHQ, "validate", "protocol", "--repo", str(repo), "--json",
         "--details", "--severity", "info"],
        capture_output=True, text=True, timeout=300)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"__error__": (out.stdout[-400:] + out.stderr[-400:]).strip()}

    by_check = {}
    for rep in (doc.get("reports") or []):
        for f in (rep.get("findings") or []):
            by_check[f.get("check")] = by_check.get(f.get("check"), 0) + 1
    return {cid: bool(by_check.get(cli)) for cid, cli in PAIRS.items()}


def main(repos):
    print(f"{'repo':<34} {'control':<10} {'CI':<8} {'CLI':<8} verdict")
    print("-" * 74)
    agree = disagree = unasked = 0
    errors = []
    spread = {cid: [] for cid in PAIRS}
    for r in repos:
        repo = Path(r)
        ci, cli = ci_verdicts(repo), cli_verdicts(repo)
        if "__error__" in ci:
            errors.append(f"{repo.name}: CI failed: {ci['__error__'][:200]}")
            continue
        if "__error__" in cli:
            errors.append(f"{repo.name}: CLI failed: {cli['__error__'][:200]}")
            continue
        for cid in PAIRS:
            a, b = ci.get(cid), cli.get(cid)
            if a is None:
                unasked += 1
                continue
            spread[cid].append(a)
            if a == b:
                agree += 1
            else:
                disagree += 1
                print(f"{repo.name:<34} {cid:<10} {str(a):<8} {str(b):<8} DISAGREE")
    print("-" * 74)
    print(f"agree {agree}   DISAGREE {disagree}   out-of-scope {unasked}")
    # A parity run where every verdict is the same value proves nothing: both
    # implementations would "agree" by saying yes to everything. Report the spread
    # so the strength of the agreement is visible, not assumed.
    print()
    print(f"{'control':<10} {'flagged':<9} {'clean':<7} strength")
    for cid in PAIRS:
        t = sum(1 for v in spread[cid] if v is True)
        f = sum(1 for v in spread[cid] if v is False)
        note = "both outcomes seen" if t and f else ("ALL FLAGGED - agreement untested on a clean repo" if t else "ALL CLEAN - agreement untested on a violating repo")
        print(f"{cid:<10} {t:<9} {f:<7} {note}")
    for e in errors:
        print(f"  harness error: {e}")
    return 1 if disagree else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "usage: IHQ_BIN=/path/to/ihq check-detector-parity.py <repo>...\n"
            "\n"
            "Pass every repository that publishes docs/openapi.json, plus at least one\n"
            "fixture that CONFORMS. A run in which every verdict is the same value proves\n"
            "nothing, and the spread table at the end is what shows whether it did.")
    sys.exit(main(args))
