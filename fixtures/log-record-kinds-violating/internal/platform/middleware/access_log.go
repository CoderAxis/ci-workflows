// Package middleware is the negative half of the check-log-record-kinds.py fixture pair.
//
// Not compiled, not imported, and deliberately wrong in three separate ways so that a regression
// in any one rule turns this fixture green and fails the self-test. Each violation below is one
// that actually shipped, which is why the fixture reproduces them rather than inventing simpler
// ones - see Core ADR-0104.
package middleware

import (
	"context"

	"github.com/coderaxis/platform-shared-go/logging"
)

// AccessLogMiddleware violates rule 4: the http_request_completed emitter is owned by
// platform-shared-go/platform/ginmiddleware, and a local copy drifts from the catalogue silently
// while its own tests keep passing. Twelve gRPC copies and five HTTP copies were found this way.
func AccessLogMiddleware(ctx context.Context, log logging.Logger) {
	// Violates rule 2: the kind is spelled by hand instead of constructed, so nothing obliges the
	// author to supply the fields the kind requires.
	log.Info(ctx, "request completed", logging.Fields{
		"event_name":       "http_request_completed",
		"http_status_code": 200,
	})

	// Violates rule 1: `event` is not the discriminator. Alloy does not promote it, so the kind
	// ends up in the line body where a query has to parse every line to reach it.
	log.Info(ctx, "call completed", logging.Fields{
		"event": "grpc_request_completed",
	})
}
