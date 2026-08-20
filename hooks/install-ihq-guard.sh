#!/usr/bin/env bash
# Superseded by `ihq git hooks install --fleet`. This forwards to it.
#
# Three installers existed: `make install-hooks` in 33 repositories, a
# scripts/install-git-hooks.sh in 35, and this. They wrote different hooks, so what a
# repository checked depended on which one had last been run in it. One command now
# does it, and it is the same command in every repository.
#
# Kept as a forwarder because it is referenced by onboarding docs and by muscle
# memory; there is no reason for either to break.
#
#   ./install-ihq-guard.sh [--dry-run] [workspace-root]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
ROOT=""
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    *) ROOT="${arg}" ;;
  esac
done
ROOT="${ROOT:-$(cd "${HERE}/../.." && pwd)}"

ihq_bin() {
  if [[ -n "${IHQ_BIN:-}" && -x "${IHQ_BIN}" ]]; then echo "${IHQ_BIN}"; return 0; fi
  if command -v ihq >/dev/null 2>&1; then command -v ihq; return 0; fi
  if [[ -x "${ROOT}/tools/inboxxhq-cli/ihq" ]]; then echo "${ROOT}/tools/inboxxhq-cli/ihq"; return 0; fi
  return 1
}

if ! BIN="$(ihq_bin)"; then
  echo "the ihq CLI is required: (cd ${ROOT}/tools/inboxxhq-cli && make install)" >&2
  exit 1
fi

echo "note: this script now forwards to \`ihq git hooks install --fleet\`."
echo

args=(git hooks install --fleet)
[[ "${DRY_RUN}" == "1" ]] && args+=(--dry-run)

exec "${BIN}" "${args[@]}"
