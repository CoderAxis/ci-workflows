// Package ids reproduces the mustUUIDv7 anti-pattern verbatim as it appeared in
// eleven repositories, plus the wrapper form where the lie sits one hop from the
// generator (voice-ai-agent-core-postgres).
package ids

import "github.com/google/uuid"

// mustUUIDv7 is the UUID-0003 case: two declared parameters, neither read, a
// fresh id per call. Nothing about the sink matters; the function cannot do what
// its signature promises.
func mustUUIDv7(_ uuid.UUID, _ []byte) uuid.UUID {
	return uuid.Must(uuid.NewV7())
}

// DeterministicCallID is the UUID-0004 case: the parameter IS read, so the
// unused-parameter rule cannot see it, but no return path derives anything.
func DeterministicCallID(callSID string) uuid.UUID {
	if callSID == "" {
		return uuid.Must(uuid.NewV7())
	}
	return mustUUIDv7(uuid.NameSpaceURL, []byte(callSID))
}
