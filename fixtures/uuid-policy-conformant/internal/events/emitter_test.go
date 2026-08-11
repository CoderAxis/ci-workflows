// This file exists to be ignored. A test builds fixtures, so it mints arbitrary
// UUIDs and puts them wherever the assertion needs them — including straight
// into EventID, which would be a UUID-0001 violation in production code. Scanning
// tests would produce a large volume of findings that are all noise, and noise is
// what gets a gate switched off. scan.skip_test_files pins that decision here.
package events

import (
	"testing"

	"github.com/google/uuid"

	"github.com/coderaxis/platform-shared-go/outbox"
)

var fixtureNamespace = uuid.MustParse("11111111-2222-3333-4444-555555555555")

func TestFixtureEventUsesDeterministicEventID(t *testing.T) {
	ev := outbox.Event{
		EventID:        uuid.NewSHA1(fixtureNamespace, []byte("golden-vector")),
		IdempotencyKey: uuid.Must(uuid.NewV7()),
		EventType:      "usage.recorded.v1",
	}
	if ev.EventID == uuid.Nil {
		t.Fatal("fixture event id must be set")
	}
}
