#!/usr/bin/env bash
# The tool set a runner in the inboxxhq-ci / inboxxhq-deploy pools must provide.
#
# The fleet's reusable workflows (CoderAxis/ci-workflows) were written against GitHub's
# ubuntu-latest image and assume its tools are simply there. The self-hosted scale-set image
# (inboxxhq-infra ci-workflows-runner/runner-image/Dockerfile.gha) starts from GitHub's minimal
# runner image, so every one of those assumptions is a package somebody has to have added - and
# each one missing so far was discovered by a lane failing after the image had rolled: python3
# (nineteen lanes, 2026-09-04), gh and shellcheck (2026-09-04), envsubst (2026-09-05), and the
# C toolchain (2026-09-05: module-release.yaml's `go test -race` needs cgo, Go disables cgo
# without a compiler, and every module release in the fleet died before tagging).
#
# This script is the executable form of that contract. It exists in two places and the two
# copies are kept byte-identical:
#   - CoderAxis/ci-workflows scripts/runner-toolbox-contract.sh, run on a LIVE runner by
#     runner-toolbox-contract.yaml, daily and on demand, so the image the pool actually runs
#     is checked and not only the one that was built;
#   - inboxxhq-infra ci-workflows-runner/runner-image/toolbox-contract.sh, run INSIDE a freshly
#     built image by runner-image/build.sh before anything is pushed.
# It exits non-zero and lists every missing tool, so one run shows the whole gap.
#
# Adding a tool to the image means adding its probe here, in both copies, in the same change.
set -uo pipefail

missing=()

# need <label> <probe command...>: the probe must exit 0 when the tool is usable.
need() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  ok       %s\n' "${label}"
  else
    printf '  MISSING  %s\n' "${label}"
    missing+=("${label}")
  fi
}

echo "runner toolbox contract on $(uname -m) ($(hostname))"

# The ubuntu-latest tool set the lanes use directly.
need "bash" bash --version
need "git" git --version
need "curl" curl --version
need "jq" jq --version
need "make" make --version
need "tar" tar --version
need "unzip" unzip -v
need "xz" xz --version
need "zip" zip -v
need "aws CLI v2" aws --version

# Nineteen ci-workflows lanes run python scripts; several import yaml and fall back to
# `pip install pyyaml` (which needs pip and, in some lanes, a venv).
need "python3" python3 --version
need "python3: yaml module" python3 -c 'import yaml'
need "python3: pip" python3 -m pip --version
need "python3: venv" python3 -m venv --help

# Called directly by ci-workflows steps: gh (23 steps), shellcheck (3 lint steps),
# envsubst (sigstore/cosign-installer's install script).
need "gh" gh --version
need "shellcheck" shellcheck --version
need "envsubst (gettext-base)" envsubst --version

# The C toolchain for cgo: module-release.yaml runs `go test -race` on every Go module and
# the race detector needs cgo. A compiler binary alone is not enough - linking needs the
# libc headers and static bits from libc6-dev - so compile and run a program rather than
# print a version.
need "gcc" gcc --version
# shellcheck disable=SC2016  # the probe is a script for the inner bash; nothing here should expand
need "C toolchain compiles and links a program (libc6-dev)" bash -c '
  tmp="$(mktemp -d)" || exit 1
  printf "int main(void) { return 0; }\n" > "${tmp}/probe.c"
  gcc -o "${tmp}/probe" "${tmp}/probe.c" && "${tmp}/probe"
  rc=$?
  rm -rf "${tmp}"
  exit "${rc}"
'

# On a live runner Go is installed by actions/setup-go before this runs; then the real
# question can be asked directly. Inside the image at build time there is no Go, and the
# compile probe above stands in for it.
if command -v go >/dev/null 2>&1; then
  # shellcheck disable=SC2016  # evaluated by the inner bash, on purpose
  need "Go reports cgo enabled (go env CGO_ENABLED = 1)" bash -c '[ "$(go env CGO_ENABLED)" = "1" ]'
fi

# The gha-runner-scale-set chart starts the runner with /home/runner/run.sh; the
# summerwind-based image did not have it and every pod died on exec (2026-09-04).
need "scale-set runner layout (/home/runner/run.sh)" test -x /home/runner/run.sh

# Informational only: the pools run no Docker daemon by design (image builds go to
# CodeBuild), so a docker CLI here would be misleading rather than useful.
if command -v docker >/dev/null 2>&1; then
  echo "  note     docker CLI present; the pools run no daemon, so it cannot be used"
fi

if ((${#missing[@]} > 0)); then
  echo
  echo "runner toolbox contract FAILED: ${#missing[@]} missing"
  for m in "${missing[@]}"; do echo "  - ${m}"; done
  echo "Fix ci-workflows-runner/runner-image/Dockerfile.gha (inboxxhq-infra), rebuild with build.sh, re-apply the pool."
  exit 1
fi
echo "runner toolbox contract OK"
