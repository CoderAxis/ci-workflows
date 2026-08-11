#!/usr/bin/env python3
"""Assert the retry policy in scripts/lib/http_retry.py, in both directions.

WHY THIS EXISTS
----------------
http_retry.request() is what stands between a single flaky TLS handshake and a whole job
failing - see the module docstring for the 2026-08-11 bump-module-pin incident this replaces a
fix for. A retry helper that retries too much is as dangerous as one that retries too little: it
would turn a real 403 (this job is not authorised) into a slow real 403, wasting the whole
attempt budget re-asking a question whose answer cannot change, while a helper that retries too
little leaves the original bug in place. Both directions are asserted here, case by case, rather
than as a single "it works" smoke test, because a case dropped from a table is easy to miss and a
case dropped from a narrative test is invisible.

No real sockets and no real sleeping: `opener`, `sleep`, `jitter` and `clock` are injected fakes,
so this suite runs in milliseconds and is deterministic - a suite that has to wait out real
backoffs to prove backoffs exist would itself be testing what it exists to avoid.

Run: python3 scripts/test_http_retry.py
"""

from __future__ import annotations

import pathlib
import socket
import ssl
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).parent / "lib"))

import http_retry  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        failures.append(name)


class FakeResponse:
    """Stands in for the `with urlopen(...) as r:` context manager on success."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


def flaky_opener(exceptions_then_success, calls):
    """Raise each exception in order, then return a success response on the call after."""

    def opener(_req, timeout=None):
        calls.append(timeout)
        if len(calls) <= len(exceptions_then_success):
            raise exceptions_then_success[len(calls) - 1]
        return FakeResponse("ok")

    return opener


def always_raise(exc):
    def opener(_req, timeout=None):
        raise exc

    return opener


def no_sleep(recorded):
    def sleep(seconds):
        recorded.append(seconds)

    return sleep


def fixed_jitter(_lo, hi):
    """Deterministic stand-in for random.uniform: always the upper bound."""
    return hi


def fake_clock(values):
    it = iter(values)

    def clock():
        return next(it)

    return clock


def make_url_error(reason: BaseException) -> urllib.error.URLError:
    return urllib.error.URLError(reason)


def make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, None)


# ── retried: genuinely transient failures ──────────────────────────────────────────────────────

def test_transient_timeout_is_retried_and_succeeds() -> None:
    calls: list[float] = []
    sleeps: list[float] = []
    opener = flaky_opener([make_url_error(socket.timeout("timed out"))] * 2, calls)
    result = http_retry.request(
        lambda: "request", description="d", endpoint="e", attempts=4,
        opener=opener, sleep=no_sleep(sleeps), jitter=fixed_jitter,
        clock=fake_clock([0.0, 1.0]),
    )
    with result as value:
        check("a timeout retried twice then succeeded returns the eventual result",
              value == "ok", f"got {value!r}")
    check("the flaky opener was actually called 3 times (2 failures + 1 success)",
          len(calls) == 3, f"calls={calls}")
    check("exactly 2 backoff sleeps happened, one per failed attempt", len(sleeps) == 2,
          f"sleeps={sleeps}")


def test_dns_failure_is_retried() -> None:
    calls: list[float] = []
    opener = flaky_opener([make_url_error(socket.gaierror("Name or service not known"))], calls)
    result = http_retry.request(
        lambda: "request", description="d", endpoint="e", attempts=2,
        opener=opener, sleep=no_sleep([]), jitter=fixed_jitter, clock=fake_clock([0.0, 0.5]),
    )
    with result as value:
        check("a DNS failure (socket.gaierror) is retried and can still succeed", value == "ok")


def test_connection_reset_is_retried() -> None:
    calls: list[float] = []
    opener = flaky_opener([make_url_error(ConnectionResetError())], calls)
    result = http_retry.request(
        lambda: "request", description="d", endpoint="e", attempts=2,
        opener=opener, sleep=no_sleep([]), jitter=fixed_jitter, clock=fake_clock([0.0, 0.5]),
    )
    with result as value:
        check("a connection reset is retried and can still succeed", value == "ok")


def test_ssl_handshake_timeout_is_retried() -> None:
    """The exact shape of the incident: `<urlopen error _ssl.c:983: The handshake operation
    timed out>`, which urlopen reports as an SSLError wrapped in a URLError."""
    calls: list[float] = []
    handshake_timeout = ssl.SSLError("_ssl.c:983: The handshake operation timed out")
    opener = flaky_opener([make_url_error(handshake_timeout)], calls)
    result = http_retry.request(
        lambda: "request", description="d", endpoint="e", attempts=2,
        opener=opener, sleep=no_sleep([]), jitter=fixed_jitter, clock=fake_clock([0.0, 0.5]),
    )
    with result as value:
        check("the incident's exact SSL-handshake-timeout shape is retried and can succeed",
              value == "ok")


def test_5xx_is_retried() -> None:
    calls: list[float] = []
    opener = flaky_opener([make_http_error(503)], calls)
    result = http_retry.request(
        lambda: "request", description="d", endpoint="e", attempts=2,
        opener=opener, sleep=no_sleep([]), jitter=fixed_jitter, clock=fake_clock([0.0, 0.5]),
    )
    with result as value:
        check("a 503 is retried and can still succeed", value == "ok")


# ── never retried: real, deterministic answers ──────────────────────────────────────────────────

def test_403_is_not_retried() -> None:
    calls: list[float] = []

    def opener(_req, timeout=None):
        calls.append(timeout)
        raise make_http_error(403)

    sleeps: list[float] = []
    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep(sleeps), jitter=fixed_jitter,
                           clock=fake_clock([0.0, 0.0]))
        check("a 403 must raise, not return a value", False)
    except urllib.error.HTTPError as e:
        check("a 403 propagates as HTTPError rather than RetryExhausted",
              e.code == 403, f"got {e!r}")
    check("a 403 is never retried: exactly 1 attempt was made", len(calls) == 1,
          f"calls={calls}")
    check("a 403 spends no time in backoff", sleeps == [], f"sleeps={sleeps}")


def test_401_is_not_retried() -> None:
    calls: list[float] = []

    def opener(_req, timeout=None):
        calls.append(timeout)
        raise make_http_error(401)

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep([]), jitter=fixed_jitter,
                           clock=fake_clock([0.0, 0.0]))
        check("a 401 must raise, not return a value", False)
    except urllib.error.HTTPError:
        pass
    check("a 401 is never retried: exactly 1 attempt was made", len(calls) == 1,
          f"calls={calls}")


def test_400_is_not_retried() -> None:
    calls: list[float] = []

    def opener(_req, timeout=None):
        calls.append(timeout)
        raise make_http_error(400)

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep([]), jitter=fixed_jitter,
                           clock=fake_clock([0.0, 0.0]))
        check("a 400 must raise, not return a value", False)
    except urllib.error.HTTPError:
        pass
    check("a 400 (malformed request) is never retried", len(calls) == 1, f"calls={calls}")


def test_404_is_not_retried() -> None:
    """Not called out explicitly in the brief, but the same reasoning applies: a 404 is a
    decisive answer about a specific resource, not a network blip, and only 5xx is retried."""
    calls: list[float] = []

    def opener(_req, timeout=None):
        calls.append(timeout)
        raise make_http_error(404)

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep([]), jitter=fixed_jitter,
                           clock=fake_clock([0.0, 0.0]))
        check("a 404 must raise, not return a value", False)
    except urllib.error.HTTPError:
        pass
    check("a 404 is never retried (only 5xx is)", len(calls) == 1, f"calls={calls}")


def test_cert_verification_failure_is_not_retried() -> None:
    """SSLCertVerificationError is a subclass of SSLError. If the subclass check in
    is_retryable is ever removed or reordered, this is the case that silently starts retrying a
    certificate problem instead of reporting it once."""
    calls: list[float] = []
    cert_error = ssl.SSLCertVerificationError("certificate verify failed")

    def opener(_req, timeout=None):
        calls.append(timeout)
        raise make_url_error(cert_error)

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep([]), jitter=fixed_jitter,
                           clock=fake_clock([0.0, 0.0]))
        check("a certificate verification failure must raise, not return a value", False)
    except urllib.error.URLError:
        pass
    check("a certificate verification failure is never retried", len(calls) == 1,
          f"calls={calls}")


def test_missing_env_var_is_the_callers_problem_not_this_modules() -> None:
    """A KeyError/ValueError raised while BUILDING the request (e.g. a missing
    ACTIONS_ID_TOKEN_REQUEST_TOKEN) must not be swallowed or retried - it is not even an HTTP
    failure, so is_retryable never gets a say."""
    calls: list[int] = []

    def make_request():
        calls.append(1)
        raise KeyError("ACTIONS_ID_TOKEN_REQUEST_TOKEN")

    try:
        http_retry.request(make_request, description="d", endpoint="e", attempts=4,
                           opener=lambda *_a, **_k: FakeResponse("unreachable"),
                           sleep=no_sleep([]), jitter=fixed_jitter, clock=fake_clock([0.0, 0.0]))
        check("a KeyError while building the request must propagate", False)
    except KeyError:
        pass
    check("a missing env var fails on the first attempt, not after a retry budget",
          len(calls) == 1, f"calls={calls}")


# ── exhaustion: the message a human reads at 2am ────────────────────────────────────────────────

def test_exhaustion_message_names_attempts_period_and_endpoint() -> None:
    calls: list[float] = []
    opener = always_raise(make_url_error(socket.timeout("timed out")))

    def counting_opener(_req, timeout=None):
        calls.append(timeout)
        return opener(_req, timeout=timeout)

    try:
        http_retry.request(
            lambda: "request", description="fetching the GitHub OIDC token",
            endpoint="token.actions.githubusercontent.com", attempts=4,
            opener=counting_opener, sleep=no_sleep([]), jitter=fixed_jitter,
            clock=fake_clock([0.0, 41.7]),
        )
        check("exhausting every attempt must raise, not return a value", False)
    except http_retry.RetryExhausted as e:
        msg = str(e)
        check("the message names the attempt count", "4 attempt" in msg, msg)
        check("the message names the elapsed period", "41.7s" in msg, msg)
        check("the message names the endpoint", "token.actions.githubusercontent.com" in msg,
              msg)
        check("the message does not sound like a credentials problem",
              "not a credentials problem" in msg, msg)
    check("exhaustion happens only after using the full attempt budget", len(calls) == 4,
          f"calls={calls}")


def test_a_single_non_transient_failure_never_reaches_exhaustion() -> None:
    """RetryExhausted is a distinct type from HTTPError specifically so a caller's error
    handling can tell "network problem" apart from "real answer" - this pins that they do not
    collapse into one exception type."""
    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=always_raise(make_http_error(403)), sleep=no_sleep([]),
                           jitter=fixed_jitter, clock=fake_clock([0.0, 0.0]))
        check("a 403 must raise", False)
    except http_retry.RetryExhausted:
        check("a 403 must not be reported as RetryExhausted", False)
    except urllib.error.HTTPError:
        check("a 403 raises HTTPError, distinct from RetryExhausted", True)


# ── backoff shape ────────────────────────────────────────────────────────────────────────────────

def test_backoff_delays_are_bounded_and_growing() -> None:
    """Asserts the jitter UPPER bound passed to `jitter(0, hi)` at each attempt - not the
    delay actually used, which fixed_jitter always sets to that bound - so this is really
    checking min(cap, base * 2**n), the computation that bounds the jitter."""
    highs: list[float] = []

    def recording_jitter(lo, hi):
        highs.append(hi)
        return hi

    def opener(_req, timeout=None):
        raise make_url_error(socket.timeout("timed out"))

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=4,
                           opener=opener, sleep=no_sleep([]), jitter=recording_jitter,
                           clock=fake_clock([0.0, 100.0]), backoff_base=1.0, backoff_cap=6.0)
    except http_retry.RetryExhausted:
        pass
    check("one backoff computed per retry (attempts - 1)", len(highs) == 3, f"highs={highs}")
    check("backoff grows: 1s, 2s, 4s before the cap", highs == [1.0, 2.0, 4.0], f"highs={highs}")


def test_backoff_respects_the_cap() -> None:
    highs: list[float] = []

    def recording_jitter(lo, hi):
        highs.append(hi)
        return hi

    try:
        http_retry.request(lambda: "request", description="d", endpoint="e", attempts=6,
                           opener=always_raise(make_url_error(socket.timeout())),
                           sleep=no_sleep([]), jitter=recording_jitter,
                           clock=fake_clock([0.0, 100.0]), backoff_base=1.0, backoff_cap=6.0)
    except http_retry.RetryExhausted:
        pass
    check("backoff is capped rather than growing without bound",
          all(h <= 6.0 for h in highs), f"highs={highs}")
    check("the cap is actually reached, not just never exceeded", 6.0 in highs, f"highs={highs}")


def test_request_factory_is_called_fresh_each_attempt() -> None:
    """A request built once and replayed across attempts is the bug this factory shape exists
    to prevent for a POST carrying a body; pinned here so nobody collapses it back to a single
    shared Request built outside the retry loop."""
    build_count = [0]

    def make_request():
        build_count[0] += 1
        return f"request-{build_count[0]}"

    calls: list[float] = []
    opener = flaky_opener([make_url_error(socket.timeout())] * 2, calls)
    with http_retry.request(make_request, description="d", endpoint="e", attempts=4,
                            opener=opener, sleep=no_sleep([]), jitter=fixed_jitter,
                            clock=fake_clock([0.0, 1.0])):
        pass
    check("the request factory ran once per attempt, not once total", build_count[0] == 3,
          f"build_count={build_count[0]}")


def main() -> int:
    for fn in [
        test_transient_timeout_is_retried_and_succeeds,
        test_dns_failure_is_retried,
        test_connection_reset_is_retried,
        test_ssl_handshake_timeout_is_retried,
        test_5xx_is_retried,
        test_403_is_not_retried,
        test_401_is_not_retried,
        test_400_is_not_retried,
        test_404_is_not_retried,
        test_cert_verification_failure_is_not_retried,
        test_missing_env_var_is_the_callers_problem_not_this_modules,
        test_exhaustion_message_names_attempts_period_and_endpoint,
        test_a_single_non_transient_failure_never_reaches_exhaustion,
        test_backoff_delays_are_bounded_and_growing,
        test_backoff_respects_the_cap,
        test_request_factory_is_called_fresh_each_attempt,
    ]:
        print(f"\n{fn.__name__}")
        fn()

    if failures:
        print(f"\n{len(failures)} case(s) failed: {failures}")
        return 1
    print("\nall http_retry cases hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
