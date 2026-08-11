// Package events reproduces the defect that shipped in
// gateways/inboxxhq-voice-gateway before 92a7df2: a deterministic UUIDv5 handed
// to the outbox as event_id. The derivation is one hop away from the sink, which
// is what made it survive review.
package events

import (
	"context"
	"time"

	"example.com/uuid-policy-violating/internal/keys"
	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

var usageNamespace = uuid.MustParse("6b6f7d2a-9d2e-4f5a-9c3b-2c1f0a7e4b10")

// EmitUsage is the UUID-0001 case: event_id must be a fresh v7 per ADR-0071
// decision 1, and this supplies a value derived from the idempotency key.
func EmitUsage(ctx context.Context, p *outbox.Publisher, orgID, key string) error {
	return p.Publish(ctx, outbox.Event{
		EventID:       deterministicEventID(usageNamespace, []byte("usage:"+key)),
		AggregateType: "voice_usage",
		EventType:     "usage.recorded.v1",
		OccurredAt:    time.Now().UTC(),
	})
}

// EmitTeardown is the same defect reached through a local variable rather than
// an inline call, so the taint has to survive an assignment.
func EmitTeardown(ctx context.Context, p *outbox.Publisher, orgID string) error {
	eventID := deterministicEventID(usageNamespace, []byte("teardown:"+orgID))
	return p.Publish(ctx, outbox.Event{
		EventID:       eventID,
		AggregateType: "subscription_teardown",
		EventType:     "subscription.resources.teardown.step.completed.v1",
		OccurredAt:    time.Now().UTC(),
	})
}

// EmitCrossPackage reaches the same defect through a function in another package
// of this same repository.
func EmitCrossPackage(ctx context.Context, p *outbox.Publisher, orgID string) error {
	return p.Publish(ctx, outbox.Event{
		EventID:       keys.Derive(usageNamespace, []byte("cross:"+orgID)),
		AggregateType: "voice_usage",
		EventType:     "usage.recorded.v1",
		OccurredAt:    time.Now().UTC(),
	})
}

func deterministicEventID(namespace uuid.UUID, data []byte) uuid.UUID {
	return uuid.NewSHA1(namespace, data)
}
