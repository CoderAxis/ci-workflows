//go:build fixture_untagged_docker

package untagged

import (
	"testing"

	"github.com/testcontainers/testcontainers-go"
)

func TestStartsAContainerWithoutTheTag(t *testing.T) {
	if !testcontainers.DaemonReachable() {
		t.Fatal("rootless Docker not found")
	}
}
