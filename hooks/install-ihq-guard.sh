#!/usr/bin/env bash
# Install the shared ihq contract guard into every repository in the workspace.
#
# The fleet has five distinct pre-commit variants across 34 repositories, plus 84 with no
# hooks at all. Replacing them would discard gates that already work - auth's auth-core sync
# check, the oasdiff baseline comparison in pre-push. So this APPENDS a marker-guarded block
# that sources the shared guard, and only writes a hook from scratch where none exists.
#
# Re-running is safe: the marker block is replaced, never duplicated, so bumping the guard is
# just this script again.
#
#   ./install-ihq-guard.sh [--dry-run] [workspace-root]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD_SRC="${HERE}/ihq-guard.sh"
BEGIN="# >>> ihq-guard (managed by ci-workflows/hooks/install-ihq-guard.sh) >>>"
END="# <<< ihq-guard <<<"

DRY_RUN=0
ROOT=""
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    *) ROOT="${arg}" ;;
  esac
done
ROOT="${ROOT:-$(cd "${HERE}/../.." && pwd)}"

[[ -f "${GUARD_SRC}" ]] || { echo "missing ${GUARD_SRC}" >&2; exit 1; }

block_for() {   # $1 = fast|full
  cat <<EOF
${BEGIN}
# Contract checks driven by the ihq CLI. There is no SKIP_IHQ_GUARD: that escape
# hatch was deliberately removed, because a gate with a documented bypass is the
# bypass. git's own --no-verify still skips every hook, and that is the point --
# it leaves a visible choice rather than an env var that reads as sanctioned.
# A failure here is the answer CI will give you, delivered sooner.
if [[ -f "\$(git rev-parse --show-toplevel)/.githooks/lib/ihq-guard.sh" ]]; then
  # shellcheck source=/dev/null
  source "\$(git rev-parse --show-toplevel)/.githooks/lib/ihq-guard.sh"
  ihq_guard $1 || exit 1
fi
${END}
EOF
}

new_hook() {    # $1 = fast|full
  printf '#!/usr/bin/env bash\nset -uo pipefail\n\n'
  block_for "$1"
}

installed=0; appended=0; created=0; skipped=0

while IFS= read -r repo; do
  # A repository, not just a directory: hooks are meaningless without .git.
  [[ -e "${repo}/.git" ]] || continue
  # Only code repos - a docs repo has no contract to check.
  [[ -f "${repo}/go.mod" || -f "${repo}/package.json" ]] || { skipped=$((skipped+1)); continue; }

  if [[ "${DRY_RUN}" == "1" ]]; then
    state="append"; [[ -f "${repo}/.githooks/pre-commit" ]] || state="create"
    echo "  [${state}] ${repo#"${ROOT}"/}"
    installed=$((installed+1))
    continue
  fi

  mkdir -p "${repo}/.githooks/lib"
  cp "${GUARD_SRC}" "${repo}/.githooks/lib/ihq-guard.sh"

  for pair in "pre-commit:fast" "pre-push:full"; do
    hook="${repo}/.githooks/${pair%%:*}"
    mode="${pair##*:}"
    if [[ -f "${hook}" ]]; then
      # Every one of the 68 pre-existing hooks ends in `exit 0`, so appending would leave the
      # guard unreachable - present in the file, never executed, and green. The block therefore
      # goes in BEFORE the last top-level exit, and only at the end when there is none.
      blockfile="$(mktemp)"; block_for "${mode}" > "${blockfile}"
      python3 - "${hook}" "${BEGIN}" "${END}" "${blockfile}" <<'PY'
import re, sys
path, begin, end, blockfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
block = open(blockfile).read().rstrip("\n")
text = open(path).read()

# Drop a previous managed block so re-running replaces rather than duplicates.
text = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)

lines = text.splitlines()
insert_at = len(lines)
for i in range(len(lines) - 1, -1, -1):
    if re.match(r"^\s*exit\s+\d+\s*$", lines[i]):
        insert_at = i
        break

out = lines[:insert_at] + ["", block, ""] + lines[insert_at:]
open(path, "w").write("\n".join(out).rstrip("\n") + "\n")
PY
      rm -f "${blockfile}"
      appended=$((appended+1))
    else
      new_hook "${mode}" > "${hook}"
      created=$((created+1))
    fi
    chmod +x "${hook}"
  done

  git -C "${repo}" config core.hooksPath .githooks 2>/dev/null || true
  installed=$((installed+1))
done < <(find "${ROOT}"/{services,gateways,shared,contracts,frontend,tools} \
              -maxdepth 2 -mindepth 1 -type d 2>/dev/null | sort)

echo
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "dry run: ${installed} repositories would be wired"
else
  echo "ihq-guard installed into ${installed} repositories"
  echo "  hooks extended: ${appended}   hooks created: ${created}   non-code dirs skipped: ${skipped}"
fi
