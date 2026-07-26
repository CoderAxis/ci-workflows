module github.com/coderaxis/fixture-service

go 1.24

require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v1.4.2
)

// A local-path replace makes the build depend on a working tree that exists on one machine, so
// what CI validates is not what ships. Must block regardless of how the pin itself looks.
replace github.com/coderaxis/fixture-core => ../fixture-core
