// Package keys exists so the violating fixture can route a derivation through a
// second package: taint that stops at a package boundary would miss the shape
// that appeared in chat-service, where the derivation lived in internal/actorref
// and the sinks lived in three other packages.
package keys

import (
	"context"
	"time"

	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

// Derive is a deterministic v5 deriver. Legitimate on its own; the defect is
// what internal/events does with the result.
func Derive(namespace uuid.UUID, data []byte) uuid.UUID {
	return uuid.NewSHA1(namespace, data)
}

// EmitWithFreshKey is the UUID-0002 case, the inverse direction and the more
// dangerous one: idempotency_key must be deterministic (ADR-0071 decision 2), and
// a fresh value here satisfies NOT NULL, satisfies outboxwritepath's non-null
// check, and silently stops every consumer ledger from deduplicating.
func EmitWithFreshKey(ctx context.Context, p *outbox.Publisher, orgID string) error {
	return p.Publish(ctx, outbox.Event{
		IdempotencyKey: uuid.Must(uuid.NewV7()),
		AggregateType:  "billing_invoice",
		EventType:      "billing.invoice.issued.v1",
		OccurredAt:     time.Now().UTC(),
	})
}
