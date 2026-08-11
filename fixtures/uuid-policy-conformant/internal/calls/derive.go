// Package calls pins the precision of UUID-0004. A deriver that falls back to a
// fresh id on empty input has one deterministic return path and one fresh one,
// so its name is honest and it must NOT be flagged. Requiring only ONE
// deterministic path is what separates this from the wrapper-form lie in the
// violating fixture, where no path derived anything.
package calls

import (
	"strings"

	"github.com/google/uuid"
)

var callSIDNamespace = uuid.MustParse("6f7e8a5d-7c45-4e6f-9b41-2c3f9c1a0a9d")

// DeterministicCallUUID derives a stable id from a provider CallSID.
func DeterministicCallUUID(callSID string) uuid.UUID {
	if strings.TrimSpace(callSID) == "" {
		return uuid.Must(uuid.NewV7())
	}
	//uuid:v5 reason=provider-webhook-dedup adr=ADR-0071
	return uuid.NewSHA1(callSIDNamespace, []byte(callSID))
}
