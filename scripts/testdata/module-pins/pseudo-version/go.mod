module github.com/coderaxis/fixture-service

go 1.24

// The adapter is pinned to an untagged commit. On a release branch this must block: the version
// cannot be resolved back to a release, so the provenance claim of anything built from it is void.
require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v0.0.0-20260518192409-9f39cdd313ed
)
