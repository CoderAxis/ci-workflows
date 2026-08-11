#!/usr/bin/env python3
"""Tell a job GitHub refused to start apart from a gate that ran and failed.

WHY THIS EXISTS
---------------
On 2026-08-11 the organisation's Actions budget was reached with
`prevent_further_usage: true`. Between 07:33:15Z and 08:01:11Z GitHub refused to dispatch 121
jobs across 24 repositories. Eight port-drift pull requests opened inside that window each
reported the SAME failing step - `Resolve service identity` - and each passed on rerun once the
budget was raised. Eight out of eight looks like a systematic defect in the resolver, and it was
read as one.

It was not. `Resolve service identity` is the root of service-ci.yaml's job graph, so on a
freshly triggered run it is the only job that even attempts to start; every other job is then
marked `skipped` because its dependency "failed". Whichever job tries to start inside the window
is the one that gets refused, and nothing about it is specific to identity resolution. The proof
is in the same incident: run 31469306219 in inboxxhq-voice-ai-agent-service resolved identity
successfully at 07:33:04Z (4 steps, `conclusion: success`) and then had `Build and test (go)` and
`Integration tests` refused eleven seconds later.

A refusal is distinguishable from a verdict, but only if you look at the right field. A refused
job has:

  * `conclusion: failure`, exactly like a real one;
  * an EMPTY `steps` array, because no runner ever claimed it;
  * no log blob at all - `/logs` returns 404 BlobNotFound, not an empty log;
  * one check-run annotation attached to `.github` rather than to any source file.

The empty step array is the discriminator this script keys on. It is written by the runner, so it
cannot be non-empty unless the runner ran, which makes it the one signal a misleading annotation
cannot forge.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not retry anything. A refusal happens before a runner claims the job, so there is no step
in which a retry could execute: `continue-on-error`, step-level retry and in-workflow loops are
all unreachable. Nothing that can be added to service-ci.yaml prevents this class of failure, and
`Notify on failure` is refused by the same budget, so not even the Slack alert survives. The
remediation is the budget. This script exists to stop the NEXT such window being spent looking
for a race in the resolver.

Nor does it ever excuse a job that ran. RT-0003 asserts the inverse case: a failed job with a
non-empty step array is a gate verdict whatever its annotation says. Without that, a triage tool
becomes a way to relabel real violations as billing notes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

import yaml

DEFAULT_CONTROLS = pathlib.Path(__file__).resolve().parent.parent / "controls" / "run-triage.yaml"

REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "policy", "rationale",
                   "remediation", "detector")

# Exit codes are the interface. 0 means "believe the red X"; EX_REFUSED means "this run carries no
# verdict about the commit". A distinct code rather than a flag because the caller's next action
# differs completely: fix the code, versus fix the budget and rerun.
EX_REFUSED = 75  # EX_TEMPFAIL: the condition is external and expected to clear.
EX_CATALOG = 2

# Matched against the annotation GitHub attaches to a job it would not dispatch. Kept as patterns
# rather than exact strings because the wording is GitHub's and has changed before; anchored on the
# nouns that carry the meaning so a rewording does not silently stop matching.
#
# Each pattern also carries the REFUSAL FRAMING and not just the billing noun. A bare /billing/ or
# /payment/ matched this fleet's own output - inboxxhq-billing-service and inboxxhq-payment-service
# emit both words in ordinary test failures - so a looser pattern would have named exactly the
# repositories whose failures must never be excused. The step-array guard already prevents that on
# its own; requiring both is what stops the guard from being the only thing standing between a
# reworded annotation and a laundered violation.
BILLING_PATTERNS = (
    r"recent account payments have failed",
    r"spending limit needs to be increased",
    r"(?:job|run) was not started because.{0,120}(?:payment|spending limit|billing)",
)

RUNNER_PATTERNS = (
    r"no (?:self-hosted )?runner (?:matching|is available)",
    r"requested labels? .* (?:is|are) not (?:available|valid)",
    r"concurrency limit",
    r"exceeded the maximum (?:number of )?concurrent jobs",
)


class Verdict:
    """One failed job's classification, with the evidence that produced it."""

    def __init__(self, job: dict, control_id: str, kind: str, evidence: str) -> None:
        self.job = job
        self.control_id = control_id
        self.kind = kind          # "refused" | "genuine"
        self.evidence = evidence

    @property
    def name(self) -> str:
        return str(self.job.get("name", "<unnamed>"))


def _ran_no_steps(job: dict) -> bool:
    return not (job.get("steps") or [])


def _annotation_text(job: dict, annotations: dict) -> str:
    entries = annotations.get(str(job.get("id"))) or []
    return "\n".join(str(a.get("message") or "") for a in entries)


