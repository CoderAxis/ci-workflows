#!/usr/bin/env python3
"""Bounded retry for the handful of plain HTTP calls the CI credential brokers make.

WHY THIS EXISTS
---------------
`ci-app-token` and `ci-slack-notify` both mint a short-lived credential by making a short chain
of ordinary HTTP calls before they ever touch AWS: fetch an OIDC token from GitHub, then call the
GitHub REST API (installations, access tokens) or Slack's Web API. Every one of those calls used
to be a single `urllib.request.urlopen()` with a flat 30-second timeout and no retry.

On 2026-08-11 a `bump-module-pin` run failed like this:

    Mint module-read token             30s
    Run coderaxis/ci-workflows/ci-app-token@v1
    Mint installation token            30s
    Error: could not obtain an OIDC token: <urlopen error _ssl.c:983: The handshake operation timed out>

Both steps took exactly 30 seconds - the timeout firing, not a slow success - against
`token.actions.githubusercontent.com`, an endpoint with no reason to be slow. One held-open TLS
handshake failed the whole job exactly as if the App key had been refused, at the moment (a
release fan-out that bumps dozens of consumer repositories at once) when the retry that would
have fixed it costs a few seconds and re-running the whole job costs a full CI minute.

WHAT IS RETRIED
---------------
Only failures that say something about the NETWORK, not about the request: connection and TLS
handshake timeouts, DNS resolution failures, connection reset/refused/aborted, and 5xx responses.
None of these say anything about whether the caller is allowed to do what it is asking - they say
the network or the far end had a bad moment, and the very next attempt against the same,
correctly-configured endpoint can plausibly succeed.

WHAT IS NEVER RETRIED
----------------------
Anything that is a real, repeatable answer: 4xx responses (400 means the request itself is
malformed, 401/403 mean the identity or scope is wrong) and a TLS certificate verification
failure (the endpoint is not who it claims to be - retrying only delays reporting that; it will
not start being who it claims to be on attempt two). A caller missing or holding an invalid
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` is likewise not this module's problem to retry: that is checked
by the caller before it ever reaches here, because no number of attempts changes an environment
variable that is simply absent. A blanket except-and-retry would paper over exactly these cases,
spending the whole attempt budget re-asking a question whose answer cannot change while hiding,
behind a generic "still failing", the one detail - a bad scope, a missing permission, a wrong
audience - that would let the caller fix it right now.

BOUNDS
------
4 attempts, 10 seconds each, exponential backoff with FULL jitter between attempts
(`uniform(0, min(cap, base * 2**n))`, base 1s, cap 6s). Worst case is therefore
`4*10 + (1+2+4) = 47s`: a handful of attempts over tens of seconds, comparable to the single
30-second attempt this replaces rather than several times longer, and nowhere near "minutes".

The per-attempt timeout is shorter than the old flat 30s deliberately: none of these endpoints
(GitHub's OIDC issuer, the GitHub REST API, Slack's Web API) ever takes anywhere near 30 seconds
to answer under normal conditions, so waiting out a full 30 seconds before the FIRST retry even
starts is 30 seconds spent confirming what a second attempt would have told us in 10.

Full jitter - not plain exponential backoff - matters specifically because of how this fleet
calls these actions: a release cascade bumps dozens of consumer repositories at once, so a blip
at a shared endpoint (GitHub's OIDC issuer, GitHub's own API) is felt by many callers within the
same few seconds. Retrying on a fixed schedule would make all of them retry in lockstep and turn
one blip into a synchronised retry storm against the endpoint that just had a bad moment; full
jitter spreads those retries out instead.
"""

from __future__ import annotations

import random
import socket
import ssl
import time
import urllib.error
import urllib.request

DEFAULT_ATTEMPTS = 4
DEFAULT_PER_ATTEMPT_TIMEOUT = 10.0
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 6.0

# OSError subclasses that mean "the network had a bad moment", not "the request was wrong".
# socket.timeout is TimeoutError itself from Python 3.10 on, kept as an explicit alias so this
# reads correctly on 3.9 too, where they are distinct types.
_RETRYABLE_OSERROR_TYPES = (
    socket.timeout,
    TimeoutError,
    socket.gaierror,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    BrokenPipeError,
)


class RetryExhausted(Exception):
    """Every attempt failed on a genuinely transient error.

    Carries the endpoint, the attempt count and the elapsed time so the terminal message names
    all three - the next person reading the log should not have to guess whether this was a
    network problem or a credentials problem.
    """

    def __init__(self, description: str, endpoint: str, attempts: int, elapsed: float,
                 last_error: BaseException) -> None:
        self.description = description
        self.endpoint = endpoint
        self.attempts = attempts
        self.elapsed = elapsed
        self.last_error = last_error
        super().__init__(
            f"{description} did not succeed after {attempts} attempt(s) over {elapsed:.1f}s "
            f"against {endpoint}. This looks like a network problem, not a credentials problem: "
            f"the last attempt failed with: {last_error}"
        )


def is_retryable(exc: BaseException) -> bool:
    """Transient network failure -> True. A real, deterministic answer -> False.

    `SSLCertVerificationError` is checked before the general `ssl.SSLError` case because it is a
    subclass of it: a certificate the endpoint cannot prove is not fixed by asking again, so
    without this ordering a bad cert would be retried into a slower version of the same failure
    instead of being reported once, immediately.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (ssl.SSLError,) + _RETRYABLE_OSERROR_TYPES):
        return True
    if isinstance(exc, urllib.error.URLError):
        # urlopen wraps the OSError/ssl.SSLError it caught in `.reason`. A string reason (e.g.
        # "unknown url type") is a caller mistake, not a network blip, so it is not retried.
        return isinstance(exc.reason, BaseException) and is_retryable(exc.reason)
    return False


def request(
    make_request,
    *,
    description: str,
    endpoint: str,
    attempts: int = DEFAULT_ATTEMPTS,
    per_attempt_timeout: float = DEFAULT_PER_ATTEMPT_TIMEOUT,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_cap: float = DEFAULT_BACKOFF_CAP,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
    jitter=random.uniform,
    clock=time.monotonic,
):
    """Perform one HTTP request, retrying only genuinely transient failures.

    `make_request` is a zero-argument callable that returns a fresh `urllib.request.Request` on
    every call - a factory, not a single shared instance, so a request carrying a body is never
    replayed from a state a previous attempt already consumed.

    On success, returns whatever `opener(...)` returns (an `http.client.HTTPResponse` context
    manager for the real `urllib.request.urlopen`); the caller reads and closes it as usual.

    A non-transient failure (`is_retryable` says no) propagates immediately on the first attempt
    - it is a decisive answer, not a blip, so turning it into a slow decisive answer would be a
    regression, not a fix. A transient failure is retried until `attempts` is exhausted, at which
    point this raises `RetryExhausted` naming the endpoint, the attempt count and the elapsed
    time.

    `opener`, `sleep`, `jitter` and `clock` are injectable so tests can exercise the retry and
    backoff logic deterministically, without real sockets or real waiting.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_exc: BaseException | None = None
    start = clock()
    for attempt in range(1, attempts + 1):
        try:
            return opener(make_request(), timeout=per_attempt_timeout)
        except Exception as exc:  # noqa: BLE001 - is_retryable makes the real decision below
            if not is_retryable(exc):
                raise
            last_exc = exc
            if attempt == attempts:
                break
            delay = jitter(0, min(backoff_cap, backoff_base * (2 ** (attempt - 1))))
            sleep(delay)
    raise RetryExhausted(description, endpoint, attempts, clock() - start, last_exc)
