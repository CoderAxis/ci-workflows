// Package testcontainers stands in for github.com/testcontainers/testcontainers-go. The detector
// matches the import path, so all this needs is to fail the way the real one does without a daemon.
package testcontainers

import "os"

// DaemonReachable is true only where the fixture's caller says a Docker daemon is present.
func DaemonReachable() bool { return os.Getenv("FIXTURE_DOCKER") == "1" }
