//go:build integration

package containers

import (
	"testing"

	"github.com/testcontainers/testcontainers-go"
)

func TestStartsAContainer(t *testing.T) {
	if !testcontainers.DaemonReachable() {
		t.Fatal("start postgres container: rootless Docker not found")
	}
	if NeedsDocker() != "partition" {
		t.Fatal("wrong value")
	}
}
