package plain

import "testing"

func TestAnswer(t *testing.T) {
	if Answer() != 42 {
		t.Fatal("wrong answer")
	}
}
