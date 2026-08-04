#!/usr/bin/env python3
"""Enforce controls/api-contract.yaml against a service repository.

Answers the question no existing checker asks: does this service's HTTP API actually
obey the platform's contract decisions, or does it merely have a spec file? The central
openapi-contract workflow already proves a spec exists, lints, and that the repo's own
contract tests pass. None of that stops a service from shipping a Swagger UI, inventing
its own response envelope, hand-rolling a copy of the shared conformance suite, or
committing four rival spec files - all of which the fleet had done.

    ./scripts/check-api-contract.py                  # check the repo in $PWD
    ./scripts/check-api-contract.py path/to/repo …   # check specific service roots
    ./scripts/check-api-contract.py --format json    # machine-readable report

RATCHET. Migrating 36 services is a program of work, not a PR, so a gate that fails on
the whole backlog on day one would simply be switched off. Each repo may commit an
`.api-contract-baseline.json` freezing its CURRENT violation count per control. The gate
then fails only when a count RISES. A repo with no baseline is held to zero, so a service
created tomorrow cannot introduce any of this, and a service with debt can only shrink it.
The baseline is a small, reviewable, CODEOWNERS-guardable diff - raising it is a visible
act, not an accident.

Exit 0 when every control is upheld at or above --fail-on, 1 on violations, 2 on a bad catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment problem, not a policy failure
    raise SystemExit("::error::PyYAML is required: python3 -m pip install pyyaml")

SELF_REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLS = SELF_REPO / "controls" / "api-contract.yaml"
BASELINE_FILE = ".api-contract-baseline.json"
# Byte-identical to baselineComment in the ihq CLI's internal/cli/baseline.go. Both tools write
# this key, so a difference would make the file flip between two texts as each ran, turning
# every alternate run into a spurious diff on a line neither tool actually disagreed about.
BASELINE_COMMENT = ("Frozen api-contract debt for this service. The gate fails if any count "
                    "RISES; lower it by fixing violations, then re-run --write-baseline. "
                    "Raising a number here is a reviewable, deliberate act.")

# The proto projection API-0007 compares against. Generated, never hand-written:
#   go run ./platform/openapicontract/commonv1policy/cmd/emit-canonical-components
# in platform-shared-go, redirected here. Its `source.version` records the
# platform-contracts-go release it was projected from.
CANONICAL_COMPONENTS = SELF_REPO / "controls" / "common-v1-components.json"
_canonical_cache = None


def load_canonical_components():
    """Return (components, source_version), or (None, None) when unavailable.

    Callers must treat unavailable as a configuration error, not a skip - main() checks
    this up front and exits 2. The artifact ships in this repository beside this script,
    so its absence means a broken checkout (a sparse-checkout that omitted controls/, say),
    and a version gate that reports a pass because it could not find its reference is worse
    than no gate at all.
    """
    global _canonical_cache
    if _canonical_cache is None:
        try:
            doc = json.loads(CANONICAL_COMPONENTS.read_text(encoding="utf-8"))
            _canonical_cache = (doc.get("components") or {},
                                (doc.get("source") or {}).get("version", "unknown"))
        except (OSError, json.JSONDecodeError):
            _canonical_cache = (None, None)
    return _canonical_cache

SEVERITY_ORDER = {"critical": 3, "major": 2, "minor": 1}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"active", "deprecated", "superseded"}
VALID_SCOPE = {"service", "spec", "source"}
VALID_APPLIES = {"always", "http-api"}
REQUIRED_FIELDS = ("id", "title", "owner", "scope", "status", "severity", "applies_when",
                   "policy", "rationale", "remediation", "detector", "refs")
MAX_DETAILS = 25

DOCS_BEGIN = "<!-- BEGIN api-contract-controls (generated: scripts/check-api-contract.py --write-docs) -->"
DOCS_END = "<!-- END api-contract-controls -->"

# The canonical envelope components, owned by proto/common/v1 (CONTRACT_AUTHORITY_MATRIX row 2).
CANONICAL_SUCCESS = "common.v1.SuccessResponse"
CANONICAL_ERROR = "common.v1.ErrorResponse"

# RFC-0038 section 1: the platform's single pinned OpenAPI dialect. This MUST stay a single
# named constant - never inlined into a comparison - because the RFC says the pin moves as one
# deliberate edit to this value (and, in lockstep, to the Go CLI's twin of it).
PINNED_OPENAPI_VERSION = "3.0.3"

# Spec files a service may legitimately commit. Anything else under docs/ matching
# openapi*.json is a rival spec - the exact hygiene failure that left auth carrying a
# 307KB "premigration" spec advertising 58 paths against a live 49.
ALLOWED_SPEC_FILES = {
    "openapi.json",                      # the generated contract
    "openapi.base.json",                 # authored, non-derivable metadata only
    "openapi.operationids.lock.json",    # semver-governed operationId registry
    "openapi.operationids.schema.json",  # schema for the lock
}

# Runtime documentation surfaces, forbidden by ADR-0067.
RUNTIME_DOCS_MARKERS = (
    (re.compile(r"platform/swaggerpolicy"), "imports swaggerpolicy (runtime docs gating)"),
    (re.compile(r"swaggo/gin-swagger|ginSwagger"), "imports gin-swagger (Swagger UI handler)"),
    (re.compile(r"openapiroutes\.Register"), "registers openapiroutes spec endpoints"),
    (re.compile(r"^//go:build .*\bswagger\b", re.M), "carries a `swagger` build tag"),
    (re.compile(r"\"/swagger(/|\")"), "registers a /swagger route"),
    (re.compile(r"\"/api/docs(-json)?\""), "registers an /api/docs route"),
)

# A hand-rolled copy of the shared conformance suite: driving kin-openapi directly.
KIN_OPENAPI_MARKERS = re.compile(r"gorillamux\.NewRouter|openapi3filter\.ValidateResponse")
SHARED_CONFORMANCE_IMPORT = "openapicontract/conformance"

# --- RFC-0038: HTTP protocol semantics (API-0008..API-0015) --------------------------------
#
# Detectors for the eight controls added by RFC-0038. The specification for exactly what each
# one detects - including its exclusions and the counts measured at adoption - lives in
# RFC0038_CONTROL_SPEC.md, which is also the input to an independent Go implementation in the
# `ihq` CLI; the two are REQUIRED to agree on the same fixture, so a change to a rule here
# should be mirrored there, not diverged from silently.

PATCH_MEDIA_TYPES = {"application/merge-patch+json", "application/json-patch+json"}

# API-0008: a path identifies a single resource, structurally, when its final non-empty segment
# is a template parameter. Segments below mark infrastructure rather than a resource even when
# they happen to sit after one (e.g. a health probe nested under a resource path).
NON_RESOURCE_PATH_SEGMENTS = {
    "health", "healthz", "readyz", "livez", "metrics", "ping", "version", ".well-known",
}
PAGINATION_PARAM_NAMES = {"page", "pagesize", "limit", "offset", "cursor"}
SINGLE_RESOURCE_PATH_RE = re.compile(r"/\{[^/{}]+\}/?$")

# API-0009 / API-0010: function-scoping is approximated, per the spec, by scanning from the
# preceding top-level `func` line to the next one. This over-includes a closure nested inside
# the enclosing function - the common shape for Gin middleware - rather than under-including it,
# so a Vary or Retry-After set from the inner closure is still found.
FUNC_LINE_RE = re.compile(r"^func\b")

# A literal `.Header(`, `.Set(` or `.Add(` call naming a header as its own first argument.
# Deliberately narrower than "the header name appears on this line": a loop that forwards a
# slice of header names (`for _, h := range []string{"Cache-Control", ...}`) must NOT match,
# because the header name is not the literal argument of the call that sets it.
CACHE_CONTROL_SITE_RE = re.compile(r'\.(?:Header|Set|Add)\(\s*"Cache-Control"\s*,\s*(.*)\)',
                                   re.IGNORECASE)
VARY_SET_RE = re.compile(r'\.(?:Header|Set|Add)\(\s*"Vary"', re.IGNORECASE)
XRATELIMIT_SITE_RE = re.compile(r'\.(?:Header|Set|Add)\(\s*"(X-RateLimit-[A-Za-z0-9-]*)"',
                                re.IGNORECASE)
STATUS_429_RE = re.compile(r"\bStatusTooManyRequests\b")
RETRY_AFTER_SET_RE = re.compile(r'\.(?:Header|Set|Add)\(\s*"Retry-After"', re.IGNORECASE)
# The two ways a Go function can put bytes on an HTTP response. A function with
# neither is CLASSIFYING a 429, not sending one - every BFF's gRPC client has a
# `case codes.ResourceExhausted: httpStatus = 429` translation helper that takes
# an error and returns an error, and demanding Retry-After from it asks a value
# constructor to set a header it has no writer for. Checking for the writer rather
# than excluding the assignment line keeps the genuine
# `status := 429; c.JSON(status, ...)` pattern in scope.
RESPONSE_WRITER_RE = re.compile(r"gin\.Context|http\.ResponseWriter|\bResponseWriter\b")

# API-0012: the literal header spelling REQUIRES the hyphen, which is what keeps this from
# matching the unrelated Go identifier `IdempotencyKey` or the JSON tag `idempotency_key` that
# several services use for a same-named-but-different domain concept (an outbox/ledger key).
# PUT and DELETE are idempotent by definition (RFC 9110 9.2.2), so they are deliberately absent:
# an Idempotency-Key there duplicates a guarantee the method already carries.
NON_IDEMPOTENT_METHODS = ("post", "patch")

IDEMPOTENCY_TEXT_RE = re.compile(r"idempotency-key", re.IGNORECASE)
HEADER_READ_CALL_RE = re.compile(r"\bGetHeader\(|\.Header\.Get\(|Request\.Header\.Get\(",
                                 re.IGNORECASE)

# The Idempotency-Key header being READ, with the name inside the read call. Kept separate
# from HEADER_READ_CALL_RE because pairing that one with a mention anywhere in the file
# cannot tell an inbound read from an outbound Header.Set of the same name.
IDEMPOTENCY_HEADER_READ_RE = re.compile(
    r"(?:GetHeader|\.Header\.Get)\(\s*[\"']idempotency-key[\"']\s*\)", re.IGNORECASE)

# API-0014: the two shared packages a client may be built from or wired to. Referencing either
# one anywhere inside a bare `&http.Client{...}` literal (e.g. `Transport: httpx.NewTransport
# (nil)`) is the permitted third case; a client obtained entirely from `httpclient.NewClient(...)`
# never matches CLIENT_LITERAL_RE in the first place, so it needs no special case here.
CLIENT_LITERAL_RE = re.compile(r"&?\bhttp\.Client\s*\{")
DEFAULT_CLIENT_RE = re.compile(r"\bhttp\.DefaultClient\b")
BARE_OUTBOUND_CALL_RE = re.compile(r"\bhttp\.(Get|Post|Head|PostForm)\(")
SHARED_TRANSPORT_MARKER_RE = re.compile(r"\bhttpx\.NewTransport\(|\bhttpclient\.")

# API-0015. Mirrors SHARED_TRANSPORT_MARKER_RE above: a call into the shared
# platform/ginmiddleware.SecurityHeaders() middleware sets Strict-Transport-Security,
# X-Content-Type-Options, Referrer-Policy and Permissions-Policy in one place, so a
# consuming repo carries none of those literal header strings itself. Without this marker
# every repo that correctly adopts the shared middleware - the fix RFC-0038 section 9 asks
# for - reports the same four headers "absent" forever, which is indistinguishable from a
# repo that set none of them at all. It does not cover Content-Security-Policy: that header
# is opt-in per RFC-0038 section 9 (WithHTML/WithContentSecurityPolicy), so a bare
# SecurityHeaders() call is not evidence a caller made that choice.
SECURITY_HEADERS_MARKER_RE = re.compile(r"\bginmiddleware\.SecurityHeaders\(")

# API-0015, the other half of the marker above. A BARE SecurityHeaders() is not evidence of a
# CSP, but passing WithHTML() or WithContentSecurityPolicy() is exactly the caller making that
# choice, and the shared middleware then sets the header on every response. Without this,
# adopting the option RFC-0038 section 9 asks for still reports Content-Security-Policy
# "absent", and the only way to satisfy the checker is a second middleware that re-sets a
# header already set - which is what notification-service and communication-service each grew,
# both carrying a comment saying it exists for this scan. A control that can only be satisfied
# by redundant code is measuring the code's shape, not the response's.
CSP_OPT_IN_MARKER_RE = re.compile(
    r"\bginmiddleware\.With(?:HTML|ContentSecurityPolicy)\(")

# API-0015.
SECURITY_HEADERS_REQUIRED = ("Strict-Transport-Security", "X-Content-Type-Options",
                             "Referrer-Policy", "Permissions-Policy")
HTML_CONTENT_TYPE_RE = re.compile(r"text/html", re.IGNORECASE)
HTML_TEMPLATE_IMPORT_RE = re.compile(r'"html/template"')


def _enclosing_function_span(lines: list, idx: int) -> tuple:
    """(start, end) line indices approximating the function enclosing lines[idx].

    See FUNC_LINE_RE above for why the approximation is scoped to the nearest top-level `func`
    lines rather than to the innermost brace-balanced block.
    """
    start = 0
    for i in range(idx, -1, -1):
        if FUNC_LINE_RE.match(lines[i]):
            start = i
            break
    end = len(lines)
    for i in range(idx + 1, len(lines)):
        if FUNC_LINE_RE.match(lines[i]):
            end = i
            break
    return start, end


def _extract_balanced(text: str, open_idx: int) -> str:
    """text[open_idx:...] from a '{' at open_idx to its matching '}', inclusive."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return text[open_idx:]


