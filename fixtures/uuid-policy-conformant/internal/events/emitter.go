// Package events is the voice-gateway emitter as it looks AFTER 92a7df2: the
// derived value moved to IdempotencyKey and EventID is left zero so
// outbox.resolveEventID mints the v7.
//
// This file is also the reason the gate parses Go instead of grepping it. The doc
// comment below quotes both `uuid.NewSHA1` and `uuid.Must(uuid.NewV7())` while
// explaining the defect that was removed — exactly as the real fixed file does.
// A text scan flags the fix; go/parser puts comments in file.Comments and
// constructors in the AST, so prose can never be mistaken for a call.
package events

import (
	"context"
	"time"

	"github.com/coderaxis/platform-shared-go/outbox"
	"github.com/google/uuid"
)

var usageNamespace = uuid.MustParse("6b6f7d2a-9d2e-4f5a-9c3b-2c1f0a7e4b10")

// EmitUsage leaves EventID zero deliberately.
func EmitUsage(ctx context.Context, p *outbox.Publisher, orgID, key string) error {
	return p.Publish(ctx, outbox.Event{
		IdempotencyKey: deterministicIdempotencyKey(usageNamespace, []byte("usage:"+key)),
		AggregateType:  "voice_usage",
		EventType:      "usage.recorded.v1",
		OccurredAt:     time.Now().UTC(),
	})
}

// deterministicIdempotencyKey derives the row's dedup identity.
//
// This helper used to be a stray "mustUUIDv7" that ignored both arguments and
// returned uuid.Must(uuid.NewV7()) -- a fresh random id on every call. Replacing
// it with uuid.NewSHA1 (RFC 4122 version 5) is what "deterministic" requires.
// It needs no declaration marker: the value flows into Event.IdempotencyKey,
// whose deterministic requirement ADR-0071 decision 2 already documents, so a
// marker here would be a suppression with no reader.
func deterministicIdempotencyKey(namespace uuid.UUID, data []byte) uuid.UUID {
	return uuid.NewSHA1(namespace, data)
}