def _matches(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(0)
    return None


def refused_for_billing(job: dict, annotations: dict) -> tuple[bool, str]:
    """RT-0001. Empty step array AND GitHub's billing wording."""
    if not _ran_no_steps(job):
        return False, ""
    hit = _matches(_annotation_text(job, annotations), BILLING_PATTERNS)
    if not hit:
        return False, ""
    return True, (f"ran 0 steps and is annotated {hit!r}; the account budget refused to dispatch "
                  "it, so it reports nothing about this commit")


def refused_for_runner(job: dict, annotations: dict) -> tuple[bool, str]:
    """RT-0002. Empty step array AND a runner-acquisition failure."""
    if not _ran_no_steps(job):
        return False, ""
    hit = _matches(_annotation_text(job, annotations), RUNNER_PATTERNS)
    if not hit:
        return False, ""
    return True, (f"ran 0 steps and is annotated {hit!r}; no runner was obtained, so the job "
                  "reports nothing about this commit")


def genuine_gate_failure(job: dict, annotations: dict) -> tuple[bool, str]:
    """RT-0003. The runner ran it, so the failure is the gate's verdict.

    Deliberately independent of the annotation text. This is the control that stops RT-0001 and
    RT-0002 from being usable to launder a real violation.
    """
    steps = job.get("steps") or []
    if not steps:
        return False, ""
    failed = [s.get("name") for s in steps if s.get("conclusion") == "failure"]
    where = f"; failing step(s): {', '.join(str(f) for f in failed)}" if failed else ""
    return True, f"ran {len(steps)} step(s), so this is the gate's verdict{where}"


# Keyed by the catalog's `detector` NAME rather than by control id, matching
# check-gateway-baseline.py: the catalog's detector key is what binds a control to its
# implementation, so binding by id would let a control be renamed without anything noticing that
# the implementation stayed behind.
DETECTORS = {
    "refused_for_billing": refused_for_billing,
    "refused_for_runner": refused_for_runner,
    "genuine_gate_failure": genuine_gate_failure,
}

REFUSAL_DETECTORS = ("refused_for_billing", "refused_for_runner")


def load_controls(path: pathlib.Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"::error::cannot read control catalog {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: expected a controls list")
    errors, seen = [], set()
    for control in doc["controls"]:
        cid = control.get("id", "<missing>")
        missing = [key for key in REQUIRED_FIELDS if not control.get(key)]
        if missing:
            errors.append(f"{cid}: missing {missing}")
        if control.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {control.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate id")
        seen.add(cid)
    if errors:
        for error in errors:
            print(f"::error::run-triage catalog invalid: {error}")
        raise SystemExit(EX_CATALOG)
    return doc


def fetch_bundle(repo: str, run_id: str, attempt: str) -> dict:
    """Collect the same shape the fixtures hold, live, via gh.

    Jobs come from the attempt endpoint because a rerun REPLACES the job list on the run
    endpoint - which is why the first attempt of each of the eight pull requests could not be
    read from the run itself once it had been rerun green.
    """
    def gh(path: str) -> object:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"::error::gh api {path} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)

    jobs = gh(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else []
    annotations = {}
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        # A refused job has no log blob, so the annotation is the only text that exists for it.
        proc = subprocess.run(["gh", "api", f"repos/{repo}/check-runs/{job['id']}/annotations"],
                              capture_output=True, text=True)
        annotations[str(job["id"])] = json.loads(proc.stdout) if proc.returncode == 0 else []
    return {"jobs": jobs, "annotations": annotations}


def classify(bundle: dict, doc: dict) -> list[Verdict]:
    jobs = bundle.get("jobs") or []
    annotations = bundle.get("annotations") or {}
    active = [c for c in doc["controls"] if c.get("status") == "active"]

    verdicts: list[Verdict] = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        for control in active:
            fired, evidence = DETECTORS[control["detector"]](job, annotations)
            if fired:
                kind = "refused" if control["detector"] in REFUSAL_DETECTORS else "genuine"
                verdicts.append(Verdict(job, control["id"], kind, evidence))
                break
        else:
            # Fail open towards "believe the red X". An unrecognised refusal shape must not be
            # excused by default, or the first reworded annotation silently becomes a pass.
            verdicts.append(Verdict(
                job, "RT-0003", "genuine",
                "ran 0 steps but carries no annotation this catalog recognises; treated as a "
                "genuine failure, because excusing an unrecognised shape is how a real violation "
                "would slip through"))
    return verdicts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", help="JSON file holding {jobs, annotations}")
    source.add_argument("--repo", help="OWNER/REPO to read live via gh")
    parser.add_argument("--run", help="workflow run id (with --repo)")
    parser.add_argument("--attempt", default="1", help="run attempt to read (default: 1)")
    parser.add_argument("--controls", default=str(DEFAULT_CONTROLS))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    doc = load_controls(pathlib.Path(args.controls))

    if args.repo:
        if not args.run:
            raise SystemExit("::error::--repo requires --run")
        bundle = fetch_bundle(args.repo, args.run, args.attempt)
    else:
        try:
            bundle = json.loads(pathlib.Path(args.bundle).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"::error::cannot read bundle {args.bundle}: {exc}")

    verdicts = classify(bundle, doc)
    refused = [v for v in verdicts if v.kind == "refused"]
    genuine = [v for v in verdicts if v.kind == "genuine"]

    if args.format == "json":
        print(json.dumps({
            "refused": [{"control": v.control_id, "job": v.name, "evidence": v.evidence}
                        for v in refused],
            "genuine": [{"control": v.control_id, "job": v.name, "evidence": v.evidence}
                        for v in genuine],
        }, indent=2))
    else:
        if not verdicts:
            print("[ok] no failed jobs in this attempt")
        for verdict in genuine:
            print(f"::error::[{verdict.control_id}] {verdict.name}: {verdict.evidence}")
        for verdict in refused:
            print(f"[refused] {verdict.control_id} {verdict.name}: {verdict.evidence}")
        if refused and not genuine:
            print(f"::notice::{len(refused)} job(s) were refused before running and no gate "
                  "returned a verdict. This run says nothing about the commit: it is not a code "
                  "failure and there is no workflow change that would have prevented it. Fix the "
                  "account budget or runner availability, then rerun.")
        elif refused and genuine:
            print(f"::warning::mixed attempt: {len(refused)} job(s) refused before running and "
                  f"{len(genuine)} genuine gate failure(s). The genuine failures are real and "
                  "must be fixed; rerunning will not clear them.")

    # A genuine failure anywhere in the attempt outranks a refusal: there is something to fix, so
    # the caller must not be told the run carried no verdict.
    if genuine:
        return 0
    return EX_REFUSED if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
