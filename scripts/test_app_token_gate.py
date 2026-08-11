#!/usr/bin/env python3
"""Self-test for the `admits-central` event gate in app-token-selftest.yaml.

    python3 scripts/test_app_token_gate.py

WHAT THIS GUARDS. The broker self-check's positive job asserts that AWS mints a read-scoped
credential for a job defined in a central workflow. That assertion is only reachable from a run
whose OIDC `sub` inboxxhq-infra's `ci_caller_subs` admits - `repo:<owner>/*:ref:refs/heads/main`,
`repo:<owner>/*:pull_request` and `repo:<owner>/*:ref:refs/tags/*` - so the job carries an `if:`
gate that skips it on any other caller shape. A push to a feature branch mints
`repo:CoderAxis/ci-workflows:ref:refs/heads/<branch>`, which matches none of the three, and the
job spent that whole shape reporting a red X for a broker that was working exactly as scoped.

The gate is therefore load-bearing in both directions and silent in both. Narrow it - drop the
`pull_request` arm, or write `github.ref == 'refs/tags/'` where a prefix test was meant - and the
positive assertion stops running on the shapes it exists to cover, while CI stays green because a
skipped job is not a failed one. Widen it, or delete it, and the feature-branch red X comes back
and gets dismissed as pre-existing, which is what happened three times before anyone traced it to
the trust policy.

WHY THE EXPRESSION IS EXTRACTED AND NOT COPIED. This test parses the workflow and evaluates the
literal string it finds in `jobs.admits-central.if`. A hand-written copy of the expression would
assert that the copy behaves as intended and nothing at all about the file GitHub reads, so the
two would drift apart the first time somebody edited one of them - and the copy would keep
passing, which is the worst available outcome for a guard whose whole subject is a silent skip.

WHY IT IS EVALUATED AND NOT PATTERN-MATCHED. Asserting that the text contains `refs/tags/` would
pass against `github.ref == 'refs/tags/'`, against a gate whose arms had been joined with `&&`,
and against one where the whole thing sat behind a `!`. Only evaluation distinguishes those from
the gate that is meant to be there, so this file carries a small evaluator for the subset of the
GitHub Actions expression language the gate actually uses.

WHAT THIS DOES NOT PROVE, WHICH MATTERS AS MUCH AS WHAT IT DOES. It proves the gate admits and
skips the event shapes named below. It cannot prove those shapes are the ones AWS still admits:
`ci_caller_subs` lives in inboxxhq-infra (environments/management/ci-identity/ci-app-token.tf),
this repository neither owns nor reads it, and nothing here would notice it being widened or
narrowed. If that policy changes, this test keeps passing while the gate silently drifts out of
step with it - skipping a shape that would now be admitted, or running the assertion on one that
would now be denied. Keeping the two aligned is a human obligation on any change to that file;
CID-0001..0004 in the ci-identity catalog police the shape of those `sub` patterns, not their
agreement with this gate.

Exit codes:
    0  the gate evaluates as intended on every case below
    1  the gate ran and gave a wrong answer, or the negative job grew a gate it must not have
    2  the test could not evaluate the gate at all - file, job or `if:` key missing, or the
       expression uses syntax this evaluator does not model. Distinct from 1 because the two call
       for opposite responses: fix the workflow, versus extend this file.
"""
from __future__ import annotations

import pathlib
import string
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
WORKFLOW = HERE.parent / ".github" / "workflows" / "app-token-selftest.yaml"

# Both job keys are asserted by name. Renaming either one without updating this file must fail
# rather than quietly reduce the suite to nothing: a test that cannot find its subject and reports
# success is the same class of defect as the silent skip it is written to catch.
GATED_JOB = "admits-central"
UNGATED_JOB = "refuses-unlisted-workflow"

FAILURES: list[str] = []


class Unevaluatable(Exception):
    """The gate uses a construct this evaluator does not model.

    Raised, never swallowed. An evaluator that treated an unrecognised construct as false would
    report the gate skipping every event shape, and one that treated it as true would report the
    opposite; both are a confident answer about an expression nothing understood. The only honest
    outcome is to stop and say the evaluator has to be extended.
    """


