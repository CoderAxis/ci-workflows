// Package undeclared holds a deterministic derivation with no declaration and no
// documented sink: the plain UUID-0005 case, which is the "unless a documented
// reason exists" half of the policy.
package undeclared

import (
	"strings"

	"github.com/google/uuid"
)

// TenantSlugID derives an id from a tenant slug. It may well be correct; nothing
// in any ADR or standard says so, which is precisely the finding.
func TenantSlugID(slug string) uuid.UUID {
	return uuid.NewSHA1(uuid.NameSpaceOID, []byte("tenant:"+strings.ToLower(slug)))
}
