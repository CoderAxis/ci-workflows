#!/usr/bin/env bash
# Shared contract guard, driven entirely by the ihq CLI.
#
# WHY THIS IS A LIBRARY AND NOT A HOOK
# ------------------------------------
# The fleet has five distinct pre-commit variants across 34 repositories and 84 repositories
# with no hooks at all. Rewriting them would throw away service-specific gates that already
# work (auth's auth-core sync check, the oasdiff baseline comparison in pre-push). So this is
# sourced by a hook rather than replacing it: existing gates keep running, and this adds the
# contract checks that were previously CI-only.
#
# WHAT IT ENFORCES
# ----------------
# Everything `ihq validate` checks for THIS repository: operationId presence, uniqueness and
# ADR-0006 naming, the operationId lockfile, request and response examples, response schemas,
# the RFC-0001 envelope and its binding to common.v1, RFC-0003 pagination, RFC-0002 error
# codes, that no legacy swagger file is committed, and that every call to a central workflow
# pins @v1.
#
# WHY IT IS THIN
# --------------
# It used to scope the report itself: `ihq validate` covered the whole workspace and exited 1
# if any service had errors, which in a hook is useless — committing to auth would fail
# because authz has errors — and `ihq validate auth` also matched authz. So the guard parsed
# --json in Python and picked out its own rows. The CLI now takes `--repo`, which selects one
# repository exactly, resolves without a configured workspace, scopes the exit code to that
# repository, and applies only the checks the repo actually owes. All of that scoping logic
# is gone from here, along with the python3 dependency, because the tool does it.
#
# THERE IS NO BYPASS
# ------------------
# This had a SKIP_IHQ_GUARD escape hatch. It is gone: a gate with a documented bypass is a
# suggestion, and the findings it was being skipped for are the ones that reach production.
#
# Be clear about what that does and does not buy. `git commit --no-verify` and
# `git push --no-verify` skip local hooks entirely — that is built into git and no hook can
# prevent it. So this is early detection, not enforcement. The enforcement is the same check
# running in CI as a required status check, where there is no --no-verify. Treat a failure here
# as the answer CI will give you, delivered sooner.
#
# Usage:  ihq_guard <fast|full>
#   fast  - only when a contract-affecting file is staged (pre-commit)
#   full  - always (pre-push)
#
# Environment:
#   IHQ_BIN           explicit path to the ihq binary
#   IHQ_GUARD_FAIL_ON lowest severity that blocks: error (default), warning, info.
#                     It can only be made STRICTER than the default, never looser.

# --- locating the CLI ------------------------------------------------------------------------

ihq_guard_bin() {
  if [[ -n "${IHQ_BIN:-}" && -x "${IHQ_BIN}" ]]; then echo "${IHQ_BIN}"; return 0; fi
  if command -v ihq >/dev/null 2>&1; then command -v ihq; return 0; fi
  # Developer running from the multi-repo workspace before `ihq` is on PATH.
  local root; root="$(git rev-parse --show-toplevel 2>/dev/null)"
  local up="${root}"
  while [[ -n "${up}" && "${up}" != "/" ]]; do
    if [[ -x "${up}/tools/inboxxhq-cli/ihq" ]]; then echo "${up}/tools/inboxxhq-cli/ihq"; return 0; fi
    up="$(dirname "${up}")"
  done
  return 1
}

# --- the pin the CI resolves ------------------------------------------------------------------
#
# WHY THIS EXISTS
# ---------------
# A developer building in the multi-repo workspace resolves platform-shared-go and every core
# module to the checkout beside them. CI sets GOWORK=off and resolves what go.mod pins. Those are
# different builds, and nothing said so, which meant a change could compile perfectly for the
# person writing it and not compile at all anywhere else.
#
# That is not hypothetical. A sweep moved the fleet onto a new servicetokenjwt.GenerateServiceToken
# signature, envutil.IsDeployed and grpcauth.NewMapAuthorizer, released platform-shared-go, and
# bumped no consumer. Every repository still built locally. Twenty-nine of ninety-nine did not
# build in CI, including nine core modules -- and a core module whose CI is red cannot be released,
# because the release workflow requires a green run for the exact commit. Consumers then could not
# be bumped onto a fix that was never published. The workspace hid the break, and the break blocked
# its own repair.
#
# The check is the same build CI runs, one push earlier. It is deliberately not clever: no parsing
# of go.mod against symbol requirements, just the compiler, told to resolve modules the way the
# machine that gates the merge will.
ihq_guard_pins() {
  local label="$1" repo_root="$2"

  [[ -f "${repo_root}/go.mod" ]] || return 0
  command -v go >/dev/null 2>&1 || return 0

  echo "${label}: building with GOWORK=off, the way CI resolves modules…" >&2

  local out
  if out="$(cd "${repo_root}" && GOWORK=off go build ./... 2>&1)"; then
    return 0
  fi

  echo "${label}: this repository does not build the way CI builds it." >&2
  echo "" >&2
  echo "${out}" | head -n 15 >&2
  echo "" >&2

  # An undefined symbol from a module this repo pins is the signature of a stale pin, and it is
  # worth saying so outright: the compiler's own message names a symbol, not a version, so the
  # reader is otherwise left to work out that the code is newer than the dependency.
  if echo "${out}" | grep -qE 'undefined:|not enough arguments|too many arguments|does not implement'; then
    echo "${label}: this usually means the code here is newer than a module it pins." >&2
    echo "  It compiles for you because the workspace resolves that module to your checkout." >&2
    echo "  CI has no workspace and resolves the pin, which does not carry the symbol yet." >&2
    echo "" >&2
    echo "  Fix the pin rather than the workspace:" >&2
    echo "    GOWORK=off go get <module>@<version> && GOWORK=off go mod tidy" >&2
    echo "" >&2
    echo "  If the version carrying it was never released, that release comes first." >&2
  fi

  return 1
}

