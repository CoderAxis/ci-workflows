//go:build integration

package viahelper_test

import (
	"testing"

	"example.com/dockerfixture/internal/dockerhelp"
	"example.com/dockerfixture/viahelper"
)

func TestThroughTheHelper(t *testing.T) {
	if !dockerhelp.Up() {
		t.Fatal("no Docker daemon")
	}
	if viahelper.Value() != 7 {
		t.Fatal("wrong value")
	}
}
