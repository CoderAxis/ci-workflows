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
    echo "${label}: blocked. CI runs the same check, so this is the result you would get there." >&2
    return 1
  fi

  echo "${label}: ✓ contract clean" >&2
  return 0
}
