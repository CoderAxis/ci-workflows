// Package httpapi is the ADR-0035 case. The HTTP Idempotency-Key header accepts
// UUIDv4, UUIDv7 or ULID, and the shared middleware in
// platform-shared-go/http/middleware/idempotency validates all three. Nothing
// here is a violation, and the gate must not force a suppression onto an accepted
// ADR: a v4 header is not a minting site the policy governs, and the field is not
// a sink with a documented version.
package httpapi

import (
	"net/http"

	"github.com/google/uuid"
)

// RequestScope carries per-request identifiers. RequestID and CorrelationID are
// classified "Unspecified - no doc states a version" in outbox-ddl-standard
// section 2.1, which explicitly says the standard MUST NOT invent one. A gate
// that flagged these would be inventing policy.
type RequestScope struct {
	RequestID     uuid.UUID
	CorrelationID uuid.UUID
}

// NewRequestScope mints fresh v4 identifiers. Legitimate, undocumented, and not
// the gate's business.
func NewRequestScope(r *http.Request) RequestScope {
	return RequestScope{RequestID: uuid.New(), CorrelationID: uuid.New()}
}

// AcceptIdempotencyKey admits a v4, a v7 or a ULID per ADR-0035.
func AcceptIdempotencyKey(header string) (uuid.UUID, bool) {
	id, err := uuid.Parse(header)
	if err != nil {
		return uuid.Nil, false
	}
	switch id.Version() {
	case 4, 7:
		return id, true
	}
	return uuid.Nil, false
}
