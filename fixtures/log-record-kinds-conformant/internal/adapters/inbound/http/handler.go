// Package http is the positive half of the check-log-record-kinds.py fixture pair.
//
// It is not compiled and it is not imported. It exists so that the checker has a tree it must
// pass, because a detector is only half-specified by the things it rejects: the quickest way to
// silence a false positive is to stop matching, and a suite that only tests violations reports
// green when that happens. The violating sibling proves recall; this proves precision.
//
// Everything here is the shape ADR-0104 asks for. The record kind is a typed constructor from
// platform-shared-go/logging handed to logging.Emit, so the fields the kind requires come from the
// compiler. No `event` key, no hand-written `event_name`, no local access-log middleware.
package http

import (
	"context"
	"time"

	"github.com/coderaxis/platform-shared-go/logging"
)

func handle(ctx context.Context, log logging.Logger, started time.Time) {
	logging.Emit(ctx, log, logging.HTTPRequestCompleted{
		Method:     "GET",
		Route:      "/v1/things/:id",
		Target:     "/v1/things/42",
		StatusCode: 200,
		DurationMS: float64(time.Since(started).Microseconds()) / 1000.0,
		ClientIP:   "203.0.113.7",
		UserAgent:  "fixture/1.0",
	})
}
