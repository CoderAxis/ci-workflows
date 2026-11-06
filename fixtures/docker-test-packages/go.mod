module example.com/dockerfixture

go 1.22

require (
	github.com/moby/moby/client v0.0.0
	github.com/testcontainers/testcontainers-go v0.0.0
)

replace (
	github.com/moby/moby/client => ./stubs/moby-client
	github.com/testcontainers/testcontainers-go => ./stubs/testcontainers-go
)