# Does THIS repository's CI actually run a contract check?
#
# Only used to word the blocked message truthfully. Absence is reported as absence,
# never as a pass — the findings stand either way.
#
# Almost every service reaches the check INDIRECTLY: its ci.yaml is three lines
# calling the shared service-ci.yaml, and check-api-contract runs in there. Looking
# only for the checker's own name therefore answers "no" for the entire fleet,
# which is the wrong answer in the more dangerous direction — it would tell a
# service developer their findings are not enforced when they are. So match the
# reusable callers too. openapi-contract.yaml is covered by the api-contract
# pattern, since the name contains it.
#
# Comment lines are dropped before matching. Without that, this answered "yes" for
# inboxxhq-cli on the strength of a COMMENT in its ci.yaml stating it deliberately
# does NOT use service-ci.yaml — the same defect as the detector whose findings
# this hook prints, reached the same way: prose naming a thing read as the thing.
ihq_guard_ci_checks_contract() {
  local root="$1"
  local wf="${root}/.github/workflows"
  [[ -d "${wf}" ]] || return 1
  grep -rhv '^[[:space:]]*#' "${wf}" 2>/dev/null \
    | grep -qE 'check-api-contract|api-contract\.ya?ml|service-ci\.ya?ml|ihq[[:space:]]+validate'
}

# --- entry point ------------------------------------------------------------------------------

ihq_guard() {
  local mode="${1:-fast}"
  local repo_root; repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  local label="ihq-guard"
  # Only ever stricter than the default. A repo may hold itself to warnings; it may not
  # quietly lower the bar, which is the one direction that turns a gate into a formality.
  local fail_on="error"
  case "${IHQ_GUARD_FAIL_ON:-}" in
    warning|info) fail_on="${IHQ_GUARD_FAIL_ON}" ;;
  esac

  # In fast mode only look when something contract-affecting is staged, so an unrelated commit
  # is not made to wait on a scan.
  if [[ "${mode}" == "fast" ]]; then
    local touched=0 f
    while IFS= read -r f; do
      case "${f}" in
        docs/openapi*.json|docs/openapi*.go|.github/workflows/*) touched=1; break;;
      esac
    done < <(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
    [[ "${touched}" == "1" ]] || return 0
  fi

  local bin
  if ! bin="$(ihq_guard_bin)"; then
    # A guard that quietly passes because its tool is missing is worse than no guard: the commit
    # looks checked and is not. Blocking is recoverable in one command; a bad contract is not.
    echo "${label}: the ihq CLI was not found, so nothing was checked." >&2
    echo "  install it, or set IHQ_BIN=/path/to/ihq." >&2
    return 1
  fi

  echo "${label}: checking this repository via $(basename "${bin}")…" >&2
  if ! "${bin}" validate --repo "${repo_root}" --fail-on "${fail_on}" --details; then
    # "CI runs the same check" was printed unconditionally, and it is not true
    # everywhere: a repo whose workflows contain no contract job — inboxxhq-cli is
    # one, its CI is vet/test/coverage/cross-compile — stays green on main while
    # this hook blocks. Telling someone the gate they just hit is the gate CI will
    # apply, when it demonstrably is not, is how a gate teaches people to reach for
    # --no-verify: the first time the claim is caught out, the whole message stops
    # being believed. So say only what is checkable from here.
    if ihq_guard_ci_checks_contract "${repo_root}"; then
      echo "${label}: blocked. This repository's CI runs the same check, so this is the result you would get there." >&2
    else
      echo "${label}: blocked. NOTE: this repository's CI does not run a contract check, so these" >&2
      echo "  findings will NOT appear there — this hook is ahead of it. That is a gap in the" >&2
      echo "  pipeline, not a reason the findings are wrong." >&2
    fi
    return 1
  fi

  echo "${label}: ✓ contract clean" >&2

  # Only on push. A build is too slow to sit in front of every commit, and the divergence this
  # catches only matters once the code leaves the machine that was hiding it.
  if [[ "${mode}" == "full" ]]; then
    ihq_guard_pins "${label}" "${repo_root}" || return 1
    echo "${label}: ✓ builds against its pins" >&2
  fi

  return 0
}
