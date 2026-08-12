// Package identity is identity-core after 315de91: the event id is still spelled
// ID and still identified only by the accessor, and it is now minted as a v7. The
// inference must recognise the sink here exactly as it does in the violating
// fixture, and then find nothing to report — a rule that only ever fires is not
// evidence that it fires for the right reason.
package identity

import (
	"time"

	"github.com/google/uuid"
)

type IdentityCreatedEvent struct {
	ID         uuid.UUID
	IdentityID uuid.UUID
	Email      string
	Timestamp  time.Time
}

func (e IdentityCreatedEvent) EventID() uuid.UUID     { return e.ID }
func (e IdentityCreatedEvent) AggregateID() uuid.UUID { return e.IdentityID }

// NewIdentityCreated takes the occurrence id from the v7 seam. The aggregate id
// keeps its v4 fallback: no document versions it, and it is not the event id, so
// the accessor does not bind it.
func NewIdentityCreated(email string) IdentityCreatedEvent {
	eventID, err := uuid.NewV7()
	if err != nil {
		// Zero is the publisher's documented signal to mint one itself, which is
		// preferable to a v4 it would refuse.
		eventID = uuid.Nil
	}
	return IdentityCreatedEvent{
		ID:         eventID,
		IdentityID: uuid.New(),
		Email:      email,
		Timestamp:  time.Now().UTC(),
	}
}
