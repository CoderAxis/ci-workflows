#!/usr/bin/env python3
"""Prove the CI and CLI detectors agree, per control per repo, for both control families.

Two implementations of one rule have already cost us once: the operationId rule
was enforced by a CI gate reading a declared lock and by `ihq validate` DERIVING
bounded contexts, and the two disagreed for weeks without anyone noticing,
because nothing ever asked them the same question about the same repository.

So this asks exactly that. For every repo, for each control in each family, it
records what both implementations said and reports only the disagreements.

Two families are covered, because there are two pairs of implementations:

  rfc0038  check-api-contract.py      vs  ihq validate protocol   API-0008..API-0015
  gateway  check-gateway-baseline.py  vs  ihq validate gateway    GW-0001..GW-0008

VERDICTS ARE THREE-VALUED, not boolean. A control can be FLAGGED, CLEAN or
INDETERMINATE, and INDETERMINATE is a real verdict rather than a soft pass: it
means source could not establish an answer and review owns it. Collapsing it into
either neighbour would let one implementation say "cannot tell" while the other
says "conforms" and call that agreement, which is the exact failure this harness
exists to prevent.

COUNTS ARE COMPARED TOO, and the policy differs per family because the two
families make different promises about them:

  rfc0038  counts advisory. Its two implementations are allowed to group findings
           differently by design - one may report a route where the other reports
           an operation - so a divergence here is reported and not fatal.
  gateway  counts binding. The CLI's own gateway.go header commits to mirroring
           the Python detector and to reporting counts separately "so a
           boolean-only parity run cannot hide a counting disagreement", so a
           divergence contradicts a stated invariant and fails the run.

That distinction is not this harness inventing a two-tier standard; it is each
pair of implementations being held to what it says about itself. It also earns
its keep: API-0008 sat at CI-9 against CLI-8 for a whole session while a
verdict-only run reported agreement, and the cause was a real double-count.
"""
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTROL_DIR = HERE.parent / "controls"
# The CLI is built by the caller; its path is an argument because this script must not assume
# a checkout layout that only holds inside the monorepo.
IHQ = os.environ.get("IHQ_BIN", "ihq")

# Verdicts. Distinct from None, which means the control did not apply to the repository.
FLAGGED, CLEAN, INDETERMINATE = "flagged", "clean", "indeterminate"


@dataclass
class Family:
    """One pair of implementations of one control family."""
    ci: Path
    controls: Path
    cli_subcommand: str
    # CI control id -> CLI check name. The two naming schemes differ on purpose: a CI
    # control id is a stable policy reference and a CLI check name is a runnable filter,
    # so the mapping is declared here rather than derived from a string transformation
    # that would silently pair the wrong two the first time either side is renamed.
    pairs: dict
    strict_counts: bool
    spread: dict = field(default_factory=dict)

    def __post_init__(self):
        self.spread = {cid: [] for cid in self.pairs}


FAMILIES = {
    "rfc0038": Family(
        ci=HERE / "check-api-contract.py",
        controls=CONTROL_DIR / "api-contract.yaml",
        cli_subcommand="protocol",
        pairs={
            "API-0008": "conditional_requests",
            "API-0009": "cache_key_declared",
            "API-0010": "standard_ratelimit_fields",
            "API-0011": "patch_media_type",
            "API-0012": "idempotency_declared",
            "API-0013": "protocol_version_pinned",
            "API-0014": "trace_context_propagated",
            "API-0015": "security_response_headers",
        },
        strict_counts=False,
    ),
    "gateway": Family(
        ci=HERE / "check-gateway-baseline.py",
        controls=CONTROL_DIR / "gateway-baseline.yaml",
        cli_subcommand="gateway",
        pairs={
            "GW-0001": "gateway_shared_baseline",
            "GW-0002": "gateway_no_baseline_overrides",
            "GW-0003": "gateway_baseline_module_floor",
            "GW-0004": "gateway_protocol_ingress",
            "GW-0005": "gateway_connection_bounds",
            "GW-0006": "gateway_grpc_auth_decisions",
            "GW-0007": "grpc_server_auth_interceptors",
            "GW-0008": "gateway_backend_tls",
        },
        strict_counts=True,
    ),
}