# ── the expression subset ─────────────────────────────────────────────────────────────────────
# Deliberately only what the gate uses today: `github.event_name`, `github.ref`, `==`, `||`,
# `startsWith(a, b)`, single-quoted literals and parentheses. `&&`, `!`, `!=`, the ordering
# operators, `contains`/`endsWith` and every other context are absent on purpose - an untested
# implementation of them would be indistinguishable from a correct one right up to the run that
# depended on it, whereas their absence surfaces as a loud, actionable failure the moment the gate
# starts using one.

SUPPORTED_CONTEXTS = ("github.event_name", "github.ref")
SUPPORTED_FUNCTIONS = ("startsWith",)

_NAME_START = frozenset(string.ascii_letters + "_")
_NAME_BODY = frozenset(string.ascii_letters + string.digits + "_-.")


def _tokenize(expression: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    i, n = 0, len(expression)
    while i < n:
        char = expression[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char in "(),":
            tokens.append(("punct", char, i))
            i += 1
            continue
        if expression.startswith("==", i):
            tokens.append(("op", "==", i))
            i += 2
            continue
        if expression.startswith("||", i):
            tokens.append(("op", "||", i))
            i += 2
            continue
        if char == "'":
            # GitHub escapes a quote inside a literal by doubling it.
            j, parts = i + 1, []
            while True:
                if j >= n:
                    raise Unevaluatable(
                        f"unterminated string literal starting at offset {i}")
                if expression[j] == "'":
                    if expression.startswith("''", j):
                        parts.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                parts.append(expression[j])
                j += 1
            tokens.append(("string", "".join(parts), i))
            i = j
            continue
        if char in _NAME_START:
            j = i
            while j < n and expression[j] in _NAME_BODY:
                j += 1
            tokens.append(("name", expression[i:j], i))
            i = j
            continue
        raise Unevaluatable(
            f"unsupported character {char!r} at offset {i}. This evaluator models only "
            f"{' '.join(SUPPORTED_CONTEXTS)}, ==, ||, startsWith(), quoted literals and "
            "parentheses; extend it to cover whatever the gate now uses")
    return tokens


class _Parser:
    """Builds an AST for the WHOLE expression before anything is evaluated.

    Evaluating while parsing would be shorter and wrong. `||` short-circuits, so on a
    `pull_request` context the first arm is true and the rest of the expression is never looked
    at - meaning a gate whose tail had been rewritten into syntax this file cannot evaluate would
    sail through the case that happens to match first and fail only on some later case, or on
    none. Parsing everything up front makes the loud failure independent of which context is being
    tested.
    """

    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self._tokens = tokens
        self._i = 0

    def _peek(self) -> tuple[str, str, int] | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _take(self) -> tuple[str, str, int]:
        token = self._peek()
        if token is None:
            raise Unevaluatable("expression ends where a term was expected")
        self._i += 1
        return token

    def _expect(self, kind: str, text: str) -> None:
        token = self._take()
        if token[0] != kind or token[1] != text:
            raise Unevaluatable(f"expected {text!r}, found {token[1]!r} at offset {token[2]}")

    def parse(self) -> tuple:
        node = self._or()
        trailing = self._peek()
        if trailing is not None:
            raise Unevaluatable(
                f"unconsumed {trailing[1]!r} at offset {trailing[2]}: the expression does not "
                "parse as a whole, so no part of this evaluation would be trustworthy")
        return node

    def _or(self) -> tuple:
        operands = [self._equality()]
        while (token := self._peek()) is not None and token[1] == "||":
            self._take()
            operands.append(self._equality())
        return operands[0] if len(operands) == 1 else ("or", operands)

    def _equality(self) -> tuple:
        left = self._primary()
        if (token := self._peek()) is not None and token[1] == "==":
            self._take()
            return ("eq", left, self._primary())
        return left

    def _primary(self) -> tuple:
        kind, text, pos = self._take()
        if kind == "punct" and text == "(":
            node = self._or()
            self._expect("punct", ")")
            return node
        if kind == "string":
            return ("str", text)
        if kind == "name":
            following = self._peek()
            if following is not None and following[1] == "(":
                return self._call(text, pos)
            if text not in SUPPORTED_CONTEXTS:
                raise Unevaluatable(
                    f"unsupported reference {text!r} at offset {pos}. Only "
                    f"{' and '.join(SUPPORTED_CONTEXTS)} can be given a value here, so a gate "
                    "reading anything else cannot be judged without extending this evaluator "
                    "and giving the new context a value in every case below")
            return ("ref", text)
        raise Unevaluatable(f"unexpected {text!r} at offset {pos}")

    def _call(self, name: str, pos: int) -> tuple:
        if name not in SUPPORTED_FUNCTIONS:
            raise Unevaluatable(
                f"unsupported function {name}() at offset {pos}; this evaluator implements only "
                f"{', '.join(f'{f}()' for f in SUPPORTED_FUNCTIONS)}")
        self._expect("punct", "(")
        args = [self._or()]
        while (token := self._peek()) is not None and token[1] == ",":
            self._take()
            args.append(self._or())
        self._expect("punct", ")")
        if len(args) != 2:
            raise Unevaluatable(f"{name}() takes 2 arguments, found {len(args)}")
        return ("startswith", args[0], args[1])


def _type_of(node: tuple) -> str:
    """Types the whole tree up front, for the same reason the parser does not evaluate.

    GitHub's operators are far looser than this - `||` yields the first truthy operand rather than
    a boolean, and `==` coerces across types - and modelling that faithfully is a much larger job
    than the gate justifies. Rejecting the mixed forms outright keeps the difference between this
    evaluator and GitHub's from ever becoming a wrong answer: anything it does not type is
    reported as unevaluatable instead.
    """
    kind = node[0]
    if kind in ("str", "ref"):
        return "string"
    if kind in ("eq", "startswith"):
        for operand in node[1:]:
            if _type_of(operand) != "string":
                raise Unevaluatable(
                    f"{kind} compares a non-string operand; GitHub coerces here and this "
                    "evaluator deliberately does not")
        return "boolean"
    if kind == "or":
        for operand in node[1]:
            if _type_of(operand) != "boolean":
                raise Unevaluatable(
                    "|| is applied to a non-boolean operand; in GitHub that yields a value "
                    "rather than a condition, which this evaluator does not model")
        return "boolean"
    raise Unevaluatable(f"unsupported node {kind!r}")


def _strip_wrapper(raw: str) -> str:
    """`if: <expr>` and `if: ${{ <expr> }}` are the same gate, so neither spelling may fail here.

    Rejecting the braced form would make a purely cosmetic edit turn this red, and a guard that
    cries wolf is one somebody switches off. Anything else containing `${{` is refused rather than
    guessed at: an interpolation in the middle of the expression is a construct with no meaning to
    this evaluator.
    """
    text = raw.strip()
    if text.startswith("${{"):
        if not text.endswith("}}"):
            raise Unevaluatable("the gate opens with '${{' but does not close with '}}'")
        text = text[3:-2]
    if "${{" in text or "}}" in text:
        raise Unevaluatable(
            "the gate embeds a '${{ }}' interpolation inside the expression, which this "
            "evaluator does not model")
    return text.strip()


def compile_condition(raw: str) -> tuple:
    node = _Parser(_tokenize(_strip_wrapper(raw))).parse()
    if _type_of(node) != "boolean":
        raise Unevaluatable(
            "the gate does not evaluate to a condition. GitHub applies its own truthiness rules "
            "to whatever it produces, which this evaluator does not model")
    return node


def evaluate(node: tuple, context: dict[str, str]) -> bool | str:
    kind = node[0]
    if kind == "str":
        return node[1]
    if kind == "ref":
        return context[node[1]]
    # Case-insensitive, because GitHub's string comparison and startsWith() both ignore casing.
    # It makes no difference to any ref below, but a model that silently differs from the runtime
    # in a corner is not worth having.
    if kind == "eq":
        return str(evaluate(node[1], context)).lower() == str(evaluate(node[2], context)).lower()
    if kind == "startswith":
        return str(evaluate(node[1], context)).lower().startswith(
            str(evaluate(node[2], context)).lower())
    if kind == "or":
        # Every operand is evaluated. Short-circuiting is invisible here (all operands are pure)
        # and hiding half the tree from the run is exactly what this file exists to prevent.
        return any([evaluate(operand, context) for operand in node[1]])
    raise Unevaluatable(f"unsupported node {kind!r}")


def gate_admits(node: tuple, event_name: str, ref: str) -> bool:
    result = evaluate(node, {"github.event_name": event_name, "github.ref": ref})
    if not isinstance(result, bool):
        raise Unevaluatable(f"the gate produced {result!r}, which is not a condition")
    return result


# ── the cases the gate has to get right ───────────────────────────────────────────────────────
# Expected values are not a restatement of the expression; each is the answer to "does
# ci_caller_subs admit the `sub` this event shape mints", which is what the gate is a proxy for.
# Read them against the three admitted patterns quoted in the module docstring.
#
# (event_name, github.ref, gate must be, why this case is here)
CASES: list[tuple[str, str, bool, str]] = [
    ("pull_request", "refs/pull/42/merge", True,
     "a pull request mints `repo:<owner>/<repo>:pull_request`, which is admitted whatever the "
     "branch is called - the arm is on the event, not the ref, and a gate that tested the ref "
     "here would skip every pull request while looking correct"),
    ("pull_request", "refs/heads/some-feature-branch", True,
     "the same event off a feature branch is still admitted. This is the case that fails if the "
     "arms are ever joined with && instead of ||, which would leave the guard asserting only on "
     "main and never say so"),
    ("push", "refs/heads/main", True,
     "a merge to main mints `ref:refs/heads/main`, the first admitted pattern; this is the shape "
     "that proves the broker still works after a pull request has landed"),
    ("push", "refs/tags/v1", True,
     "the major tag this repository's consumers pin. `refs/tags/*` is admitted, and the release "
     "that moves v1 is precisely when the broker must be known to work"),
    ("push", "refs/tags/v1.2.3", True,
     "a point release, so the tag arm is a prefix test rather than an equality against v1"),
    ("push", "refs/heads/some-feature-branch", False,
     "THE CASE THE GATE WAS ADDED FOR. This mints `ref:refs/heads/<branch>`, which ci_caller_subs "
     "deliberately never admits, so the job must skip rather than report a red X for an "
     "AccessDenied that is the policy working as designed"),
    ("workflow_dispatch", "refs/heads/some-feature-branch", False,
     "a manual run off a feature branch mints the same unadmitted `sub` as a push to it; the gate "
     "must not be event-only, or every hand-triggered run on a branch fails again"),
    ("workflow_dispatch", "refs/heads/main", True,
     "and the same trigger on main IS admitted - the branch arm keys off the ref, so restricting "
     "it to `push` would skip a run that would have asserted for real"),
    ("schedule", "refs/heads/main", True,
     "ci.yaml runs on a cron, which GitHub always dispatches against the default branch; that "
     "shape is admitted and must not be skipped"),
    ("push", "refs/heads/refs/tags/x", False,
     "STARTSWITH, NOT SUBSTRING. A branch whose name contains the tag prefix mints a "
     "`ref:refs/heads/...` sub that is not admitted. A gate written with contains() - or a test "
     "that grepped the expression instead of running it - passes this wrongly and hands the "
     "broker's positive assertion to anyone who can name a branch"),
    ("push", "refs/heads/main-backport", False,
     "the main arm is an equality, not a prefix. `refs/heads/main-backport` mints its own "
     "unadmitted sub, so a startsWith() written there by symmetry with the tag arm would run the "
     "assertion on a shape AWS refuses"),
    ("push", "refs/tags/v2.0.0-rc.1", True,
     "`refs/tags/*` admits any tag, so the arm must stay a bare prefix test; narrowing it to a "
     "shape like `v<major>` would skip a release the policy would have admitted"),
]

# ── evaluator self-checks ─────────────────────────────────────────────────────────────────────
# HAND-WRITTEN ON PURPOSE, unlike everything above: these judge the evaluator, not the gate. If it
# has a bug - an || that behaves like &&, a startsWith() matching anywhere in the string - then
# every assertion above is a confident statement about nothing, and the gate cases alone cannot
# tell, because the same bug produces the same verdict on both sides.
EVALUATOR_CASES: list[tuple[str, str, str, bool]] = [
    ("github.event_name == 'push'", "push", "refs/heads/main", True),
    ("github.event_name == 'push'", "pull_request", "refs/heads/main", False),
    ("startsWith(github.ref, 'refs/tags/')", "push", "refs/tags/v1", True),
    ("startsWith(github.ref, 'refs/tags/')", "push", "refs/heads/refs/tags/v1", False),
    ("github.event_name == 'push' || github.ref == 'refs/heads/main'", "push", "refs/pull/1/merge",
     True),
    ("github.event_name == 'push' || github.ref == 'refs/heads/main'", "schedule",
     "refs/heads/main", True),
    ("github.event_name == 'push' || github.ref == 'refs/heads/main'", "schedule",
     "refs/heads/topic", False),
    ("(github.event_name == 'push' || github.event_name == 'schedule')", "schedule",
     "refs/heads/topic", True),
    ("${{ github.event_name == 'push' }}", "push", "refs/heads/main", True),
]

# Constructs that MUST be refused. Without these the fail-loud path is unexecuted code, and an
# evaluator that had quietly started treating an unknown construct as false would report the gate
# skipping everything - which reads as "the gate is very strict" rather than as a broken test.
MUST_REFUSE: list[tuple[str, str]] = [
    ("github.event_name == 'push' && github.ref == 'refs/heads/main'",
     "&& is not modelled"),
    ("!startsWith(github.ref, 'refs/tags/')", "negation is not modelled"),
    ("github.event_name != 'push'", "!= is not modelled"),
    ("contains(github.ref, 'refs/tags/')", "contains() is not modelled"),
    ("github.base_ref == 'main'", "an unmodelled context has no value in the cases above"),
    ("github.event_name == true", "`true` is not a modelled reference"),
    ("github.event_name == 'push'", None),  # control: the supported form must NOT be refused
    ("(github.event_name == 'push'", "an unbalanced parenthesis is not a parseable gate"),
    ("github.ref", "a bare string is not a condition"),
    ("github.event_name == 'push' github.ref == 'x'", "a trailing term is not parseable"),
]


def load_gate() -> tuple[dict, str]:
    """Reads the real workflow. Every way this can fail is fatal, none of them vacuous."""
    if not WORKFLOW.is_file():
        raise SystemExit(die(f"{WORKFLOW} does not exist. The gate this file tests cannot be "
                             "found, so a pass here would certify nothing."))
    try:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(die(f"{WORKFLOW} is not parseable YAML, so the gate cannot be "
                             f"read: {exc}"))
    jobs = (document or {}).get("jobs")
    if not isinstance(jobs, dict):
        raise SystemExit(die(f"{WORKFLOW} has no `jobs:` mapping"))
    if GATED_JOB not in jobs:
        raise SystemExit(die(
            f"{WORKFLOW} has no `{GATED_JOB}` job (it has: {', '.join(sorted(jobs))}). Either the "
            "broker's positive assertion was removed, or it was renamed and this test silently "
            "stopped having a subject."))
    condition = jobs[GATED_JOB].get("if")
    if condition is None:
        raise SystemExit(die(
            f"`{GATED_JOB}` has no `if:` gate. Without it the job runs on every caller shape, "
            "including a push to a feature branch, whose OIDC `sub` ci_caller_subs deliberately "
            "never admits - so the broker self-check reports AccessDenied as a failure of the "
            "broker rather than as the trust policy holding. See the comment above the gate."))
    if not isinstance(condition, str):
        raise SystemExit(die(
            f"`{GATED_JOB}`'s `if:` is {condition!r}, not an expression string"))
    return jobs, condition


def die(message: str) -> int:
    print(f"app-token gate: CANNOT EVALUATE\n  ::error::{message}")
    return 2


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def main() -> int:
    for expression, event_name, ref, want in EVALUATOR_CASES:
        try:
            got = gate_admits(compile_condition(expression), event_name, ref)
        except Unevaluatable as exc:
            return die(f"the evaluator cannot handle its own test case {expression!r}: {exc}")
        expect(got is want,
               f"EVALUATOR BUG: {expression!r} with event_name={event_name!r} ref={ref!r} gave "
               f"{got}, want {want}. Every gate assertion below is meaningless until this is "
               "fixed, because the same bug would be applied to the real expression")

    for expression, why in MUST_REFUSE:
        try:
            compile_condition(expression)
        except Unevaluatable:
            expect(why is not None,
                   f"EVALUATOR BUG: {expression!r} was refused, but it is the supported form; "
                   "the gate itself would now be unevaluatable")
            continue
        expect(why is None,
               f"EVALUATOR BUG: {expression!r} was accepted although {why}. A construct nobody "
               "implemented must fail loudly, never be judged - a wrong answer about the gate is "
               "worse than no answer, because it is believed")

    if FAILURES:
        print(f"app-token gate: FAILED ({len(FAILURES)} assertion(s))")
        for failure in FAILURES:
            print(f"  ::error::{failure}")
        return 1

    jobs, raw = load_gate()
    try:
        node = compile_condition(raw)
    except Unevaluatable as exc:
        return die(
            f"`{GATED_JOB}`'s `if:` uses syntax this test cannot evaluate: {exc}\n"
            f"  ::error::the gate reads: {' '.join(raw.split())}\n"
            "  ::error::This is NOT a verdict on the gate - it means scripts/test_app_token_gate.py "
            "must be extended to model whatever was added, because until it is, nothing checks "
            "that the broker's positive assertion still runs on the shapes AWS admits.")

    for event_name, ref, want, why in CASES:
        try:
            got = gate_admits(node, event_name, ref)
        except Unevaluatable as exc:
            return die(f"evaluating the gate for event_name={event_name!r} ref={ref!r} "
                       f"failed: {exc}")
        verb = "run" if want else "skip"
        expect(got is want,
               f"on event_name={event_name!r} ref={ref!r} the gate says "
               f"{'run' if got else 'skip'}, but `{GATED_JOB}` must {verb}: {why}")

    # The negative job's freedom from a gate is an assertion in its own right. Its denial comes
    # from `job_workflow_ref` alone, which every condition pair enforces regardless of `sub`, so it
    # holds on every event shape - including the feature-branch push where the positive job is
    # skipped, and where it is then the only thing still asserting anything. Copying the gate onto
    # it "for symmetry" would silently reduce the run to zero broker assertions on exactly the
    # shape this whole arrangement was written to survive.
    if UNGATED_JOB not in jobs:
        return die(f"{WORKFLOW} has no `{UNGATED_JOB}` job, so the broker's negative assertion is "
                   "gone or renamed and this test no longer covers it")
    if "if" in jobs[UNGATED_JOB]:
        expect(False,
               f"`{UNGATED_JOB}` has grown an `if:` gate ({jobs[UNGATED_JOB]['if']!r}). It must "
               "not have one: it is event-independent, and it is the only assertion left running "
               "on the shapes where the positive job is skipped. Gating it makes a run with no "
               "broker coverage at all indistinguishable from a green one")

    if FAILURES:
        print(f"app-token gate: FAILED ({len(FAILURES)} assertion(s))")
        for failure in FAILURES:
            print(f"  ::error::{failure}")
        return 1

    print(f"app-token gate: OK ({len(CASES)} caller shapes evaluated against the real `if:` "
          f"expression read from {WORKFLOW.name}, and `{UNGATED_JOB}` carries no gate)")
    print("  note: this proves the gate matches the sub shapes it was WRITTEN for; it cannot see "
          "inboxxhq-infra's ci_caller_subs, so a change there needs a matching change here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
