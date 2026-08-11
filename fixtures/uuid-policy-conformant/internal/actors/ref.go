// Package actors is the declared-exception case: a deterministic reference for a
// principal with no platform identity. The reason is not documented by any sink,
// so it needs a marker — and the marker plus the code it justifies land in one
// diff hunk, in front of the reviewer who can judge the claim.
package actors

import (
	"strings"

	"github.com/google/uuid"
)

var phoneNamespace = uuid.MustParse("2b1a0c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")

// CustomerRef returns a stable reference for an inbound phone number. A fresh id
// per message would scatter one participant across single-use identities and
// leave a thread with no coherent sender.
func CustomerRef(phone string) uuid.UUID {
	//uuid:v5 reason=deterministic-actor-ref adr=ADR-0071
	return uuid.NewSHA1(phoneNamespace, []byte("tel:"+strings.TrimSpace(phone)))
}
