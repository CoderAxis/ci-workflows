module github.com/coderaxis/fixture-service

go 1.24

// The catalog-required modules are both clean. The violation is platform-shared-go, which no
// catalog entry names - it is pinned by 86 repositories and was therefore checked by nothing
// before the governed sweep existed. This fixture fails only if that sweep is working.
require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v1.4.2
	github.com/coderaxis/platform-shared-go v0.0.0-20260518192409-9f39cdd313ed
)
