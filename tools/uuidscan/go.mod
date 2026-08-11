// uuidscan is stdlib-only on purpose: it must build and run in any consumer
// repo's CI job with no module proxy, no private-module credentials, and no
// dependency on the repo under test. Adding a require here would put a network
// dependency in front of a fleet-wide gate.
module github.com/coderaxis/ci-workflows/tools/uuidscan

go 1.22
