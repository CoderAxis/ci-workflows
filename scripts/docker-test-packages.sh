#!/usr/bin/env bash
# Print the packages whose tests need a Docker daemon, one import path per line, sorted.
#
#   docker-test-packages.sh [TAGS]
#
# Run from a Go module root after `go mod download`. TAGS is a build tag list as `go test -tags`
# takes it; empty means the default build.
#
# WHY. service-ci's Build and test runs on the self-hosted pools, which run no Docker daemon, so a
# test that starts a container there fails with "rootless Docker not found" instead of testing
# anything. Those packages are run by the Docker-backed tests job on a GitHub-hosted runner, and
# this is what decides which packages they are.
#
# HOW. A package needs Docker when its test binary, built with TAGS, links a Docker Engine API
# client: testcontainers-go and dockertest both do, and so does a test that drives the daemon
# directly. `go list -test` resolves the whole import graph of each test binary under those tags
# without compiling it, so a test that reaches a client through a shared helper package is found as
# well, and a package cannot opt out by renaming anything.
set -euo pipefail

tags="${1:-}"

# `[.]` rather than `\.`: awk -v processes backslash escapes and would warn on `\.`.
clients='^github[.]com/(testcontainers/testcontainers-go|ory/dockertest|fsouza/go-dockerclient|docker/docker/client|moby/moby/client)(/|$)'

# Only the generated test mains are read. Their Deps are the test binary's full closure, while a
# package's own entry would miss everything its _test.go files import.
GOWORK=off go list -tags="${tags}" -test \
  -f '{{if eq .Name "main"}}{{.ImportPath}} {{join .Deps " "}}{{end}}' ./... \
  | awk -v clients="${clients}" '
      $1 ~ /[.]test$/ {
        for (i = 2; i <= NF; i++) {
          if ($i ~ clients) { sub(/[.]test$/, "", $1); print $1; next }
        }
      }' \
  | sort -u
