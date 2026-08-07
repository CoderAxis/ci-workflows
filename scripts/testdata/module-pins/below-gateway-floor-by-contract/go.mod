// A gateway whose module path does not say so. Only the service contract does, which is the
// signal check-gateway-baseline.py reads first - so a floor scoped to gateways has to see it
// here too, or the two would disagree about which repositories are gateways.
module github.com/coderaxis/fixture-frontdoor

go 1.24

require (
	github.com/coderaxis/fixture-core v0.16.4
	github.com/coderaxis/fixture-core-postgres v1.4.2
	github.com/coderaxis/platform-shared-go v1.21.0
)
