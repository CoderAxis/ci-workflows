module github.com/coderaxis/fixture-service

go 1.24

// The catalog says this service pins both modules directly, but the core arrives only
// transitively. Go then resolves it to whatever the adapter asks for, so bumping the core here
// would change nothing and the service would silently keep running old code.
require (
	github.com/coderaxis/fixture-core v0.16.4 // indirect
	github.com/coderaxis/fixture-core-postgres v1.4.2
)
