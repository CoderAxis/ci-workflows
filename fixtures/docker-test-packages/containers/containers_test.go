package containers

import "testing"

func TestUnit(t *testing.T) {
	if Unit() != "unit" {
		t.Fatal("wrong value")
	}
}
