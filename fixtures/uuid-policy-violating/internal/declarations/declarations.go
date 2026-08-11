// Package declarations holds the two ways a declaration itself can be wrong.
// Both are UUID-0006: the mechanism has to police its own instruments, or a
// stale or invented excuse becomes a permanent hole.
package declarations

import "github.com/google/uuid"

// StaleMarker carries a declaration whose constructor is gone. The marker used
// to sit above a uuid.NewSHA1 call; the call was replaced and the excuse stayed.
// This is the failure mode that made log-schema.yaml's event_key_allowlist
// unfalsifiable, and pairing markers to call sites is what makes it decidable.
//
//uuid:v5 reason=deterministic-actor-ref adr=ADR-0071
func StaleMarker(phone string) uuid.UUID {
	return uuid.Must(uuid.NewV7())
}

// InventedReason cites a token that is not in the sanctioned vocabulary, which
// is how a per-site marker would otherwise degrade into a free-text suppression.
func InventedReason(namespace uuid.UUID, data []byte) uuid.UUID {
	//uuid:v5 reason=because-i-said-so adr=ADR-0071
	return uuid.NewSHA1(namespace, data)
}

// WrongVersion declares v3 next to a v5 constructor.
func WrongVersion(namespace uuid.UUID, data []byte) uuid.UUID {
	//uuid:v3 reason=deterministic-actor-ref adr=ADR-0071
	return uuid.NewSHA1(namespace, data)
}

// WrongADR cites an ADR that does not sanction the reason it names.
func WrongADR(namespace uuid.UUID, data []byte) uuid.UUID {
	//uuid:v5 reason=seed-well-known-id adr=ADR-0035
	return uuid.NewSHA1(namespace, data)
}
