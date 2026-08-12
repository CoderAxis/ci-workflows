// Package identity reproduces the field name that made this gate miss a live
// defect. identity-core spells the outbox event id `ID`, not `EventID`, and only
// IdentityCreatedEvent.EventID() says which field the event id is. A gate
// matching field NAMES alone emitted no sink fact for the whole repository, so
// the v4 that platform-shared-go v1.36.0 refuses at the write passed CI.
package identity

import (
	"time"

	"github.com/google/uuid"
)

// Event is the shape identity-core's domain events satisfy. The accessor is the
// only thing that identifies which field carries occurrence identity, which is
// why it is what the scanner infers from.
type Event interface {
	EventID() uuid.UUID
	AggregateID() uuid.UUID
}

// IdentityCreatedEvent carries the event id in a field called ID, and the
// aggregate id in one called IdentityID.
type IdentityCreatedEvent struct {
	ID         uuid.UUID
	IdentityID uuid.UUID
	Email      string
	Timestamp  time.Time
}

func (e IdentityCreatedEvent) EventID() uuid.UUID     { return e.ID }
func (e IdentityCreatedEvent) AggregateID() uuid.UUID { return e.IdentityID }

// NewSamePackage is the UUID-0001 v4 case reached through a local variable, so
// the inference has to survive both the accessor lookup and the taint table.
func NewSamePackage(email string) IdentityCreatedEvent {
	eventID := uuid.New()
	return IdentityCreatedEvent{
		ID:         eventID,
		IdentityID: uuid.New(),
		Email:      email,
		Timestamp:  time.Now().UTC(),
	}
}
