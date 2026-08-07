// A service pinning platform-shared-go below the gateway-scoped floor. This is the shape 78 of
// the fleet's 88 Go modules were in, and it must pass: the floor is declared for gateways.
module github.com/coderaxis/fixture-service

go 1.24

require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v1.4.2
	github.com/coderaxis/platform-shared-go v1.21.0
)
