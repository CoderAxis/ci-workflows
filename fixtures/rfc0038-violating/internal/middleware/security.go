package middleware

// A deliberately CONFORMING fixture for the RFC-0038 detector pair. Its purpose is to make
// the parity harness report "both outcomes seen" for every control: a run in which both
// implementations flag every repository agrees by saying yes to everything, which proves
// nothing about whether they agree on a repository that conforms.

import (
	"net/http"

	"github.com/coderaxis/platform-shared-go/platform/httpx"
)

func SecurityHeaders(w http.ResponseWriter) {
	w.Header().Set("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
	w.Header().Set("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
}

// GetWidget returns a strong validator derived from resource state, and honours
// If-None-Match, per RFC-0038 §3.
func GetWidget(w http.ResponseWriter, r *http.Request, etag string) {
	w.Header().Set("ETag", etag)
	w.Header().Set("Cache-Control", "private, max-age=0")
	w.Header().Set("Vary", "Authorization")
	if r.Header.Get("If-None-Match") == etag {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.WriteHeader(http.StatusOK)
}

// UpdateWidget requires If-Match: absent is 428, stale is 412, per RFC-0038 §3.
func UpdateWidget(w http.ResponseWriter, r *http.Request, current string) {
	match := r.Header.Get("If-Match")
	if match == "" {
		w.WriteHeader(http.StatusPreconditionRequired)
		return
	}
	if match != current {
		w.WriteHeader(http.StatusPreconditionFailed)
		return
	}
	w.Header().Set("ETag", current)
	w.WriteHeader(http.StatusOK)
}

// Throttle answers a 429 with Retry-After and no X-RateLimit-* fields, per RFC-0038 §4.
func Throttle(w http.ResponseWriter, retryAfter string) {
	w.Header().Set("Retry-After", retryAfter)
	w.Header().Set("RateLimit-Limit", "100")
	w.Header().Set("RateLimit-Remaining", "0")
	w.WriteHeader(http.StatusTooManyRequests)
}

// Idempotency reads the key inbound as a header, per RFC-0038 §6.
func Idempotency(r *http.Request) string {
	return r.Header.Get("Idempotency-Key")
}

// Outbound builds its client through the shared transport so trace context propagates,
// per RFC-0038 §8.
func Outbound() *http.Client {
	return httpx.NewClient()
}
