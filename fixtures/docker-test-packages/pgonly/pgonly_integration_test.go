//go:build integration

package pgonly

import "testing"

func TestTagged(t *testing.T) {
	if Tagged() != "tagged" {
		t.Fatal("wrong value")
	}
}
