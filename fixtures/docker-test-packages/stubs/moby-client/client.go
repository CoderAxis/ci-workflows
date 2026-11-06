// Package client stands in for the Docker Engine API client a test can drive the daemon with.
package client

import "os"

// DaemonReachable is true only where the fixture's caller says a Docker daemon is present.
func DaemonReachable() bool { return os.Getenv("FIXTURE_DOCKER") == "1" }