def _line_no(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _is_type_reference_not_literal(text: str, match_start: int) -> bool:
    """True when a CLIENT_LITERAL_RE match is actually `) *http.Client {`: a function
    signature's `*http.Client` return type immediately followed by its body's opening brace,
    not a composite literal. `HttpClient() *http.Client {` is real code in this fleet (a getter
    exposing the underlying client), and without this check every such getter would be counted
    as an uninstrumented client construction it never performs.
    """
    prefix = re.sub(r"[\s*]+$", "", text[:match_start])
    return prefix.endswith(")")


def _is_single_resource_path(path: str) -> bool:
    return bool(SINGLE_RESOURCE_PATH_RE.search(path))


def _path_has_non_resource_segment(path: str) -> bool:
    return any(seg in NON_RESOURCE_PATH_SEGMENTS for seg in path.split("/") if seg)


def _op_declares_pagination_param(op: dict) -> bool:
    for p in op.get("parameters") or []:
        if isinstance(p, dict) and str(p.get("name", "")).strip().lower() in PAGINATION_PARAM_NAMES:
            return True
    return False


def _resolve_schema_ref(schema, components: dict, seen: frozenset = frozenset()):
    """Follow a single `$ref` into components.schemas, one level of cycle-guard included."""
    if not isinstance(schema, dict) or "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        return schema
    name = ref[len(prefix):]
    if name in seen:
        return schema
    target = components.get(name)
    if not isinstance(target, dict):
        return schema
    return _resolve_schema_ref(target, components, seen | {name})


def _merge_allof(schema, components: dict) -> dict:
    """Best-effort flattening of an `allOf` into one dict with a combined `properties`.

    Good enough to find the `data` property the proto envelope wraps a payload in - which is
    all API-0008's collection check needs - without a general JSON-Schema merge.
    """
    schema = _resolve_schema_ref(schema, components)
    if not isinstance(schema, dict):
        return {}
    members = schema.get("allOf")
    if not isinstance(members, list):
        return schema
    merged: dict = {"properties": {}}
    for member in members:
        sub = _merge_allof(member, components)
        if not isinstance(sub, dict):
            continue
        if sub.get("type"):
            merged["type"] = sub["type"]
        props = sub.get("properties")
        if isinstance(props, dict):
            merged["properties"].update(props)
    return merged


def _response_is_collection(op: dict, status: str, components: dict) -> bool:
    """True when the STATUS response body is an array, or a `data` envelope around one.

    Only proven when the schema says so; anything unresolvable (no schema, a non-JSON media
    type, an unresolvable $ref) is treated as NOT a collection rather than guessed - the
    structural path check already established this is a candidate single resource, and RFC-0038
    §Machine verification's instruction is to skip what cannot be evaluated soundly, not to
    widen an exclusion on a guess.
    """
    resp = (op.get("responses") or {}).get(status)
    if not isinstance(resp, dict):
        return False
    schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
    if not isinstance(schema, dict):
        return False
    merged = _merge_allof(schema, components)
    if merged.get("type") == "array":
        return True
    data_schema = (merged.get("properties") or {}).get("data")
    resolved_data = _resolve_schema_ref(data_schema, components) if isinstance(data_schema, dict) else None
    return isinstance(resolved_data, dict) and resolved_data.get("type") == "array"


def _response_declares_header(op: dict, status: str, header_name: str) -> bool:
    resp = (op.get("responses") or {}).get(status)
    if not isinstance(resp, dict):
        return False
    headers = resp.get("headers")
    if not isinstance(headers, dict):
        return False
    return any(str(h).lower() == header_name.lower() for h in headers)


def _op_declares_header_param(op: dict, header_name: str) -> bool:
    for p in op.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        if (str(p.get("in", "")).lower() == "header"
                and str(p.get("name", "")).lower() == header_name.lower()):
            return True
    return False


def conditional_requests(repo: ServiceRepo) -> Finding:
    """API-0008: single-resource reads carry ETag; matching writes honour If-Match/412/428."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    components = ((repo.spec or {}).get("components") or {}).get("schemas") or {}
    ops = repo.operations()

    read_etag_paths = set()
    violations = []

    for path, method, op in ops:
        if method != "get" or not _is_single_resource_path(path):
            continue
        if _path_has_non_resource_segment(path):
            continue
        if op.get("deprecated") is True:
            continue
        if _op_declares_pagination_param(op):
            continue
        resp200 = (op.get("responses") or {}).get("200")
        if not isinstance(resp200, dict):
            continue  # nothing documented to carry an ETag; not this control's failure to report
        if _response_is_collection(op, "200", components):
            continue
        if _response_declares_header(op, "200", "ETag"):
            read_etag_paths.add(path)
            continue
        violations.append(f"GET {path}: single-resource read has no ETag on its 200 response")

    for path, method, op in ops:
        if method not in ("put", "patch", "delete") or not _is_single_resource_path(path):
            continue
        if path not in read_etag_paths:
            continue  # precondition unmet: the matching GET has no ETag yet (RFC-0038 §3 ordering)
        missing = []
        if not _op_declares_header_param(op, "If-Match"):
            missing.append("If-Match header parameter")
        if "412" not in (op.get("responses") or {}):
            missing.append("412 response")
        if "428" not in (op.get("responses") or {}):
            missing.append("428 response")
        if missing:
            violations.append(f"{method.upper()} {path}: missing {', '.join(missing)} "
                              "(the matching GET declares ETag)")

    violations = sorted(set(violations))
    reads = sum(1 for v in violations if v.startswith("GET "))
    writes = len(violations) - reads
    if violations:
        return Finding(False, f"{reads} read(s) missing ETag, {writes} write(s) missing a "
                              "precondition", _capped(violations), len(violations))
    return Finding(True, "every single-resource read carries ETag and every matching write "
                         "honours If-Match/412/428", count=0)


def cache_key_declared(repo: ServiceRepo) -> Finding:
    """API-0009: a storable Cache-Control must be paired with Vary in the same function."""
    violations = []
    for rel, text in repo.runtime_sources():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = CACHE_CONTROL_SITE_RE.search(line)
            if not m:
                continue
            value = m.group(1).strip().lower()
            if "no-store" in value:
                continue  # a response that may not be stored has no cache key to declare
            if "public" in value:
                # RFC-0038 §2's second exemption: a response that is genuinely identical for
                # every caller -- the section names a JWKS document -- MAY be marked `public`
                # and MAY omit Vary, because there is no caller-dependent input to key on.
                #
                # `public` is the marker the section chose for that opt-in, and §2 states in
                # terms that whether a route is authenticated cannot be determined at the line
                # that sets the header, so policing `public` on a caller-dependent response is
                # named there as a code-review obligation rather than a mechanical one. Flagging
                # it here would contradict the rule this control implements.
                continue
            start, end = _enclosing_function_span(lines, i)
            func_text = "\n".join(lines[start:end])
            if VARY_SET_RE.search(func_text):
                continue
            violations.append(f"{rel}:{i + 1}: sets Cache-Control ({value}) with no Vary in the "
                              "enclosing function (scanned from the preceding top-level `func` "
                              "line to the next one)")
    violations = sorted(set(violations))
    note = (" A Vary set by a DIFFERENT function (e.g. CORS middleware's `Vary: Origin`) does "
           "not satisfy this. `no-store` and `public` are exempt per RFC-0038 §2. The §2 "
           "refinement - a Vary present but naming neither Authorization nor an organisation "
           "header on an authenticated route - is NOT applied, and nor is the prohibition on "
           "`public` for a caller-dependent body: both need to know whether a route is "
           "authenticated, which is not determinable at the line that sets the header. §2 "
           "assigns those two to code review by name; this checker does not guess.")
    if violations:
        return Finding(False, f"{len(violations)} Cache-Control site(s) with no cache key.{note}",
                       _capped(violations), len(violations))
    return Finding(True, f"every storable Cache-Control site declares Vary in its own function.{note}",
                   count=0)


def _is_429_write_site(line: str) -> bool:
    """False for a line that merely mentions StatusTooManyRequests without writing a response.

    Two shapes appear in this fleet and neither is a write: a gRPC-code-to-HTTP-status mapping
    table that `return`s the constant from a switch (never itself touches a response writer),
    and the `case http.StatusTooManyRequests:` label that dispatches to the line that actually
    writes it. Both would otherwise be double- or falsely-counted as "writes 429 with no
    Retry-After" for a status value that is only ever being translated or matched, not written.
    """
    stripped = line.strip()
    # A comment is documentation, never a write, and getting this wrong is worse than one
    # spurious finding: a doc comment sits ABOVE its function, so the enclosing-function lookup
    # attributes the finding to the PREVIOUS function and the report names code that has nothing
    # to do with it.
    if stripped.startswith(("//", "*", "/*")):
        return False
    if re.match(r"^case\s+http\.StatusTooManyRequests\s*:$", stripped):
        return False
    if re.match(r"^return\s+http\.StatusTooManyRequests\s*$", stripped):
        return False
    if re.search(r"(==|!=)\s*http\.StatusTooManyRequests\b", stripped):
        return False
    if re.search(r"http\.StatusTooManyRequests\s*(==|!=)", stripped):
        return False
    return True


def standard_ratelimit_fields(repo: ServiceRepo) -> Finding:
    """API-0010: no X-RateLimit-*, and every 429 carries Retry-After from the same function."""
    violations = []
    for rel, text in repo.runtime_sources():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = XRATELIMIT_SITE_RE.search(line)
            if m:
                violations.append(f"{rel}:{i + 1}: sets prohibited {m.group(1)} response header")
        for i, line in enumerate(lines):
            if not STATUS_429_RE.search(line) or not _is_429_write_site(line):
                continue
            start, end = _enclosing_function_span(lines, i)
            func_text = "\n".join(lines[start:end])
            if not RESPONSE_WRITER_RE.search(func_text):
                continue
            if RETRY_AFTER_SET_RE.search(func_text):
                continue
            violations.append(f"{rel}:{i + 1}: writes 429 Too Many Requests with no Retry-After "
                              "in the enclosing function")
    violations = sorted(set(violations))
    xrl = sum(1 for v in violations if "X-RateLimit" in v)
    if violations:
        return Finding(False, f"{xrl} X-RateLimit-* site(s), {len(violations) - xrl} 429 "
                              "response(s) with no Retry-After", _capped(violations), len(violations))
    return Finding(True, "no X-RateLimit-* headers, and every 429 carries Retry-After", count=0)


def patch_media_type(repo: ServiceRepo) -> Finding:
    """API-0011: PATCH declares a real patch media type, never bare application/json."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    violations = []
    for path, method, op in repo.operations():
        if method != "patch":
            continue
        rb = op.get("requestBody")
        content = rb.get("content") if isinstance(rb, dict) else None
        if not isinstance(content, dict) or not content:
            violations.append(f"PATCH {path}: no request body media type declared")
            continue
        media_types = {str(mt).split(";", 1)[0].strip().lower() for mt in content}
        if "application/json" in media_types:
            violations.append(f"PATCH {path}: declares bare application/json")
        elif not (media_types & PATCH_MEDIA_TYPES):
            violations.append(f"PATCH {path}: declares neither application/merge-patch+json nor "
                              "application/json-patch+json")
    violations = sorted(set(violations))
    if violations:
        return Finding(False, f"{len(violations)} PATCH operation(s) with an unclear patch "
                              "media type", _capped(violations), len(violations))
    return Finding(True, "every PATCH operation declares a real patch media type", count=0)


def _repo_honours_idempotency_key(repo: ServiceRepo) -> bool:
    """True only where the repo READS the header INBOUND.

    The name must appear inside the read call, not merely somewhere in the same file.
    Correlating "this file mentions Idempotency-Key" with "this file reads some header"
    conflates the two opposite directions, and it produced a real false positive:
    stripe-adapter-service's Stripe client SETS Idempotency-Key on its OUTBOUND request
    (it is Stripe's client) and, thirty lines later, reads a DIFFERENT header --
    `resp.Header.Get("Request-Id")` -- off the response. Two unrelated facts in one file
    were read as "this service honours Idempotency-Key from its callers", and six of its
    operations were then required to declare a header nothing in the repo ever reads.

    Sending a header is not honouring it. Under RFC-0038 §6 the rule is conditional on a
    handler honouring the key, so a repo that only forwards one is out of scope entirely.
    """
    for rel, text in repo.runtime_sources():
        if IDEMPOTENCY_HEADER_READ_RE.search(text):
            return True
        # Middleware is the one place the read may be indirected through a named constant
        # rather than a literal, so a mention plus a header read is accepted there.
        if ("middleware" in str(rel).lower()
                and IDEMPOTENCY_TEXT_RE.search(text)
                and HEADER_READ_CALL_RE.search(text)):
            return True
    return False


def idempotency_declared(repo: ServiceRepo) -> Finding:
    """API-0012: an operation whose handler reads Idempotency-Key must declare it (service scope).

    Correlating a specific handler to a specific operation is not reliable by static analysis
    (see RFC0038_CONTROL_SPEC.md), so this is a REPO-LEVEL approximation: if ANY runtime source
    reads the header, every operation in scope is expected to declare it. This over-counts
    relative to a perfect mapping, which is acceptable only because the control ratchets -
    stated in the evidence string so a reader of a CI log is not misled into thinking every
    counted operation was proven to read the key.

    Scope is POST and PATCH, not every unsafe method. PUT and DELETE are idempotent by
    definition (RFC 9110 9.2.2), so an idempotency key on them asks for a mechanism to deliver a
    guarantee the method already gives, and 46 of this control's first 93 fleet findings were
    DELETE. RFC-0038 6's rationale is that "a caller reading the published contract cannot
    discover a safe-retry mechanism the server already implements" - for PUT and DELETE the
    method IS that mechanism, so there is nothing undiscoverable to declare.
    """
    if not _repo_honours_idempotency_key(repo):
        return Finding(True, "no runtime source reads the Idempotency-Key header; the repo does "
                             "not appear to implement idempotency", count=0)
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    violations = []
    for path, method, op in repo.operations():
        if method not in NON_IDEMPOTENT_METHODS:
            continue
        if not _op_declares_header_param(op, "Idempotency-Key"):
            violations.append(f"{method.upper()} {path}: does not declare an Idempotency-Key "
                              "header parameter")
    violations = sorted(set(violations))
    note = (" [repo-level approximation per API-0012: the repo reads Idempotency-Key SOMEWHERE "
           "in runtime source, which is not proof that every counted operation's handler reads "
           "it - only that the count can never rise faster than real adoption.]")
    if violations:
        return Finding(False, f"{len(violations)} non-idempotent operation(s) missing "
                              f"Idempotency-Key{note}", _capped(violations), len(violations))
    return Finding(True, f"repo reads Idempotency-Key and every non-idempotent operation declares "
                         f"it{note}", count=0)


def trace_context_propagated(repo: ServiceRepo) -> Finding:
    """API-0014: outbound HTTP goes through the shared instrumented transport."""
    violations = []
    for rel, text in repo.runtime_sources():
        for m in DEFAULT_CLIENT_RE.finditer(text):
            violations.append(f"{rel}:{_line_no(text, m.start())}: uses http.DefaultClient "
                              "(uninstrumented)")
        for m in BARE_OUTBOUND_CALL_RE.finditer(text):
            violations.append(f"{rel}:{_line_no(text, m.start())}: calls http.{m.group(1)}(...) "
                              "directly (uninstrumented)")
        for m in CLIENT_LITERAL_RE.finditer(text):
            if _is_type_reference_not_literal(text, m.start()):
                continue
            brace_idx = m.end() - 1
            body = _extract_balanced(text, brace_idx)
            if SHARED_TRANSPORT_MARKER_RE.search(body):
                continue
            violations.append(f"{rel}:{_line_no(text, m.start())}: bare http.Client{{}} literal "
                              "is not wired to httpx.NewTransport / platform-shared-go/httpclient")
    violations = sorted(set(violations))
    if violations:
        return Finding(False, f"{len(violations)} outbound HTTP client construction(s) bypass "
                              "trace propagation", _capped(violations), len(violations))
    return Finding(True, "every outbound HTTP client is obtained from, or wired to, the shared "
                         "instrumented transport", count=0)


def _collect_header_rules(node, found: set) -> None:
    """Walk parsed config for {name: <header>, value: <something>} rules."""
    if isinstance(node, dict):
        name, value = node.get("name"), node.get("value")
        if isinstance(name, str) and value is not None:
            found.add(name.strip().lower())
        for v in node.values():
            _collect_header_rules(v, found)
    elif isinstance(node, list):
        for v in node:
            _collect_header_rules(v, found)


def _header_set_anywhere(repo: ServiceRepo, header_name: str) -> bool:
    pattern = re.compile(r'\.(?:Header|Set|Add)\(\s*"' + re.escape(header_name) + r'"',
                         re.IGNORECASE)
    if any(pattern.search(text) for _, text in repo.runtime_sources()):
        return True
    if header_name in SECURITY_HEADERS_REQUIRED and repo.uses_shared_security_headers():
        return True
    if header_name == "Content-Security-Policy" and repo.opts_into_shared_csp():
        return True
    return header_name.lower() in repo.config_header_rules()


def _serves_html(repo: ServiceRepo) -> bool:
    return any(HTML_CONTENT_TYPE_RE.search(text) or HTML_TEMPLATE_IMPORT_RE.search(text)
              for _, text in repo.runtime_sources())


def security_response_headers(repo: ServiceRepo) -> Finding:
    """API-0015: browser-facing responses carry the standard security headers.

    A violation here is an ABSENCE across the whole repository, not a bad pattern found at one
    site, so - unlike every other detector in this file - there is no single file:line to name
    for a counted violation. The detail line says so explicitly rather than inventing one.
    """
    required = list(SECURITY_HEADERS_REQUIRED)
    serves_html = _serves_html(repo)
    if serves_html:
        required.append("Content-Security-Policy")
    missing = sorted(h for h in required if not _header_set_anywhere(repo, h))
    if missing:
        detail = [f"{h}: not set anywhere in this repository's runtime source" for h in missing]
        return Finding(False, f"{len(missing)} required security response header(s) absent",
                       _capped(detail), len(missing))
    html_note = " (including Content-Security-Policy, since the repo serves HTML)" if serves_html else ""
    return Finding(True, f"every required security response header is set somewhere in runtime "
                         f"source{html_note}", count=0)


@dataclass
class Finding:
    ok: bool
    evidence: str
    details: list = field(default_factory=list)
    count: int = 0


def _capped(items: list) -> list:
    if len(items) <= MAX_DETAILS:
        return items
    return items[:MAX_DETAILS] + [f"... and {len(items) - MAX_DETAILS} more"]


# --- repository context: read the service once, shared by every detector -------------------

class ServiceRepo:
    def __init__(self, root: Path):
        self.root = root
        self.name = root.name
        self.spec_path = root / "docs" / "openapi.json"
        self.spec, self.spec_error = self._load_spec()
        self.go_sources = self._load_go_sources()
        self._config_headers = None
        self.baseline_error = None
        self.baseline = self._load_baseline()

    def _load_spec(self):
        if not self.spec_path.is_file():
            return None, None
        try:
            return json.loads(self.spec_path.read_text(encoding="utf-8")), None
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(exc)

    def _load_go_sources(self) -> list:
        """Every non-vendored .go file, with its text, split into runtime and test."""
        out = []
        for path in sorted(self.root.rglob("*.go")):
            rel_parts = path.relative_to(self.root).parts
            # Dot-directories are tooling, not the service. In CI the central checker is
            # checked out into .api-contract-tools inside the repo being judged; scanning it
            # would let the guard fail on its own fixtures.
            if any(p.startswith(".") for p in rel_parts):
                continue
            if "vendor" in rel_parts or "node_modules" in rel_parts:
                continue
            try:
                out.append((path.relative_to(self.root), path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
        return out

    def runtime_sources(self):
        return [(rel, text) for rel, text in self.go_sources if not rel.name.endswith("_test.go")]

    def config_header_rules(self) -> set:
        """Lower-cased header names that configuration sets to a value.

        A header applied declaratively is still applied, and reading Go alone gets this
        wrong: edge-gateway sets its whole security header set from config/base/headers.yaml
        via a middleware that loops over the list, so a source-only scan reports four
        headers absent while every response actually carries them.

        Only a name/value PAIR counts. A bare list entry is a request-header allowlist
        (`forward:`, `allowed_headers:`, both of which name Idempotency-Key and If-Match
        here) and sets no response header, so matching the header name alone would trade
        this false positive for a false negative.
        """
        if self._config_headers is None:
            found = set()
            for path in sorted(self.root.rglob("*.y*ml")) + sorted(self.root.rglob("*.json")):
                rel_parts = path.relative_to(self.root).parts
                if any(p.startswith(".") for p in rel_parts):
                    continue
                if {"vendor", "node_modules", "testdata"} & set(rel_parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                # Cheap pre-filter: every rule shape needs the literal key.
                if "name" not in text:
                    continue
                try:
                    doc = (json.loads(text) if path.suffix == ".json"
                           else yaml.safe_load(text))
                except (ValueError, yaml.YAMLError):
                    continue
                _collect_header_rules(doc, found)
            self._config_headers = found
        return self._config_headers

    def test_sources(self):
        return [(rel, text) for rel, text in self.go_sources if rel.name.endswith("_test.go")]

    def uses_shared_security_headers(self) -> bool:
        """Whether runtime source wires the shared ginmiddleware.SecurityHeaders()."""
        return any(SECURITY_HEADERS_MARKER_RE.search(text) for _, text in self.runtime_sources())

    def opts_into_shared_csp(self) -> bool:
        """Whether runtime source opts the shared middleware into Content-Security-Policy."""
        return any(CSP_OPT_IN_MARKER_RE.search(text) for _, text in self.runtime_sources())

    def has_http_api(self) -> bool:
        return self.spec is not None or self.spec_error is not None

    def operations(self):
        """(path, method, operation) for every operation in the spec."""
        if not self.spec:
            return []
        out = []
        for p, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() in ("get", "put", "post", "delete", "patch", "head", "options") \
                        and isinstance(op, dict):
                    out.append((p, method.lower(), op))
        return out

    def _load_baseline(self) -> dict:
        """Frozen per-control counts, or {} when this repo carries no baseline.

        An ABSENT baseline and an UNREADABLE one both used to return {}, and the caller could
        not tell them apart. They mean opposite things: absent is a compliant service held to
        zero, unreadable is a service whose recorded debt just vanished. Conflating them turns
        a typo in this file into "every pre-existing violation is new", which fails the gate
        loudly on code the author never touched. The error is recorded so the caller can refuse
        to run rather than judge the repo against a baseline that is not there.
        """
        path = self.root / BASELINE_FILE
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.baseline_error = str(exc)
            return {}
        if not isinstance(data, dict):
            self.baseline_error = "expected a JSON object at the top level"
            return {}
        counts = data.get("controls", {})
        if not isinstance(counts, dict):
            self.baseline_error = "'controls' is not an object"
            return {}
        return counts


# --- detectors: (ServiceRepo) -> Finding ---------------------------------------------------

def no_runtime_docs(repo: ServiceRepo) -> Finding:
    """API-0001: the deployed runtime serves no documentation (ADR-0067)."""
    violations = []
    for rel, text in repo.runtime_sources():
        for pattern, why in RUNTIME_DOCS_MARKERS:
            if pattern.search(text):
                violations.append(f"{rel}: {why}")
    violations = sorted(set(violations))
    if violations:
        return Finding(False, f"{len(violations)} runtime documentation surface(s)",
                       _capped(violations), len(violations))
    return Finding(True, "no runtime documentation surface", count=0)


def single_committed_spec(repo: ServiceRepo) -> Finding:
    """API-0002: exactly one committed REST spec, plus its declared generator inputs."""
    docs = repo.root / "docs"
    if not docs.is_dir():
        return Finding(True, "no docs/ directory", count=0)
    candidates = sorted({p.name for pattern in ("openapi*.json", "openapi*.yaml", "openapi*.yml",
                                                "swagger*.json", "swagger*.yaml", "swagger*.yml")
                         for p in docs.glob(pattern)})
    extras = [n for n in candidates if n not in ALLOWED_SPEC_FILES]
    if extras:
        return Finding(False, f"{len(extras)} rival spec file(s) committed",
                       [f"docs/{n}: not one of {sorted(ALLOWED_SPEC_FILES)}" for n in extras],
                       len(extras))
    return Finding(True, "one committed spec (plus allowed generator inputs)", count=0)


def shared_conformance_suite(repo: ServiceRepo) -> Finding:
    """API-0003: conformance is imported from the shared engine, never re-implemented."""
    hand_rolled = []
    for rel, text in repo.test_sources():
        if KIN_OPENAPI_MARKERS.search(text) and SHARED_CONFORMANCE_IMPORT not in text:
            hand_rolled.append(f"{rel}: drives kin-openapi directly instead of importing "
                               f"{SHARED_CONFORMANCE_IMPORT}")
    hand_rolled = sorted(set(hand_rolled))
    if hand_rolled:
        return Finding(False, f"{len(hand_rolled)} hand-rolled conformance validator(s)",
                       _capped(hand_rolled), len(hand_rolled))
    return Finding(True, "conformance validation comes from the shared suite", count=0)


def canonical_envelope(repo: ServiceRepo) -> Finding:
    """API-0004: every JSON response references the proto common.v1 envelope."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    offenders = []
    for path, method, op in repo.operations():
        for status, resp in (op.get("responses") or {}).items():
            if not isinstance(resp, dict):
                continue
            schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
            if not isinstance(schema, dict):
                continue  # bodiless (204) or non-JSON: nothing to bind
            wanted = CANONICAL_ERROR if status.startswith(("4", "5")) else CANONICAL_SUCCESS
            if wanted not in json.dumps(schema):
                offenders.append(f"{method.upper()} {path} -> {status}: does not reference {wanted}")
    if offenders:
        return Finding(False, f"{len(offenders)} response(s) not bound to the canonical envelope",
                       _capped(sorted(offenders)), len(offenders))
    return Finding(True, "every JSON response references the common.v1 envelope", count=0)


def operation_ids_governed(repo: ServiceRepo) -> Finding:
    """API-0005: every operation has a unique operationId, and the id set is locked."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    missing, seen, duplicates = [], set(), []
    for path, method, op in repo.operations():
        oid = op.get("operationId")
        if not oid:
            missing.append(f"{method.upper()} {path}: no operationId")
            continue
        if oid in seen:
            duplicates.append(f"{method.upper()} {path}: duplicate operationId {oid!r}")
        seen.add(oid)
    problems = sorted(missing) + sorted(duplicates)
    lock = repo.root / "docs" / "openapi.operationids.lock.json"
    if not lock.is_file() and repo.operations():
        problems.append("docs/openapi.operationids.lock.json is absent: operationIds are a public "
                        "API surface and must be semver-governed")
    if problems:
        return Finding(False, f"{len(missing)} missing, {len(duplicates)} duplicate, "
                              f"lock {'present' if lock.is_file() else 'absent'}",
                       _capped(problems), len(problems))
    return Finding(True, f"{len(seen)} operationIds present, unique, and locked", count=0)


def canonical_components_current(repo: ServiceRepo) -> Finding:
    """API-0007: the spec's common.v1 components match the proto projection exactly.

    API-0004 proves a response POINTS AT the envelope. This proves the envelope it points
    at is the CURRENT one. The distinction matters because each service projects the
    components from its own platform-contracts-go pin, so two services can both pass every
    in-repo gate while publishing structurally different common.v1.Meta - each internally
    consistent, which is all a per-repo drift check can ever establish.
    """
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    reference, ref_version = load_canonical_components()
    if reference is None:
        return Finding(True, "no reference artifact available; skipped", count=0)
    schemas = ((repo.spec or {}).get("components") or {}).get("schemas") or {}
    published = {k: v for k, v in schemas.items() if k.startswith("common.v1.")}
    if not published:
        # Pre-migration service on a bespoke envelope. API-0004 already owns that failure;
        # reporting it twice would double-count the same debt in two baselines.
        return Finding(True, "no common.v1 components published; API-0004 governs adoption", count=0)
    problems = []
    for name in sorted(set(reference) | set(published)):
        want, got = reference.get(name), published.get(name)
        if want is None:
            continue  # a service may publish extra components of its own
        if got is None:
            problems.append(f"{name}: missing from the spec but present in {ref_version}")
        elif json.dumps(got, sort_keys=True) != json.dumps(want, sort_keys=True):
            problems.append(f"{name}: differs from the {ref_version} proto projection")
    if problems:
        return Finding(False, f"{len(problems)} component(s) stale against {ref_version}",
                       _capped(problems), len(problems))
    return Finding(True, f"common.v1 components match the {ref_version} proto projection", count=0)


def no_swaggo_annotation_source(repo: ServiceRepo) -> Finding:
    """API-0006: no swaggo annotation source; the engine generates from Go types."""
    offenders = [str(rel) for rel, _ in repo.go_sources if rel.name == "swagger_main.go"]
    if offenders:
        return Finding(False, f"{len(offenders)} swaggo annotation source file(s)",
                       [f"{o}: superseded by the reflection engine (ADR-0048) and the "
                        "no-runtime-docs decision (ADR-0067)" for o in offenders], len(offenders))
    return Finding(True, "no swaggo annotation source", count=0)


def protocol_version_pinned(repo: ServiceRepo) -> Finding:
    """API-0013: docs/openapi.json declares the platform's single pinned OpenAPI dialect."""
    if repo.spec_error:
        return Finding(False, f"docs/openapi.json is unparseable: {repo.spec_error}", count=1)
    found = (repo.spec or {}).get("openapi")
    if found == PINNED_OPENAPI_VERSION:
        return Finding(True, f"openapi version pinned at {PINNED_OPENAPI_VERSION!r}", count=0)
    found_display = repr(found) if found is not None else "missing"
    detail = f"docs/openapi.json: openapi={found_display}, expected {PINNED_OPENAPI_VERSION!r}"
    return Finding(False, detail, [detail], 1)


DETECTORS = {
    "no_runtime_docs": no_runtime_docs,
    "single_committed_spec": single_committed_spec,
    "shared_conformance_suite": shared_conformance_suite,
    "canonical_envelope": canonical_envelope,
    "canonical_components_current": canonical_components_current,
    "operation_ids_governed": operation_ids_governed,
    "no_swaggo_annotation_source": no_swaggo_annotation_source,
    "conditional_requests": conditional_requests,
    "cache_key_declared": cache_key_declared,
    "standard_ratelimit_fields": standard_ratelimit_fields,
    "patch_media_type": patch_media_type,
    "idempotency_declared": idempotency_declared,
    "protocol_version_pinned": protocol_version_pinned,
    "trace_context_propagated": trace_context_propagated,
    "security_response_headers": security_response_headers,
}


# --- catalog + evaluation ------------------------------------------------------------------

def load_controls(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"::error::cannot read control catalog {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controls"), list):
        raise SystemExit(f"::error::{path}: invalid control catalog (expected a 'controls:' list)")
    errors, seen = [], set()
    for i, c in enumerate(doc["controls"]):
        cid = c.get("id", f"#{i}")
        missing = [f for f in REQUIRED_FIELDS if not c.get(f)]
        if missing:
            errors.append(f"{cid}: missing required field(s) {missing}")
        if c.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity {c.get('severity')!r} not in {sorted(VALID_SEVERITY)}")
        if c.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status {c.get('status')!r} not in {sorted(VALID_STATUS)}")
        if c.get("scope") not in VALID_SCOPE:
            errors.append(f"{cid}: scope {c.get('scope')!r} not in {sorted(VALID_SCOPE)}")
        if c.get("applies_when") not in VALID_APPLIES:
            errors.append(f"{cid}: applies_when {c.get('applies_when')!r} not in {sorted(VALID_APPLIES)}")
        if c.get("detector") not in DETECTORS:
            errors.append(f"{cid}: unknown detector {c.get('detector')!r}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id (IDs must be unique and stable)")
        seen.add(cid)
    if errors:
        for e in errors:
            print(f"::error::api-contract catalog invalid: {e}")
        raise SystemExit(2)
    return doc


def baseline_problems(repo: ServiceRepo, controls: list) -> list:
    """Reasons this repo's baseline cannot be trusted to mean what it says.

    THIS EXISTS BECAUSE IT ALREADY HAPPENED. Control IDs were renamed from three digits to
    four (API-001 -> API-0001) in the catalog, which lives in this repository. The baselines
    keyed on those IDs live in the SERVICE repositories, so no single commit could carry both
    halves, and the rename shipped alone. Every baseline key then matched no control.

    Nothing detected it, because the lookup in evaluate() is `repo.baseline.get(c["id"], 0)`:
    a control asks for its own ID, does not find it, and reads its frozen debt as zero. The
    stale keys are never consulted, so they raise no error. All four gateway repositories went
    red at once - inboxxhq-platform-bff reporting 678 responses "not bound to the canonical
    envelope" as though a single commit had introduced them - and stayed red, for a reason
    visible nowhere in the output.

    The ratchet is the only thing making this catalog enforceable against services that already
    carry debt, so a baseline that silently stops applying does not weaken the gate, it inverts
    it: the check now fails precisely the repositories that were being tolerated. Refusing to
    run is the same call the API-0007 guard makes in main() - a control whose reference artifact
    is missing must not report a verdict it did not actually reach.
    """
    if repo.baseline_error:
        return [f"{repo.name}: {BASELINE_FILE} is unreadable ({repo.baseline_error}). "
                f"Fix the file or delete it - deleting holds this repo to zero, which is a "
                f"stricter gate, not a looser one."]
    known = {c["id"] for c in controls}
    unknown = sorted(set(repo.baseline) - known)
    if not unknown:
        return []
    return [f"{repo.name}: {BASELINE_FILE} freezes {k}, which is not a control in this "
            f"catalog, so the count it records is being ignored and that control is held to "
            f"zero. If {k} was renamed, rename it here too; if it was retired, drop the line. "
            f"Re-run with --write-baseline to refreeze from the current state."
            for k in unknown]


def evaluate(repo: ServiceRepo, controls: list) -> list:
    results = []
    for c in controls:
        rec = {"control": c["id"], "title": c["title"], "severity": c["severity"],
               "scope": c["scope"], "applies_when": c["applies_when"], "owner": c["owner"],
               "status": c["status"], "result": None, "evidence": "", "details": [],
               "count": 0, "baseline": repo.baseline.get(c["id"], 0), "remediation": ""}
        if c["status"] != "active":
            rec.update(result="skipped", evidence=f"lifecycle status={c['status']} (not evaluated)")
        elif c["applies_when"] == "http-api" and not repo.has_http_api():
            rec.update(result="skipped", evidence="no docs/openapi.json; not an HTTP API service")
        else:
            f = DETECTORS[c["detector"]](repo)
            rec.update(count=f.count, evidence=f.evidence, details=f.details)
            # Ratchet: a frozen count is tolerated, a rise is not, a fall is celebrated.
            if f.count > rec["baseline"]:
                rec.update(result="fail",
                           remediation=" ".join(str(c["remediation"]).split()))
            elif f.count < rec["baseline"]:
                rec.update(result="improved")
            elif f.count > 0:
                rec.update(result="frozen")
            else:
                rec.update(result="pass")
        results.append(rec)
    return results


def is_enforced(rec: dict, threshold: int) -> bool:
    return rec["result"] == "fail" and SEVERITY_ORDER[rec["severity"]] >= threshold


MARK = {"pass": "ok", "fail": "XX", "frozen": "==", "improved": "->", "skipped": "--"}


def render_text(repo: ServiceRepo, results: list, threshold: int) -> None:
    print(f"::group::api-contract: {repo.name}")
    for r in results:
        line = f"[{MARK[r['result']]}] {r['control']} [{r['severity']}] {r['title']}: {r['evidence']}"
        if r["result"] in ("frozen", "improved"):
            line += f" (baseline {r['baseline']}, now {r['count']})"
        print("  " + line)
    print("::endgroup::")
    for r in results:
        if r["result"] == "fail":
            level = "error" if is_enforced(r, threshold) else "warning"
            rose = f" - rose from baseline {r['baseline']} to {r['count']}" if r["baseline"] else ""
            print(f"::{level}::[{r['control']}][{r['severity']}] {r['title']}{rose}: {r['evidence']}")
            for d in r["details"]:
                print(f"    - {d}")
            if r["remediation"]:
                print(f"    remediation: {r['remediation']}")
        elif r["result"] == "improved":
            print(f"::notice::[{r['control']}] improved from {r['baseline']} to {r['count']} - "
                  f"run --write-baseline to lock the gain in")


def build_report(repo: ServiceRepo, results: list, policy_ssot: list, fail_on: str, threshold: int) -> dict:
    return {
        "service": repo.name,
        "root": str(repo.root),
        "policy_ssot": policy_ssot,
        "fail_on": fail_on,
        "http_api": repo.has_http_api(),
        "baseline_present": bool(repo.baseline),
        "controls_total": len(results),
        "passed": sum(1 for r in results if r["result"] == "pass"),
        "frozen": sum(1 for r in results if r["result"] == "frozen"),
        "improved": sum(1 for r in results if r["result"] == "improved"),
        "failed": sum(1 for r in results if r["result"] == "fail"),
        "skipped": sum(1 for r in results if r["result"] == "skipped"),
        "enforced_failures": sum(1 for r in results if is_enforced(r, threshold)),
        "open_violations": {r["control"]: r["count"] for r in results if r["count"]},
        "ok": not any(is_enforced(r, threshold) for r in results),
        "results": results,
    }


def write_baseline(repo: ServiceRepo, results: list) -> int:
    """Freeze this catalog's per-control counts, leaving the rest of the file alone.

    THE FILE IS CO-OWNED. `controls` belongs to this script. `ihq validate --repo` freezes its
    own per-check counts in a sibling `checks` key, because the pre-push hook has to reach the
    same verdict this gate does and could not without the same ratchet.

    Both destructive habits this function used to have were therefore silent data loss. It
    rebuilt the payload as {_comment, controls} from scratch, so any sibling key was dropped;
    and it deleted the whole file when this catalog had nothing to freeze, taking the other
    tool's counts with it. Either one unfreezes several hundred findings in a repository whose
    developer just ran a flag that is supposed to be a no-op on the gate - and it would surface
    as a push blocked on violations nobody introduced, which is precisely the failure the
    ratchet exists to prevent.

    So the write is member-by-member, and the file is retired only when this catalog's key was
    the only thing of substance in it. ihq's retireBaselineChecks is the mirror of this.
    """
    counts = {r["control"]: r["count"] for r in results if r["count"]}
    path = repo.root / BASELINE_FILE

    existing = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            # An unreadable baseline is refused by baseline_problems() on every path except
            # this one, which is the documented way to recover from it. Overwriting is the
            # point here, so there is nothing to preserve and nothing to report.
            existing = {}
    # Ordered so a rewrite is a no-op diff: _comment, this catalog's counts, then whatever
    # else already lived in the file, in the order it was written.
    others = {k: v for k, v in existing.items() if k not in ("_comment", "controls")}

    if not counts:
        # A compliant service carries no debt artifact, and the last deletion in a repo is the
        # moment its migration is provably finished. That only holds while this catalog is the
        # sole owner of the file.
        if others:
            payload = {"_comment": BASELINE_COMMENT, **others}
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"api-contract: {repo.name} is clean - dropped 'controls' and kept "
                  f"{', '.join(sorted(others))} for the tool that owns it")
        elif path.is_file():
            path.unlink()
            print(f"api-contract: {repo.name} is clean - removed {BASELINE_FILE}")
        else:
            print(f"api-contract: {repo.name} is clean - no baseline needed")
        return 0

    payload = {"_comment": BASELINE_COMMENT, "controls": counts, **others}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    kept = f", preserved {', '.join(sorted(others))}" if others else ""
    print(f"api-contract: wrote {path} ({sum(counts.values())} violation(s) frozen "
          f"across {len(counts)} control(s){kept})")
    return 0


def render_markdown(doc: dict) -> str:
    rows = ["| Control | Policy | Severity | Scope | Applies | Owner | Status |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in doc["controls"]:
        policy = " ".join(str(c["policy"]).split())
        rows.append(f"| {c['id']} | {policy} | {c['severity']} | {c['scope']} | "
                    f"{c['applies_when']} | {c['owner']} | {c['status']} |")
    return "\n".join(rows)


def _docs_block(doc: dict) -> str:
    return (f"{DOCS_BEGIN}\n\n_Generated from `controls/api-contract.yaml` by "
            f"`scripts/check-api-contract.py --write-docs` — do not edit by hand._\n\n"
            f"{render_markdown(doc)}\n\n{DOCS_END}")


def write_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        raise SystemExit(f"::error::{path}: missing {DOCS_BEGIN} / {DOCS_END} markers")
    head, _, rest = text.partition(DOCS_BEGIN)
    _, _, tail = rest.partition(DOCS_END)
    path.write_text(head + _docs_block(doc) + tail, encoding="utf-8")
    print(f"api-contract: wrote generated control table into {path}")
    return 0


def verify_docs(doc: dict, path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if DOCS_BEGIN not in text or DOCS_END not in text:
        raise SystemExit(f"::error::{path}: missing {DOCS_BEGIN} / {DOCS_END} markers")
    head, _, rest = text.partition(DOCS_BEGIN)
    _, _, tail = rest.partition(DOCS_END)
    if head + _docs_block(doc) + tail != text:
        print(f"::error::{path}: control table drifted from controls/api-contract.yaml "
              "- run scripts/check-api-contract.py --write-docs")
        return 1
    print(f"api-contract: {path} control table is in sync with the catalog")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Service API-contract guard (executable form of "
                                             "ADR-0048 + ADR-0067 + RFC-0001).")
    ap.add_argument("roots", nargs="*", default=["."], help="service repository roots to check")
    ap.add_argument("--controls", default=str(DEFAULT_CONTROLS), help="control catalog YAML")
    ap.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ap.add_argument("--fail-on", choices=("critical", "major", "minor"), default="major")
    ap.add_argument("--report", help="write the JSON report to this path")
    ap.add_argument("--write-baseline", action="store_true",
                    help="freeze current violation counts into .api-contract-baseline.json")
    ap.add_argument("--write-docs", metavar="FILE", help="regenerate the control table in FILE and exit")
    ap.add_argument("--verify-docs", metavar="FILE", help="fail if FILE's control table drifted; then exit")
    args = ap.parse_args(argv)

    doc = load_controls(Path(args.controls))
    if args.write_docs:
        return write_docs(doc, Path(args.write_docs))
    if args.verify_docs:
        return verify_docs(doc, Path(args.verify_docs))
    if args.format == "markdown":
        print(render_markdown(doc))
        return 0

    threshold = SEVERITY_ORDER[args.fail_on]
    controls = doc["controls"]

    # Refuse to run a control whose reference artifact is missing rather than let it report a
    # pass it never actually checked.
    if any(c.get("detector") == "canonical_components_current" for c in controls):
        if load_canonical_components()[0] is None:
            print(f"::error::API-0007 is enabled but {CANONICAL_COMPONENTS} is missing or "
                  "unreadable. Check out this repository's controls/ directory, or regenerate "
                  "the artifact from platform-shared-go with `go run "
                  "./platform/openapicontract/commonv1policy/cmd/emit-canonical-components`.")
            return 2
    reports, failed = [], False
    for raw in (args.roots or ["."]):
        root = Path(raw).resolve()
        if not root.is_dir():
            print(f"::error::not a directory: {root}")
            return 2
        repo = ServiceRepo(root)
        # --write-baseline is the documented fix for a stale key, so it has to stay reachable
        # when the baseline is the thing that is broken.
        if not args.write_baseline:
            problems = baseline_problems(repo, controls)
            if problems:
                for p in problems:
                    print(f"::error::api-contract baseline invalid: {p}")
                return 2
        results = evaluate(repo, controls)
        if args.write_baseline:
            write_baseline(repo, results)
            continue
        report = build_report(repo, results, doc.get("policy_ssot", []), args.fail_on, threshold)
        reports.append(report)
        if args.format == "text":
            render_text(repo, results, threshold)
        failed = failed or not report["ok"]

    if args.write_baseline:
        return 0
    payload = reports[0] if len(reports) == 1 else {"repos": reports,
                                                    "ok": all(r["ok"] for r in reports)}
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
