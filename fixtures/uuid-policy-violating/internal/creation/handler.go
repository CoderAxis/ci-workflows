// Package creation is where identity-core actually built the event: one package
// away from the type that declares the accessor. The literal spells
// identity.IdentityCreatedEvent, so the field's role is decidable — but only if
// the scan reaches package scope across the module rather than one file at a
// time.
package creation

import (
	"time"

	"github.com/google/uuid"

	"example.com/uuid-policy-violating/internal/identity"
)

// Handle is the live identity-creation path. The v4 is fresh, so nothing about
// it looks like the deterministic conflation this control was first written for;
// it is still refused at the write, because ADR-0071 decision 1 asks for a
// time-sortable v7 and resolveEventID enforces exactly that.
func Handle(email string) identity.IdentityCreatedEvent {
	return identity.IdentityCreatedEvent{
		ID:         uuid.New(),
		IdentityID: uuid.New(),
		Email:      email,
		Timestamp:  time.Now().UTC(),
	}
}
