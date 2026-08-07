// The same stale pin in a gateway, which the floor is declared for and which must block. The
// module path is what makes it a gateway here; detect_role also reads service.contract.yaml,
// and the sibling fixture file covers that path.
module github.com/coderaxis/fixture-edge-gateway

go 1.24

require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v1.4.2
	github.com/coderaxis/platform-shared-go v1.21.0
)