def ci_report(fam: Family, repo: Path) -> dict:
    """control id -> (verdict, count), from the CI implementation."""
    out = subprocess.run(
        [sys.executable, str(fam.ci), str(repo), "--controls", str(fam.controls),
         "--format", "json", "--fail-on", "critical"],
        capture_output=True, text=True, timeout=300)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"__error__": (out.stdout[-400:] + out.stderr[-400:]).strip()}

    report = {}
    for c in doc.get("results", []):
        cid = c.get("control")
        if cid not in fam.pairs:
            continue
        result, count = c.get("result"), int(c.get("count") or 0)
        # A control the repo is out of scope for is not a verdict of "conforms" -- it is
        # "not asked", and the CLI must also not ask.
        if result in ("skip", "skipped", "n/a"):
            report[cid] = (None, 0)
        elif result == "indeterminate":
            report[cid] = (INDETERMINATE, count)
        else:
            report[cid] = (FLAGGED if count else CLEAN, count)
    return report


def cli_report(fam: Family, repo: Path) -> dict:
    """control id -> (verdict, count), from the CLI implementation."""
    out = subprocess.run(
        [IHQ, "validate", fam.cli_subcommand, "--repo", str(repo), "--json",
         "--details", "--severity", "info"],
        capture_output=True, text=True, timeout=300)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"__error__": (out.stdout[-400:] + out.stderr[-400:]).strip()}

    counts, indeterminate = {}, set()
    for rep in (doc.get("reports") or []):
        for f in (rep.get("findings") or []):
            check = f.get("check")
            if f.get("status") == "indeterminate":
                indeterminate.add(check)
                continue
            counts[check] = counts.get(check, 0) + 1

    report = {}
    for cid, check in fam.pairs.items():
        n = counts.get(check, 0)
        if n:
            report[cid] = (FLAGGED, n)
        elif check in indeterminate:
            # An indeterminate verdict alongside real findings is reported as FLAGGED
            # above: a control that found something has an answer, and the part it could
            # not establish does not withdraw the part it could.
            report[cid] = (INDETERMINATE, 0)
        else:
            report[cid] = (CLEAN, 0)
    return report


def run_family(name: str, fam: Family, repos, applicable) -> tuple:
    """Compare one family across repos. Returns (agree, disagree, count_gaps, unasked)."""
    agree = disagree = count_gaps = unasked = 0
    for r in repos:
        repo = Path(r)
        ci, cli = ci_report(fam, repo), cli_report(fam, repo)
        if "__error__" in ci:
            applicable.setdefault("errors", []).append(
                f"{name}/{repo.name}: CI failed: {ci['__error__'][:200]}")
            continue
        if "__error__" in cli:
            applicable.setdefault("errors", []).append(
                f"{name}/{repo.name}: CLI failed: {cli['__error__'][:200]}")
            continue
        for cid in fam.pairs:
            ci_verdict, ci_count = ci.get(cid, (None, 0))
            cli_verdict, cli_count = cli.get(cid, (None, 0))
            if ci_verdict is None:
                unasked += 1
                continue
            fam.spread[cid].append(ci_verdict)
            if ci_verdict != cli_verdict:
                disagree += 1
                print(f"{repo.name:<34} {cid:<10} {ci_verdict:<14} {cli_verdict:<14} DISAGREE")
                continue
            agree += 1
            if ci_count != cli_count:
                count_gaps += 1
                label = "COUNT DISAGREE" if fam.strict_counts else "count differs"
                print(f"{repo.name:<34} {cid:<10} {f'{ci_verdict} {ci_count}':<14} "
                      f"{f'{cli_verdict} {cli_count}':<14} {label}")
    return agree, disagree, count_gaps, unasked


