#!/usr/bin/env python3
"""Precision/recall self-check for the eight RFC-0038 detectors in check-api-contract.py.

    python3 scripts/test_api_contract_controls.py

Every detector below gets at least one fixture that MUST be counted as a violation and one
that MUST NOT, built with tempfile rather than depending on real repo state - a real repo gets
fixed over time, which would silently make a test that points at it vacuous. Several detectors
get an extra fixture that isolates the exact false-positive/false-negative shape this file's
author actually found while writing the detector (a getter returning `*http.Client` mistaken
for a client construction, a switch-case label mistaken for a response write, a query parameter
mistaken for a header), because a test that only tries the obvious case would not have caught
those - and would not catch a regression that reintroduces them.

check-api-contract.py is imported by file path (its own filename cannot be `import`ed directly)
rather than re-implemented here, so this suite tests the actual detectors the CI gate runs, not
a parallel copy of them that could drift.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CHECKER_PATH = HERE / "check-api-contract.py"
CONTROLS_PATH = HERE.parent / "controls" / "api-contract.yaml"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_api_contract", CHECKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses.dataclass() looks the defining module up in sys.modules by __module__; a
    # module loaded by file path that never registers itself there fails at class-definition
    # time with an obscure AttributeError, so this has to happen before exec_module runs.
    sys.modules["check_api_contract"] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_checker()

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def make_repo(tmp, go_files: dict | None = None, spec: dict | None = None,
              config_files: dict | None = None) -> "m.ServiceRepo":
    root = pathlib.Path(tmp)
    for rel, content in {**(go_files or {}), **(config_files or {})}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if spec is not None:
        d = root / "docs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "openapi.json").write_text(json.dumps(spec), encoding="utf-8")
    return m.ServiceRepo(root)


def base_spec(paths: dict) -> dict:
    return {"openapi": "3.0.3", "info": {"title": "fixture", "version": "1.0.0"}, "paths": paths}


def op(responses=None, parameters=None, deprecated=None, request_body=None) -> dict:
    o = {"responses": responses if responses is not None else {"200": {"description": "OK"}}}
    if parameters is not None:
        o["parameters"] = parameters
    if deprecated is not None:
        o["deprecated"] = deprecated
    if request_body is not None:
        o["requestBody"] = request_body
    return o


def header_param(name: str) -> dict:
    return {"in": "header", "name": name, "schema": {"type": "string"}}


def query_param(name: str) -> dict:
    return {"in": "query", "name": name, "schema": {"type": "string"}}


# ── API-0013 protocol_version_pinned ─────────────────────────────────────────────────────────

def test_protocol_version_pinned():
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(tmp, spec=base_spec({}))
        f = m.protocol_version_pinned(repo)
        expect(f.count == 0, f"protocol_version_pinned: 3.0.3 must pass, got count={f.count}")

    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({})
        spec["openapi"] = "3.1.0"
        f = m.protocol_version_pinned(make_repo(tmp, spec=spec))
        expect(f.count == 1, f"protocol_version_pinned: 3.1.0 MUST be a violation, got count={f.count}")
        expect("3.1.0" in f.details[0] and "3.0.3" in f.details[0],
               f"protocol_version_pinned: detail must name found AND expected version: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({})
        del spec["openapi"]
        f = m.protocol_version_pinned(make_repo(tmp, spec=spec))
        expect(f.count == 1, "protocol_version_pinned: a missing `openapi` field MUST be a violation")


# ── API-0011 patch_media_type ────────────────────────────────────────────────────────────────

def test_patch_media_type():
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op(
            request_body={"content": {"application/json": {"schema": {"type": "object"}}}})}})
        f = m.patch_media_type(make_repo(tmp, spec=spec))
        expect(f.count == 1, f"patch_media_type: bare application/json MUST be a violation, got {f.count}")
        expect("bare application/json" in f.details[0], f"unexpected detail: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op()}})  # no requestBody at all
        f = m.patch_media_type(make_repo(tmp, spec=spec))
        expect(f.count == 1, "patch_media_type: an absent request body MUST be a violation")
        expect("no request body" in f.details[0], f"unexpected detail: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op(
            request_body={"content": {"application/merge-patch+json": {"schema": {"type": "object"}}}})}})
        f = m.patch_media_type(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"patch_media_type: application/merge-patch+json MUST NOT be a "
                             f"violation, got {f.count}: {f.details}")

    # Declaring a real patch type ALONGSIDE bare application/json is still prohibited - "MUST
    # NOT contain application/json" is unconditional, not satisfied by also declaring a good one.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op(request_body={"content": {
            "application/json": {"schema": {"type": "object"}},
            "application/merge-patch+json": {"schema": {"type": "object"}},
        }})}})
        f = m.patch_media_type(make_repo(tmp, spec=spec))
        expect(f.count == 1, "patch_media_type: bare application/json alongside a good type is "
                             f"STILL a violation, got count={f.count}")

    # Case-insensitive, and `;charset=` parameters must be stripped before comparing.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op(request_body={"content": {
            "Application/Merge-Patch+JSON; charset=utf-8": {"schema": {"type": "object"}},
        }})}})
        f = m.patch_media_type(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"patch_media_type: a charset parameter and case must not defeat "
                             f"the match, got count={f.count}: {f.details}")


# ── API-0008 conditional_requests ────────────────────────────────────────────────────────────

def test_conditional_requests_reads():
    # MUST count: a single-resource GET with no ETag.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"get": op()}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 1, f"conditional_requests: a bare single-resource GET MUST be a "
                             f"violation, got {f.count}")
        expect(f.details[0].startswith("GET /widgets/{id}"), f"unexpected detail: {f.details}")

    # MUST NOT count: the same GET, but with ETag declared on 200.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"get": op(
            responses={"200": {"description": "OK", "headers": {"ETag": {"schema": {"type": "string"}}}}})}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"conditional_requests: an ETag on 200 MUST clear the violation, "
                             f"got {f.count}: {f.details}")

    # MUST NOT count: a collection path (does not end in a template param) even with an id-like
    # trailing name.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"get": op()}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, "conditional_requests: a collection path must not be flagged")

    # MUST NOT count: health-like segment excludes even a template-terminated path.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/orgs/{orgId}/health/{probeId}": {"get": op()}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"conditional_requests: a 'health' segment MUST exclude the "
                             f"operation even though the path ends in a template param, got "
                             f"{f.count}: {f.details}")

    # MUST NOT count: a declared pagination parameter marks this a collection, not a resource.
    # This is the exact "Cart.Items" false-positive shape the RFC calls out by name.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/orgs/{orgId}/items/{filter}": {"get": op(
            parameters=[query_param("limit")])}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"conditional_requests: a pagination parameter MUST exclude the "
                             f"operation, got {f.count}: {f.details}")

    # MUST NOT count: the 200 body is a collection envelope (`data` is an array).
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/roles/{roleId}/users": {}})
        spec["paths"] = {"/roles/{roleId}": {"get": op(responses={"200": {
            "description": "OK",
            "content": {"application/json": {"schema": {"allOf": [
                {"$ref": "#/components/schemas/common.v1.SuccessResponse"},
                {"type": "object", "properties": {
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/dto.User"}}}},
            ]}}},
        }})}}
        spec["components"] = {"schemas": {
            "common.v1.SuccessResponse": {"type": "object"},
            "dto.User": {"type": "object"},
        }}
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"conditional_requests: a `data` array envelope MUST exclude the "
                             f"operation, got {f.count}: {f.details}")

    # MUST NOT count: deprecated operations.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"get": op(deprecated=True)}})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, "conditional_requests: a deprecated operation must not be flagged")


def test_conditional_requests_writes():
    # The precondition: a write is only counted when the matching GET declares ETag. Zero ETags
    # anywhere today makes part 2 vacuous, so this is the ordering the spec says not to "fix".
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {
            "get": op(),  # no ETag
            "put": op(),  # no If-Match/412/428 either
        }})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        writes = [d for d in f.details if d.startswith("PUT")]
        expect(not writes, f"conditional_requests: a write MUST NOT be counted while its "
                           f"matching GET has no ETag (precondition unmet), got {writes}")

    # Once the GET has an ETag, a write missing If-Match/412/428 MUST be counted.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {
            "get": op(responses={"200": {"description": "OK",
                                         "headers": {"ETag": {"schema": {"type": "string"}}}}}),
            "put": op(),
        }})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        writes = [d for d in f.details if d.startswith("PUT")]
        expect(len(writes) == 1, f"conditional_requests: a write with no precondition support "
                                 f"MUST be counted once the GET has ETag, got {writes}")
        expect("If-Match" in writes[0] and "412" in writes[0] and "428" in writes[0],
               f"conditional_requests: the write detail must name all three absent pieces: {writes}")

    # A write declaring all three MUST NOT be counted.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {
            "get": op(responses={"200": {"description": "OK",
                                         "headers": {"ETag": {"schema": {"type": "string"}}}}}),
            "put": op(parameters=[header_param("If-Match")],
                     responses={"200": {"description": "OK"}, "412": {"description": "Precondition Failed"},
                               "428": {"description": "Precondition Required"}}),
        }})
        f = m.conditional_requests(make_repo(tmp, spec=spec))
        expect(f.count == 0, f"conditional_requests: a fully-compliant write MUST NOT be "
                             f"counted, got {f.count}: {f.details}")


# ── API-0009 cache_key_declared ──────────────────────────────────────────────────────────────

def test_cache_key_declared():
    violating_go = (
        'package handlers\n\n'
        'func Handle(c *Context) {\n'
        '\tc.Header("Cache-Control", "private, max-age=60")\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"handler.go": violating_go}))
        expect(f.count == 1, f"cache_key_declared: `private` with no Vary MUST be a violation - "
                             f"RFC-0038 2 states private excludes shared caches but a browser "
                             f"cache still keys on the URL alone, got {f.count}")
        expect("handler.go:4" in f.details[0], f"unexpected detail: {f.details}")

    clean_go = (
        'package handlers\n\n'
        'func Handle(c *Context) {\n'
        '\tc.Header("Cache-Control", "private, max-age=60")\n'
        '\tc.Header("Vary", "Authorization")\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"handler.go": clean_go}))
        expect(f.count == 0, f"cache_key_declared: Vary in the SAME function MUST clear the "
                             f"violation, got {f.count}: {f.details}")

    # no-store needs no Vary at all.
    no_store_go = (
        'package handlers\n\n'
        'func Handle(c *Context) {\n'
        '\tc.Header("Cache-Control", "no-store")\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"handler.go": no_store_go}))
        expect(f.count == 0, f"cache_key_declared: Cache-Control: no-store MUST NOT require "
                             f"Vary, got {f.count}: {f.details}")

    # A Vary set by a DIFFERENT function (e.g. CORS middleware) must NOT satisfy this handler's
    # requirement - this is the exact chat-service shape RFC-0038 names by name.
    cross_function_go = (
        'package handlers\n\n'
        'func CORSMiddleware(c *Context) {\n'
        '\tc.Header("Vary", "Origin")\n'
        '}\n\n'
        'func Handle(c *Context) {\n'
        '\tc.Header("Cache-Control", "private, max-age=60")\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"handler.go": cross_function_go}))
        expect(f.count == 1, f"cache_key_declared: a Vary set by a DIFFERENT function MUST NOT "
                             f"satisfy the requirement, got {f.count}: {f.details}")

    # A loop that forwards a slice of header names (including the literal "Cache-Control") is
    # NOT a call whose own first argument names the header, and must not be flagged.
    forwarding_loop_go = (
        'package handlers\n\n'
        'func Handle(c *Context, resp *Response) {\n'
        '\tfor _, hdr := range []string{"Content-Type", "Cache-Control"} {\n'
        '\t\tc.Header(hdr, resp.Header.Get(hdr))\n'
        '\t}\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"handler.go": forwarding_loop_go}))
        expect(f.count == 0, f"cache_key_declared: a header-name-forwarding loop MUST NOT be "
                             f"mistaken for a literal Cache-Control call, got {f.count}: {f.details}")

    # RFC-0038 2's second exemption, and the reason it is not optional: a response that is
    # genuinely identical for every caller MAY be marked `public` and MAY omit Vary. The
    # section names a JWKS document, and platform-identity-service serves exactly one --
    # `Cache-Control: public, max-age=300` on public key material that requires no auth.
    # Flagging it asked a caller-independent body to declare a caller-dependent cache key.
    jwks_go = (
        'package handlers\n\n'
        'func JWKS(c *Context) {\n'
        '\tc.Header("Cache-Control", "public, max-age=300")\n'
        '\tc.JSON(200, keys)\n'
        '}\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        f = m.cache_key_declared(make_repo(tmp, go_files={"jwks.go": jwks_go}))
        expect(f.count == 0, f"cache_key_declared: `public` is RFC-0038 2's opt-in for a "
                             f"caller-independent response and MUST NOT require Vary, got "
                             f"{f.count}: {f.details}")


# ── API-0010 standard_ratelimit_fields ───────────────────────────────────────────────────────

def test_standard_ratelimit_fields():
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Limit(c *Context) {\n'
            '\tc.Header("X-RateLimit-Limit", "100")\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 1, f"standard_ratelimit_fields: X-RateLimit-Limit MUST be a "
                             f"violation, got {f.count}")
        expect("X-RateLimit-Limit" in f.details[0], f"unexpected detail: {f.details}")

    # A COMMENT is documentation, never a write. This is not merely a spurious finding: a doc
    # comment sits ABOVE its function, so the enclosing-function lookup attributes the finding to
    # the PREVIOUS function - the report then names code that has nothing to do with it. The
    # fixture below is a developer documenting the correct behaviour and being flagged for the
    # absence of exactly what the comment describes.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Other(w http.ResponseWriter) {\n'
            '\tw.WriteHeader(http.StatusOK)\n'
            '}\n\n'
            '// Throttle answers http.StatusTooManyRequests with Retry-After set.\n'
            'func Throttle(w http.ResponseWriter) {\n'
            '\tw.Header().Set("Retry-After", "30")\n'
            '\tw.WriteHeader(http.StatusTooManyRequests)\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 0, f"standard_ratelimit_fields: a comment mentioning the status is not "
                             f"a write site, got {f.count}: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        go = 'package mw\n\nfunc Limit(c *Context) {\n\tc.Header("X-Request-ID", "abc")\n}\n'
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 0, f"standard_ratelimit_fields: an unrelated header MUST NOT be "
                             f"flagged, got {f.count}: {f.details}")

    # 429 with Retry-After in the same function: clean.
    # Signatures here spell out *gin.Context deliberately. The detector only asks
    # for Retry-After from a function that HAS somewhere to write it, so a fixture
    # abbreviating the handler to `c *Context` would be skipped and would assert
    # nothing - real gin handlers never abbreviate it.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Limit(c *gin.Context) {\n'
            '\tc.Header("Retry-After", "60")\n'
            '\tc.Status(http.StatusTooManyRequests)\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 0, f"standard_ratelimit_fields: 429 WITH Retry-After in the same "
                             f"function MUST NOT be a violation, got {f.count}: {f.details}")

    # 429 with no Retry-After anywhere in the function: violation.
    with tempfile.TemporaryDirectory() as tmp:
        go = 'package mw\n\nfunc Limit(c *gin.Context) {\n\tc.Status(http.StatusTooManyRequests)\n}\n'
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 1, f"standard_ratelimit_fields: 429 with NO Retry-After MUST be a "
                             f"violation, got {f.count}")
        expect("Retry-After" in f.details[0], f"unexpected detail: {f.details}")

    # A gRPC-code-to-HTTP-status translation helper: it takes an error and returns an
    # error, so it has no context and no ResponseWriter. Every BFF carries one. Asking
    # it for Retry-After asks a value constructor to set a header it cannot set.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package clients\n\n'
            'func serviceError(err error, operation string) error {\n'
            '\thttpStatus := 500\n'
            '\tswitch st.Code() {\n'
            '\tcase codes.ResourceExhausted:\n'
            '\t\thttpStatus = http.StatusTooManyRequests\n'
            '\t}\n'
            '\treturn &ServiceError{Status: httpStatus}\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"grpc.go": go}))
        expect(f.count == 0, f"standard_ratelimit_fields: mapping a gRPC code onto an HTTP "
                             f"status is not writing a response, got {f.count}: {f.details}")

    # The other direction, and the reason this gate checks the WRITER rather than
    # excluding the assignment line. Excluding `x = 429` outright would silence this
    # too - and here the 429 really is sent, with no Retry-After alongside it.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Throttle(c *gin.Context) {\n'
            '\tstatus := http.StatusTooManyRequests\n'
            '\tc.JSON(status, gin.H{"error": "slow down"})\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"rl.go": go}))
        expect(f.count == 1, f"standard_ratelimit_fields: a status variable that is then "
                             f"written MUST still be a violation, got {f.count}: {f.details}")

    # Regression: a switch-case label or a bare `return` of the constant is a status-code
    # MAPPING, not a response write, and must not be counted (this is a real false positive
    # this suite's author found and fixed while building the detector).
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package clients\n\n'
            'func httpStatusForGRPCCode(code int) int {\n'
            '\tswitch code {\n'
            '\tcase 8:\n'
            '\t\treturn http.StatusTooManyRequests\n'
            '\tdefault:\n'
            '\t\treturn 500\n'
            '\t}\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"map.go": go}))
        expect(f.count == 0, f"standard_ratelimit_fields: a bare `return "
                             f"http.StatusTooManyRequests` in a code-mapping switch MUST NOT be "
                             f"counted as a write, got {f.count}: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package handlers\n\n'
            'func writeServiceError(c *gin.Context, code int) {\n'
            '\tswitch code {\n'
            '\tcase http.StatusTooManyRequests:\n'
            '\t\twriteError(c, http.StatusTooManyRequests, "rate limited")\n'
            '\t}\n'
            '}\n'
        )
        f = m.standard_ratelimit_fields(make_repo(tmp, go_files={"errors.go": go}))
        expect(f.count == 1, f"standard_ratelimit_fields: the `case` label itself must not be "
                             f"double-counted, but the real write on the next line still must "
                             f"be, expected exactly 1 got {f.count}: {f.details}")


# ── API-0012 idempotency_declared ────────────────────────────────────────────────────────────

def test_idempotency_declared():
    # A repo that never reads the header: pass, regardless of what the spec says.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"post": op()}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("X-Request-ID")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 0, f"idempotency_declared: a repo that never reads Idempotency-Key "
                             f"MUST pass regardless of the spec, got {f.count}")

    # A repo that reads it, with an unsafe op that does NOT declare the header: violation.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"post": op()}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("Idempotency-Key")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 1, f"idempotency_declared: honouring the key with an undeclared "
                             f"unsafe op MUST be a violation, got {f.count}")

    # ... and declaring it as a header parameter clears it.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"post": op(parameters=[header_param("Idempotency-Key")])}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("Idempotency-Key")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 0, f"idempotency_declared: declaring the header parameter MUST clear "
                             f"the violation, got {f.count}: {f.details}")

    # PUT and DELETE are idempotent by definition (RFC 9110 9.2.2), so they are out of scope: an
    # Idempotency-Key there asks for a mechanism to deliver a guarantee the method already gives.
    # 46 of this control's first 93 fleet findings were DELETE, so the distinction is most of it.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"put": op(), "delete": op()}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("Idempotency-Key")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 0, f"idempotency_declared: PUT and DELETE are idempotent by definition "
                             f"and MUST NOT be required to declare Idempotency-Key, got "
                             f"{f.count}: {f.details}")

    # PATCH is in scope precisely because it is NOT idempotent, so the exclusion above must not
    # be read as "only POST".
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets/{id}": {"patch": op()}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("Idempotency-Key")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 1, f"idempotency_declared: PATCH is not idempotent and MUST stay in "
                             f"scope, got {f.count}: {f.details}")

    # Declaring it as a QUERY parameter is NOT the same as declaring the header - RFC-0038 §6
    # says "header parameter" and org-bff's real spec makes exactly this mistake today.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"post": op(parameters=[query_param("Idempotency-Key")])}})
        go = 'package handlers\n\nfunc Handle(c *Context) {\n\t_ = c.GetHeader("Idempotency-Key")\n}\n'
        f = m.idempotency_declared(make_repo(tmp, go_files={"h.go": go}, spec=spec))
        expect(f.count == 1, f"idempotency_declared: a QUERY-declared Idempotency-Key MUST NOT "
                             f"satisfy the header requirement, got {f.count}")

    # The literal spelling requires the hyphen: an unrelated Go identifier/JSON-tag using the
    # same words without it (a domain "idempotency key" field, not the HTTP header) must not
    # make the repo look like it honours the header.
    with tempfile.TemporaryDirectory() as tmp:
        spec = base_spec({"/widgets": {"post": op()}})
        go = ('package outbox\n\ntype Event struct {\n\tIdempotencyKey string `json:"idempotency_key"`\n}\n')
        f = m.idempotency_declared(make_repo(tmp, go_files={"outbox.go": go}, spec=spec))
        expect(f.count == 0, f"idempotency_declared: a domain 'IdempotencyKey' field (no "
                             f"hyphen) MUST NOT be mistaken for the HTTP header, got {f.count}")


# ── API-0014 trace_context_propagated ────────────────────────────────────────────────────────

def test_trace_context_propagated():
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package clients\n\n'
            'func New() *Client {\n'
            '\treturn &Client{http: &http.Client{Timeout: 5 * time.Second}}\n'
            '}\n'
        )
        f = m.trace_context_propagated(make_repo(tmp, go_files={"c.go": go}))
        expect(f.count == 1, f"trace_context_propagated: a bare &http.Client{{}} MUST be a "
                             f"violation, got {f.count}")

    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package clients\n\n'
            'func New() *Client {\n'
            '\treturn &Client{http: &http.Client{Timeout: 5 * time.Second, Transport: httpx.NewTransport(nil)}}\n'
            '}\n'
        )
        f = m.trace_context_propagated(make_repo(tmp, go_files={"c.go": go}))
        expect(f.count == 0, f"trace_context_propagated: Transport wired to httpx.NewTransport "
                             f"in the SAME literal MUST clear the violation, got {f.count}: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        go = 'package clients\n\nfunc New() *Client {\n\treturn &Client{http: httpclient.NewClientSimple(url, 5*time.Second, nil)}\n}\n'
        f = m.trace_context_propagated(make_repo(tmp, go_files={"c.go": go}))
        expect(f.count == 0, f"trace_context_propagated: a client obtained entirely from "
                             f"httpclient.NewClientSimple (no bare literal at all) MUST NOT be "
                             f"flagged, got {f.count}: {f.details}")

    expect(m.DEFAULT_CLIENT_RE.search("resp, err := http.DefaultClient.Do(req)") is not None,
           "trace_context_propagated: http.DefaultClient must be detectable")
    with tempfile.TemporaryDirectory() as tmp:
        go = 'package clients\n\nfunc Fetch(req *http.Request) (*http.Response, error) {\n\treturn http.DefaultClient.Do(req)\n}\n'
        f = m.trace_context_propagated(make_repo(tmp, go_files={"c.go": go}))
        expect(f.count == 1, f"trace_context_propagated: http.DefaultClient.Do MUST be a "
                             f"violation, got {f.count}")

    with tempfile.TemporaryDirectory() as tmp:
        go = 'package clients\n\nfunc Fetch(url string) (*http.Response, error) {\n\treturn http.Get(url)\n}\n'
        f = m.trace_context_propagated(make_repo(tmp, go_files={"c.go": go}))
        expect(f.count == 1, f"trace_context_propagated: a bare http.Get(...) call MUST be a "
                             f"violation, got {f.count}")

    # Regression: `func (c *BaseClient) HttpClient() *http.Client {` is a GETTER whose return
    # type happens to end the signature with `*http.Client {` - the `{` opens the function
    # BODY, not a composite literal. This is a real false positive this suite's author found in
    # inboxxhq-platform-bff's own internal/clients/base.go and fixed; without this regression
    # test a future change to CLIENT_LITERAL_RE could silently reintroduce it.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package clients\n\n'
            'type BaseClient struct {\n\thttpClient *http.Client\n}\n\n'
            'func (c *BaseClient) HttpClient() *http.Client {\n'
            '\treturn c.httpClient\n'
            '}\n'
        )
        f = m.trace_context_propagated(make_repo(tmp, go_files={"base.go": go}))
        expect(f.count == 0, f"trace_context_propagated: a getter returning *http.Client MUST "
                             f"NOT be mistaken for a client construction, got {f.count}: {f.details}")


# ── API-0015 security_response_headers ───────────────────────────────────────────────────────

def test_security_response_headers():
    with tempfile.TemporaryDirectory() as tmp:
        go = 'package mw\n\nfunc Handle(c *Context) {\n\tc.JSON(200, nil)\n}\n'
        f = m.security_response_headers(make_repo(tmp, go_files={"mw.go": go}))
        expect(f.count == 4, f"security_response_headers: a repo setting none of the required "
                             f"headers MUST report all 4 missing, got {f.count}: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Handle(c *Context) {\n'
            '\tc.Header("Strict-Transport-Security", "max-age=63072000")\n'
            '\tc.Header("X-Content-Type-Options", "nosniff")\n'
            '\tc.Header("Referrer-Policy", "no-referrer")\n'
            '\tc.Header("Permissions-Policy", "geolocation=()")\n'
            '\tc.JSON(200, nil)\n'
            '}\n'
        )
        f = m.security_response_headers(make_repo(tmp, go_files={"mw.go": go}))
        expect(f.count == 0, f"security_response_headers: all 4 required headers present (no "
                             f"HTML served) MUST pass, got {f.count}: {f.details}")

    # Serving HTML requires CSP too - a repo with the base 4 but no CSP, that serves HTML, MUST
    # still fail.
    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Handle(c *Context) {\n'
            '\tc.Header("Strict-Transport-Security", "max-age=63072000")\n'
            '\tc.Header("X-Content-Type-Options", "nosniff")\n'
            '\tc.Header("Referrer-Policy", "no-referrer")\n'
            '\tc.Header("Permissions-Policy", "geolocation=()")\n'
            '\tc.Data(200, "text/html; charset=utf-8", body)\n'
            '}\n'
        )
        f = m.security_response_headers(make_repo(tmp, go_files={"mw.go": go}))
        expect(f.count == 1, f"security_response_headers: a repo serving HTML MUST also be "
                             f"required to set Content-Security-Policy, got {f.count}: {f.details}")
        expect("Content-Security-Policy" in f.details[0], f"unexpected detail: {f.details}")

    with tempfile.TemporaryDirectory() as tmp:
        go = (
            'package mw\n\n'
            'func Handle(c *Context) {\n'
            '\tc.Header("Strict-Transport-Security", "max-age=63072000")\n'
            '\tc.Header("X-Content-Type-Options", "nosniff")\n'
            '\tc.Header("Referrer-Policy", "no-referrer")\n'
            '\tc.Header("Permissions-Policy", "geolocation=()")\n'
            '\tc.Header("Content-Security-Policy", "default-src '"'"'self'"'"'")\n'
            '\tc.Data(200, "text/html; charset=utf-8", body)\n'
            '}\n'
        )
        f = m.security_response_headers(make_repo(tmp, go_files={"mw.go": go}))
        expect(f.count == 0, f"security_response_headers: all 5 headers present while serving "
                             f"HTML MUST pass, got {f.count}: {f.details}")

    # A header applied from CONFIGURATION is applied. edge-gateway's middleware loops over a
    # list in config/base/headers.yaml, so a Go-only scan reported four headers absent while
    # every response actually carried them - a false positive that would have had an agent add
    # a redundant hardcoded copy to a repo that already conformed.
    with tempfile.TemporaryDirectory() as tmp:
        go = ('package mw\n\n'
              'func Handle(c *Context) {\n'
              '\tfor _, h := range cfg.Headers.Response.Security {\n'
              '\t\tc.Header(h.Name, h.Value)\n'
              '\t}\n'
              '}\n')
        cfg = ('headers:\n'
               '  response:\n'
               '    security:\n'
               '      - name: "Strict-Transport-Security"\n'
               '        value: "max-age=63072000"\n'
               '      - name: "X-Content-Type-Options"\n'
               '        value: "nosniff"\n'
               '      - name: "Referrer-Policy"\n'
               '        value: "strict-origin-when-cross-origin"\n'
               '      - name: "Permissions-Policy"\n'
               '        value: "geolocation=()"\n')
        f = m.security_response_headers(make_repo(
            tmp, go_files={"mw.go": go}, config_files={"config/base/headers.yaml": cfg}))
        expect(f.count == 0, f"security_response_headers: headers set from a config name/value "
                             f"list MUST count as set, got {f.count}: {f.details}")

    # The inverse, or the fix above would trade a false positive for a false negative: a bare
    # list entry is a REQUEST-header allowlist and sets no response header. edge-gateway's real
    # config names Idempotency-Key and If-Match exactly this way under `forward:`.
    with tempfile.TemporaryDirectory() as tmp:
        go = 'package mw\n\nfunc Handle(c *Context) {\n\tc.JSON(200, nil)\n}\n'
        cfg = ('headers:\n'
               '  forward:\n'
               '    - "Referrer-Policy"\n'
               '    - "Permissions-Policy"\n'
               '    - "Strict-Transport-Security"\n'
               '    - "X-Content-Type-Options"\n')
        f = m.security_response_headers(make_repo(
            tmp, go_files={"mw.go": go}, config_files={"config/base/headers.yaml": cfg}))
        expect(f.count == 4, f"security_response_headers: a bare config list entry is an "
                             f"allowlist, not a response header being set, so all 4 MUST still "
                             f"be reported missing, got {f.count}: {f.details}")

    # A call into the shared platform/ginmiddleware.SecurityHeaders() sets all 4 base headers
    # in one place, so a repo that correctly adopts it - the fix RFC-0038 section 9 asks for -
    # carries none of the literal header strings itself. Without this marker every such repo
    # would report the same four headers "absent" forever, indistinguishable from a repo that
    # set none of them at all.
    with tempfile.TemporaryDirectory() as tmp:
        go = ('package bootstrap\n\n'
              'func startHTTPServer() {\n'
              '\trouter.Use(ginmiddleware.SecurityHeaders())\n'
              '}\n')
        f = m.security_response_headers(make_repo(tmp, go_files={"server.go": go}))
        expect(f.count == 0, f"security_response_headers: a call into the shared "
                             f"ginmiddleware.SecurityHeaders() MUST count as setting all 4 base "
                             f"headers, got {f.count}: {f.details}")

    # The marker covers the 4 base headers only. A repo that serves HTML AND calls the shared
    # middleware bare (no WithHTML/WithContentSecurityPolicy) has not opted into CSP, so it
    # MUST still be asked for it explicitly - the marker is not a blanket pass.
    with tempfile.TemporaryDirectory() as tmp:
        go = ('package bootstrap\n\n'
              'func startHTTPServer() {\n'
              '\trouter.Use(ginmiddleware.SecurityHeaders())\n'
              '\tc.Data(200, "text/html; charset=utf-8", body)\n'
              '}\n')
        f = m.security_response_headers(make_repo(tmp, go_files={"server.go": go}))
        expect(f.count == 1, f"security_response_headers: the shared-middleware marker MUST NOT "
                             f"imply Content-Security-Policy on a repo serving HTML, got "
                             f"{f.count}: {f.details}")
        expect("Content-Security-Policy" in f.details[0], f"unexpected detail: {f.details}")


# ── End-to-end mutation test: the real CLI, not just the detector functions ─────────────────
#
# Every case above calls a detector function directly. This builds ONE synthetic repository
# that violates all eight controls, runs the actual `check-api-contract.py` entry point against
# it via subprocess exactly as CI does, and asserts every one of the eight new control IDs
# appears as a failure - so a wiring mistake (a detector registered under the wrong id, or a
# control whose applies_when silently skips it) fails here even though every unit test above
# could still pass.

def _write_violating_fixture(root: pathlib.Path) -> None:
    spec = base_spec({
        "/widgets/{id}": {
            "get": op(),
            "patch": op(request_body={"content": {"application/json": {"schema": {"type": "object"}}}}),
        },
        "/widgets": {"post": op(parameters=[query_param("Idempotency-Key")])},
    })
    spec["openapi"] = "3.1.0"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "openapi.json").write_text(json.dumps(spec), encoding="utf-8")

    go = (
        'package handlers\n\n'
        'func Handle(c *Context) {\n'
        '\t_ = c.GetHeader("Idempotency-Key")\n'
        '\tc.Header("Cache-Control", "private, max-age=60")\n'
        '\tc.Header("X-RateLimit-Limit", "100")\n'
        '\tclient := &http.Client{Timeout: 5 * time.Second}\n'
        '\t_ = client\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    (root / "internal" / "handlers").mkdir(parents=True, exist_ok=True)
    (root / "internal" / "handlers" / "widgets.go").write_text(go, encoding="utf-8")


def _write_compliant_fixture(root: pathlib.Path) -> None:
    spec = base_spec({
        "/widgets/{id}": {
            "get": op(responses={"200": {"description": "OK",
                                         "headers": {"ETag": {"schema": {"type": "string"}}}}}),
            "patch": op(request_body={"content": {"application/merge-patch+json": {"schema": {"type": "object"}}}},
                       parameters=[header_param("Idempotency-Key"), header_param("If-Match")],
                       responses={"200": {"description": "OK"},
                                 "412": {"description": "Precondition Failed"},
                                 "428": {"description": "Precondition Required"}}),
        },
        "/widgets": {"post": op(parameters=[header_param("Idempotency-Key")])},
    })
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "openapi.json").write_text(json.dumps(spec), encoding="utf-8")

    go = (
        'package handlers\n\n'
        'func Handle(c *Context) {\n'
        '\t_ = c.GetHeader("Idempotency-Key")\n'
        '\tc.Header("Cache-Control", "private, max-age=60")\n'
        '\tc.Header("Vary", "Authorization")\n'
        '\tc.Header("Strict-Transport-Security", "max-age=63072000")\n'
        '\tc.Header("X-Content-Type-Options", "nosniff")\n'
        '\tc.Header("Referrer-Policy", "no-referrer")\n'
        '\tc.Header("Permissions-Policy", "geolocation=()")\n'
        '\tclient := &http.Client{Timeout: 5 * time.Second, Transport: httpx.NewTransport(nil)}\n'
        '\t_ = client\n'
        '\tc.JSON(200, nil)\n'
        '}\n'
    )
    (root / "internal" / "handlers").mkdir(parents=True, exist_ok=True)
    (root / "internal" / "handlers" / "widgets.go").write_text(go, encoding="utf-8")


def test_end_to_end_cli():
    new_controls = ["API-0008", "API-0009", "API-0010", "API-0011", "API-0012", "API-0013",
                    "API-0014", "API-0015"]

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "violating-repo"
        root.mkdir()
        _write_violating_fixture(root)
        proc = subprocess.run(
            [sys.executable, str(CHECKER_PATH), str(root), "--controls", str(CONTROLS_PATH),
             "--fail-on", "major"],
            capture_output=True, text=True,
        )
        expect(proc.returncode != 0, f"end-to-end: the violating fixture MUST fail the gate, "
                                     f"got exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        for cid in new_controls:
            expect(f"[{cid}]" in proc.stdout,
                   f"end-to-end: {cid} did not fire against the violating fixture\n{proc.stdout}")
        expect("unknown detector" not in proc.stdout,
               f"end-to-end: the catalog must validate, got: {proc.stdout}")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "compliant-repo"
        root.mkdir()
        _write_compliant_fixture(root)
        proc = subprocess.run(
            [sys.executable, str(CHECKER_PATH), str(root), "--controls", str(CONTROLS_PATH),
             "--fail-on", "major"],
            capture_output=True, text=True,
        )
        for cid in new_controls:
            expect(f"::error::[{cid}]" not in proc.stdout,
                   f"end-to-end: {cid} fired against the COMPLIANT fixture (false positive)\n{proc.stdout}")


# ── run everything ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()

    if FAILURES:
        print(f"api-contract RFC-0038 controls: FAILED ({len(FAILURES)} assertion(s))")
        for msg in FAILURES:
            print(f"  ::error:: {msg}")
        return 1

    print(f"api-contract RFC-0038 controls: OK ({len(tests)} test function(s), "
         "each carrying at least one MUST-fail and one MUST-pass fixture)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
