// Package external covers identifiers the platform does not mint: an external
// provider's id parsed off the wire, and a well-known constant pinned in source.
// Neither is a constructor, so neither can be a finding — which is why the gate
// keys on minting sites rather than on "is this value a v7".
package external

import "github.com/google/uuid"

// PlatformGlobalOrgID is the RFC-0032 section 4.1 platform-global sentinel. It is
// not a UUID version at all: uuid_extract_version() returns NULL for it, which is
// why the DB-side companion check has to treat NULL as the sentinel and not as a
// violation.
var PlatformGlobalOrgID = uuid.MustParse("00000000-0000-0000-0000-000000000000")

// WellKnownOrgAlpha is a seeded fixture id pinned as a literal.
var WellKnownOrgAlpha = uuid.MustParse("018f3a4b-0000-7000-8000-000000000001")

// FromProvider adopts a Stripe or Twilio identifier that already is a UUID. Its
// version is whatever the vendor chose and no platform document governs it.
func FromProvider(raw string) (uuid.UUID, error) {
	return uuid.Parse(raw)
}