def main(repos):
    shared = {}
    failed = False
    for name, fam in FAMILIES.items():
        print(f"=== {name}: {fam.ci.name} vs `ihq validate {fam.cli_subcommand}` "
              f"({'counts binding' if fam.strict_counts else 'counts advisory'})")
        print(f"{'repo':<34} {'control':<10} {'CI':<14} {'CLI':<14} verdict")
        print("-" * 88)
        agree, disagree, count_gaps, unasked = run_family(name, fam, repos, shared)
        print("-" * 88)
        print(f"agree {agree}   DISAGREE {disagree}   count gaps {count_gaps}   "
              f"out-of-scope {unasked}")
        # A parity run where every verdict is the same value proves nothing: both
        # implementations would "agree" by saying yes to everything. Report the spread
        # so the strength of the agreement is visible, not assumed.
        print()
        print(f"{'control':<10} {'flagged':<9} {'clean':<7} {'indet':<7} strength")
        for cid in fam.pairs:
            seen = fam.spread[cid]
            t = sum(1 for v in seen if v == FLAGGED)
            c = sum(1 for v in seen if v == CLEAN)
            i = sum(1 for v in seen if v == INDETERMINATE)
            if t and c:
                note = "both outcomes seen"
            elif t:
                note = "ALL FLAGGED - agreement untested on a clean repo"
            elif c:
                note = "ALL CLEAN - agreement untested on a violating repo"
            else:
                note = "NEVER DECIDED - agreement untested either way"
            print(f"{cid:<10} {t:<9} {c:<7} {i:<7} {note}")
        print()
        if disagree or (count_gaps and fam.strict_counts):
            failed = True
        # A family that decided NOTHING has not agreed, and this is the one outcome the
        # summary line above cannot express: it prints "agree 0  DISAGREE 0  count gaps 0",
        # which is character-for-character what a clean run of a narrow scope looks like.
        #
        # This exited 0 on `check-detector-parity.py gateways` - one argument naming a
        # PARENT directory rather than the repositories inside it, so a single out-of-scope
        # "repo" was compared, every control landed in NEVER DECIDED, and the harness
        # reported success. A parity harness that passes when it compared nothing is the
        # same defect class it exists to catch, and the usage text already tells the reader
        # to treat an undecided column as an untested claim rather than a passing one. That
        # instruction is now enforced rather than offered.
        if agree == 0 and disagree == 0:
            print(f"  VACUOUS: the {name} family reached no verdict on any repository, so "
                  f"this run proves nothing about agreement. {unasked} control/repo pairs "
                  f"were out of scope. Pass the repositories THEMSELVES, not a parent "
                  f"directory, and include both fixtures.")
            failed = True

    for e in shared.get("errors", []):
        print(f"  harness error: {e}")
    # A harness that cannot run one side has not proved agreement; it has proved
    # nothing, and reporting zero disagreements would be the misleading answer.
    return 1 if (failed or shared.get("errors")) else 0


def parse_roots(args: list) -> list:
    """Validate positional repository roots.

    Every argument is a repository PATH. There are no options, and that is worth enforcing
    rather than assuming: the arguments used to be consumed verbatim, so `--family gateway`
    was read as two repositories named "--family" and "gateway", and `--help` as one named
    "--help". Both produced a report rather than a usage error - the first plausible-looking
    invocation of a tool whose whole purpose is to be trusted about what it measured.

    A nonexistent path is refused for the same reason. Comparing nothing is not a clean
    result, and a typo in one of the twenty-odd roots a full run takes should be named at the
    point it is made rather than absorbed into the out-of-scope tally.
    """
    flags = [a for a in args if a.startswith("-")]
    if flags:
        raise SystemExit(
            f"error: {', '.join(flags)} looks like an option, but this harness takes only "
            f"repository paths.\nThere is no --family, --help or --json: pass the "
            f"repositories to compare, and select controls by choosing which to pass.")
    missing = [a for a in args if not Path(a).is_dir()]
    if missing:
        raise SystemExit(
            f"error: not a directory: {', '.join(missing)}\n"
            f"Each argument must be a repository root.")
    return args


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "usage: IHQ_BIN=/path/to/ihq check-detector-parity.py <repo>...\n"
            "\n"
            "Pass every repository that publishes docs/openapi.json, every gateway and BFF,\n"
            "plus BOTH fixtures in this repo's fixtures/ directory:\n"
            "\n"
            "  fixtures/rfc0038-conformant   passes all eight RFC-0038 controls\n"
            "  fixtures/rfc0038-violating    identical but for an unpinned OpenAPI dialect\n"
            "  fixtures/gateway-baseline-conformant  passes the gateway controls\n"
            "  fixtures/gateway-baseline-violating   violates them\n"
            "\n"
            "The fixtures are not optional padding. A run in which every verdict is the same\n"
            "value proves nothing - both implementations would 'agree' by saying yes to\n"
            "everything - and the real fleet does not supply both outcomes for every control:\n"
            "API-0015 was flagged on all 37 repos and API-0013 on none, so those two columns\n"
            "were untested until the fixtures supplied the missing side. The spread table at\n"
            "the end reports this per control; treat any column that is not 'both outcomes\n"
            "seen' as an untested claim rather than a passing one.")
    sys.exit(main(parse_roots(args)))
