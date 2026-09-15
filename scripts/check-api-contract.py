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
import os
import re
import subprocess
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
# This gate's key in that co-owned file. It reads and writes this member and no other.
# Every member is named in the output because the file's name mentions none of them, so a
# verdict about one member reads as a verdict about the file -- and all three gates accept
# a `--write-baseline` flag while writing different members of it.
BASELINE_CONTROLS_KEY = "controls"
BASELINE_CHECKS_KEY = "checks"
# The members this gate does NOT read, mapped to the tool that does and how to refreeze
# it. Three programs freeze debt in this one file under three vocabularies and its name
# mentions none of them, so a verdict from any one reads as a verdict about the file.
BASELINE_FOREIGN_KEYS = {
    BASELINE_CHECKS_KEY: {
        "tool": "the ihq CLI's `ihq validate` gate",
        "refreeze": "ihq validate --repo . --write-baseline",
        # kebab-case check names, recognised by NOT matching another ID shape
        "id_shape": None,
    },
    "arch": {
        "tool": "architecture-check (`ihq arch`)",
        "refreeze": "ihq arch --repo . --write-baseline",
        "id_shape": r"ARCH-\d+",
    },
}
# Byte-identical to baselineComment in the ihq CLI's internal/cli/baseline.go. Both tools write
# this key, so a difference would make the file flip between two texts as each ran, turning
# every alternate run into a spurious diff on a line neither tool actually disagreed about.
BASELINE_COMMENT = ("Frozen debt for this service, co-owned by three gates that each read ONE member: "
                    "'controls' is check-api-contract.py, 'checks' is `ihq validate`, 'arch' is `ihq arch`. "
                    "Each fails if its own counts RISE. Lowering one member does NOT affect another, so "
                    "refreeze with the tool that owns the member you changed. Raising a number here is a "
                    "reviewable, deliberate act.")

# The proto projection API-0007 compares against. Generated, never hand-written:
#   go run ./platform/openapicontract/commonv1policy/cmd/emit-canonical-components
# in platform-shared-go, redirected here. Its `source.version` records the
# platform-contracts-go release it was projected from.
#
# The top-level `components`/`source` pair is the CURRENT set. While the projection moves
# to a new vintage, the file may carry ONE more accepted set beside it, under `next`
# (staged, newer than current) or `previous` (just promoted away from, older). Both are
# written by --stage-next / --promote-next / --drop-set from emitter output, and both this
# repository's CI and release.yaml hold the file against the last release (--verify-floor
# --base-from-event, --base <release tag>), so a set cannot be added, replaced or reordered by
# hand and still reach @v1. A file with neither is exactly what the emitter prints, and means
# what it always meant: one vintage, one reference.
#
# The checker trusts that a set's components ARE emitter output for the source it names: it
# checks shape, order and history, not provenance. Review a staged set by re-running the
# emitter at the recorded pin (README, "Moving the reference").
#
# A vintage is a PAIR. platform-contracts-go carries the messages, but platform-shared-go
# carries the projection rules (protoschema, commonv1policy), so one contracts release
# projects differently under two shared-go releases. `source.projector` records the
# shared-go release when the emitter knows it; `source.version` alone cannot order two sets
# whose projection moved only in shared-go.
CANONICAL_COMPONENTS = SELF_REPO / "controls" / "common-v1-components.json"
# What main() compares against: CANONICAL_COMPONENTS unless --components names another file.
reference_path = CANONICAL_COMPONENTS
CONTRACTS_MODULE = "github.com/coderaxis/platform-contracts-go"
PROJECTOR_MODULE = "github.com/coderaxis/platform-shared-go"
CURRENT_SET = "current"
EXTRA_SET_ROLES = ("next", "previous")
SET_KEYS = {"_comment", "components", "source"}
REFERENCE_KEYS = {*SET_KEYS, *EXTRA_SET_ROLES}
STABLE_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_canonical_cache: dict = {}


@dataclass(frozen=True)
class ComponentSet:
    """One accepted common.v1 projection: every component one (contracts, projector) pair produces."""
    role: str
    module: str
    version: str
    components: dict
    # The platform-shared-go release that projected the set, when the emitter recorded it.
    projector_module: str | None = None
    projector_version: str | None = None
    # The raw `source` object, so two sets compare on everything that names their provenance.
    source: dict = field(default_factory=dict)

    @property
    def vintage(self) -> str:
        if self.projector_version is None:
            return self.version
        return f"{self.version} via {self.projector_module.rsplit('/', 1)[-1]} {self.projector_version}"

    @property
    def label(self) -> str:
        return f"{self.role} {self.vintage}"

    @property
    def identity(self) -> str:
        return _canonical({"components": self.components, "source": self.source})


def _stable_version(version: str):
    m = STABLE_VERSION_RE.match(version or "")
    return tuple(int(p) for p in m.groups()) if m else None


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True)


def parse_reference(doc) -> tuple:
    """Return (accepted sets, newest first, None) or (None, reason) for an unusable reference.

    A single-set file - today's, and whatever the emitter prints - keeps its old tolerance
    for an unrecorded version. An extra set tightens that on purpose: it only exists to
    order two vintages, so both have to be stable tags, the extra one has to sit on the side
    of current its role names, and it has to project something different, or the window it
    opens migrates nothing.

    Ordering is on the (contracts, projector) pair. A newer contracts release orders two
    sets on its own, so files written before `source.projector` existed keep working. Equal
    contracts releases are ordered only by recorded projector versions: that is the
    projection change shipping in platform-shared-go alone. And the projector may not move
    against the contracts direction, since a newer contracts release projected by an older
    shared-go drops whatever projection rules the newer shared-go added.
    """
    if not isinstance(doc, dict):
        return None, "is not a JSON object"
    unknown = sorted(set(doc) - REFERENCE_KEYS)
    if unknown:
        # A misspelt `next` would otherwise be ignored, and the migration it was meant to
        # open would surface as a fleet of red API-0007 lanes instead of one error here.
        return None, (f"carries unknown member(s) {unknown}; the accepted members are "
                      f"{sorted(REFERENCE_KEYS)}")

    def one(role: str, body) -> tuple:
        if not isinstance(body, dict):
            return None, f"{role} set is not an object"
        if role != CURRENT_SET and set(body) - SET_KEYS:
            # The same reason as the top level: `componets` must not read as an empty set.
            return None, (f"{role} set carries unknown member(s) {sorted(set(body) - SET_KEYS)}; "
                          f"a set carries only {sorted(SET_KEYS)}")
        comps = body.get("components")
        if not isinstance(comps, dict) or not comps:
            return None, (f"{role} set has no components, so every spec would match it and "
                          f"API-0007 would pass without comparing anything")
        source = body.get("source") if isinstance(body.get("source"), dict) else {}
        projector = source.get("projector")
        if projector is not None and not (isinstance(projector, dict) and projector.get("version")):
            return None, (f"{role} set's source.projector must be an object naming the "
                          f"platform-shared-go `module` and `version` that projected it")
        return ComponentSet(role, str(source.get("module") or CONTRACTS_MODULE),
                            str(source.get("version") or "unknown"), comps,
                            str(projector.get("module") or PROJECTOR_MODULE) if projector else None,
                            str(projector["version"]) if projector else None,
                            source), None

    current, err = one(CURRENT_SET, doc)
    if err:
        return None, err
    extras = [role for role in EXTRA_SET_ROLES if role in doc]
    if not extras:
        return [current], None
    if len(extras) > 1:
        return None, (f"carries both {' and '.join(extras)}. At most two vintages are accepted "
                      f"at once: drop `previous` (finishing the last migration) before staging "
                      f"the next one")
    extra, err = one(extras[0], doc[extras[0]])
    if err:
        return None, err
    if extra.module != current.module:
        return None, (f"{extra.label} was projected from {extra.module}, current from "
                      f"{current.module}; accepted sets must come from the same module")
    cur_v, extra_v = _stable_version(current.version), _stable_version(extra.version)
    if cur_v is None or extra_v is None:
        return None, (f"current {current.version!r} and {extra.role} {extra.version!r} must both "
                      f"be stable vX.Y.Z tags; an accepted set from an untagged or replaced "
                      f"build cannot be pinned by any service")
    for s in (current, extra):
        if s.projector_version is not None and _stable_version(s.projector_version) is None:
            # `go run ./...` inside a checkout reports "(devel)": no service can pin that.
            return None, (f"{s.label} records projector {s.projector_version!r}, which is not a "
                          f"stable vX.Y.Z tag; stage emitter output from a tagged platform-shared-go "
                          f"release (go run <package>@<tag>) so services can pin the pair")
    if (current.projector_module and extra.projector_module
            and current.projector_module != extra.projector_module):
        return None, (f"{extra.label} was projected by {extra.projector_module}, current by "
                      f"{current.projector_module}; accepted sets must share a projector module")
    direction = "newer" if extra.role == "next" else "older"
    want = 1 if extra.role == "next" else -1
    contracts = _cmp(extra_v, cur_v)
    projector = (None if current.projector_version is None or extra.projector_version is None
                 else _cmp(_stable_version(extra.projector_version),
                           _stable_version(current.projector_version)))
    if contracts == -want or (contracts == 0 and projector in (0, -want)):
        return None, f"{extra.label} is not {direction} than current {current.vintage}"
    if contracts == 0 and projector is None:
        return None, (f"{extra.label} is not {direction} than current {current.vintage}: both name "
                      f"the same {CONTRACTS_MODULE} release, and only a platform-shared-go "
                      f"release recorded on BOTH sets (source.projector) can order a projection "
                      f"change that ships in platform-shared-go alone")
    if projector == -want:
        return None, (f"{extra.label} pairs a {direction} contracts release with a projector that "
                      f"is not {direction} than current {current.vintage}, so it drops projection "
                      f"rules the other set's platform-shared-go already applies")
    if _canonical(extra.components) == _canonical(current.components):
        return None, (f"{extra.label} projects components byte-identical to current "
                      f"{current.vintage}, so there is nothing to migrate. Relabel current "
                      f"instead: replace the file with that emitter output, which changes only "
                      f"`source`")
    # Newest first, so a spec both sets accept is reported against the vintage it is headed to.
    return ([extra, current] if extra.role == "next" else [current, extra]), None


def load_canonical_components() -> tuple:
    """Return (accepted sets, None) for reference_path, or (None, reason) when it is unusable.

    Callers must treat unavailable as a configuration error, not a skip - main() checks
    this up front and exits 2. The artifact ships in this repository beside this script,
    so its absence means a broken checkout (a sparse-checkout that omitted controls/, say),
    and a version gate that reports a pass because it could not find its reference is worse
    than no gate at all.
    """
    path = Path(reference_path)
    key = str(path)
    if key not in _canonical_cache:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _canonical_cache[key] = (None, f"is missing or unreadable ({exc})")
        else:
            _canonical_cache[key] = parse_reference(doc)
    return _canonical_cache[key]

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


def _strip_go_comments(text: str, blank_strings: bool = False) -> str:
    """Blank out Go comments, preserving line numbers. Optionally blank string contents too.

    Ported from check-gateway-baseline.py, which needed the same thing. Non-code is replaced
    character-for-character with spaces and newlines are kept, so a match's offset still maps
    to the line it came from and every caller's `_line_no` stays correct.

    This is deliberately a state machine and NOT a `//.*` regex, because a regex gets Go
    wrong in a way that reads as working. The gateway detector originally used the naive
    version and the authorization policy string "order.v1.OrderService/*" opened a block
    comment that never closed, silently discarding the rest of the file - including the gRPC
    server assembly 44 lines below. So a policy string blinded the detector that reads
    policy. Anything cheaper than tracking quotes has that failure mode.

    blank_strings additionally empties string, rune and raw-string CONTENTS while keeping the
    quotes, for checks whose pattern can appear inside a literal. That is not hypothetical:
    the ihq CLI's own RFC-0038 detector calls traceFinding(f.Path, i, "http.Client{}") and
    ("http.DefaultClient"), so naming the patterns it detects made it report itself. Blanking
    contents also fixes brace counting for free, since a `{` inside a string no longer feeds
    _extract_balanced.

    NOT applied to runtime_sources() for every check, which would be the tempting
    simplification: swaggo annotations ARE comments (`// @Summary`), and API-0006 exists to
    find them. A global strip would make the swaggo detector permanently report clean.
    """
    out, i, state = [], 0, "code"
    while i < len(text):
        c = text[i]
        n = text[i + 1] if i + 1 < len(text) else ""
        if state == "line":
            out.append(c if c == "\n" else " ")
            if c == "\n":
                state = "code"
        elif state == "block":
            if c == "*" and n == "/":
                out.extend("  ")
                i += 1
                state = "code"
            else:
                out.append("\n" if c == "\n" else " ")
        elif state in ("string", "rune"):
            quote = '"' if state == "string" else "'"
            closing = c == quote
            out.append(c if closing or not blank_strings else " ")
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1] if not blank_strings else " ")
                i += 1
            elif closing:
                state = "code"
        elif state == "raw":
            # A raw string may span lines, so newlines are kept even when blanking.
            out.append(c if c in ("`", "\n") or not blank_strings else " ")
            if c == "`":
                state = "code"
        elif c == "/" and n == "/":
            out.extend("  ")
            i += 1
            state = "line"
        elif c == "/" and n == "*":
            out.extend("  ")
            i += 1
            state = "block"
        else:
            out.append(c)
            if c == '"':
                state = "string"
            elif c == "'":
                state = "rune"
            elif c == "`":
                state = "raw"
        i += 1
    return "".join(out)


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


def _deref_component(node, section: dict, prefix: str, seen: frozenset = frozenset()):
    """Follow a single `$ref` into one components section, with a cycle guard."""
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return node
    name = ref[len(prefix):]
    if name in seen:
        return node
    target = section.get(name)
    if not isinstance(target, dict):
        return node
    return _deref_component(target, section, prefix, seen | {name})


def _deref_op(op: dict, components_root: dict) -> dict:
    """An operation with its `parameters` and `responses` resolved through components.

    OpenAPI lets an operation reference a shared parameter or response OBJECT, and these specs
    do. The checks below read `.name`, `.headers` and `.content` straight off those nodes, and a
    bare {"$ref": ...} carries none of them - so an operation documenting an ETag, an If-Match
    parameter or a pagination parameter in a shared component reads as documenting nothing.

    Each of the four resulting misreads points the same wrong way: it reports something absent
    that is present, and the only way to satisfy the control becomes inlining what was
    deliberately shared. Resolving schema $refs while ignoring response and parameter ones was
    the gap - notification-service's paginated GET /org/notifications/order/{orderID} refs
    components.responses.ArraySuccess, and so was read as a single-resource read owing an ETag.
    """
    if not isinstance(op, dict):
        return op
    params_section = components_root.get("parameters") or {}
    responses_section = components_root.get("responses") or {}
    resolved = dict(op)
    if isinstance(op.get("parameters"), list):
        resolved["parameters"] = [
            _deref_component(p, params_section, "#/components/parameters/")
            for p in op["parameters"]
        ]
    if isinstance(op.get("responses"), dict):
        resolved["responses"] = {
            status: _deref_component(body, responses_section, "#/components/responses/")
            for status, body in op["responses"].items()
        }
    return resolved


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
    components_root = (repo.spec or {}).get("components") or {}
    components = components_root.get("schemas") or {}
    ops = [(path, method, _deref_op(op, components_root))
           for path, method, op in repo.operations()]

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
    """API-0010: no X-RateLimit-*, and every 429 carries Retry-After from the same function.

    Two views of each file, because the halves of this control read opposite things.

    The 429 write-site half must NOT see string contents. Its patterns name
    StatusTooManyRequests and ResponseWriter, and a detector spells both as pattern text --
    the Go implementation reported four findings against its own exclusion table, where
    every token it searches for sat inside a regex literal and no edit to those lines could
    have cleared it. Comment handling alone was not enough: _is_429_write_site only skipped
    lines STARTING with a comment marker, so a string kept scoring.

    The header half must SEE string contents: "X-RateLimit-*" and "Retry-After" are the
    evidence itself. Blanking them would drop the violation and, worse, the thing that
    clears it -- an unseen Retry-After reads as an absent one, so every correct handler
    would be flagged.

    Comments are blanked in both. Commented-out code cannot violate the rule, and prose
    naming Retry-After must not credit a handler that never sets it, or the way to clear a
    real finding becomes writing a sentence about it.

    Kept deliberately identical to the Go implementation in internal/openapi/rfc0038.go:
    the parity harness compares counts on this family, so both sides must blank the same
    things or they disagree while both reporting "flagged".
    """
    violations = []
    for rel, text in repo.runtime_sources():
        code_lines = _strip_go_comments(text, blank_strings=True).splitlines()
        text_lines = _strip_go_comments(text, blank_strings=False).splitlines()
        for i, line in enumerate(text_lines):
            m = XRATELIMIT_SITE_RE.search(line)
            if m:
                violations.append(f"{rel}:{i + 1}: sets prohibited {m.group(1)} response header")
        for i, line in enumerate(code_lines):
            if not STATUS_429_RE.search(line) or not _is_429_write_site(line):
                continue
            start, end = _enclosing_function_span(code_lines, i)
            if not RESPONSE_WRITER_RE.search("\n".join(code_lines[start:end])):
                continue
            if RETRY_AFTER_SET_RE.search("\n".join(text_lines[start:end])):
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
    """API-0014: outbound HTTP goes through the shared instrumented transport.

    Comments are blanked first, because a comment cannot construct an HTTP client and this
    check had no comment handling at all. Two repositories were flagged for prose, and both
    are the shape that makes this worse than an ordinary false positive:

    org-service's comment DESCRIBES THE FIX ALREADY APPLIED - "each built their own
    &http.Client{} per invocation, which both skipped trace propagation (RFC-0038 section 8)"
    - sitting nine lines above the corrected construction that passes httpx.NewTransport(nil).
    So the finding penalised explaining the remediation while the remediation was in place,
    and the way to clear it was to delete the explanation.

    inboxxhq-architecture-check's is the explanatory comment inside its OWN rule for detecting
    bare http.Client literals. The tool that finds the pattern was reported for naming it.

    A detector that cannot tell code from prose about code teaches people to stop writing the
    prose, which costs more than the finding was ever worth.

    String contents are blanked for the same reason and on the same evidence: the ihq CLI's
    RFC-0038 detector names these patterns in string arguments - traceFinding(f.Path, i,
    "http.Client{}") - so it reported itself, three findings' worth, on a repository whose
    outbound clients that code never touches.
    """
    violations = []
    for rel, text in repo.runtime_sources():
        text = _strip_go_comments(text, blank_strings=True)
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
        self._foreign = {}
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
        self._foreign = {}
        for key in BASELINE_FOREIGN_KEYS:
            if key not in data:
                # Absent is not zero: "that gate freezes nothing" and "that gate does not
                # use this file" are different states, and only one is worth reporting.
                continue
            member = data[key]
            self._foreign[key] = len(member) if isinstance(member, dict) else -1
        counts = data.get(BASELINE_CONTROLS_KEY, {})
        if not isinstance(counts, dict):
            self.baseline_error = f"'{BASELINE_CONTROLS_KEY}' is not an object"
            return {}
        return counts

    def foreign_frozen(self) -> dict:
        """How much each other enforcer freezes in this file, keyed by member.

        Members absent from the file are absent here. -1 means the member is present but
        was not a JSON object: present is the whole point, so it must not be reported as
        nothing frozen.
        """
        return getattr(self, "_foreign", {})


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
    sets, _ = load_canonical_components()
    if sets is None:
        return Finding(True, "no reference artifact available; skipped", count=0)
    schemas = ((repo.spec or {}).get("components") or {}).get("schemas") or {}
    published = {k: v for k, v in schemas.items() if k.startswith("common.v1.")}
    if not published:
        # Pre-migration service on a bespoke envelope. API-0004 already owns that failure;
        # reporting it twice would double-count the same debt in two baselines.
        return Finding(True, "no common.v1 components published; API-0004 governs adoption", count=0)
    return compare_to_accepted_sets(published, sets)


def compare_to_accepted_sets(published: dict, sets: list) -> Finding:
    """Pass when the spec reproduces ONE accepted set whole; never a per-component mix.

    Each set is judged on its own names only - a service may publish other common.v1 messages
    its DTOs reach, as it always could. Whole-set rather than per-component, because a spec
    is projected from one platform-contracts-go pin and the components point at each other:
    ErrorResponse refs ProblemDetails, which refs ErrorCode. A spec whose ErrorCode matches
    one vintage and PaginationMeta the other was built from no release at all, so it
    describes a wire no single build emits. A per-component rule would accept exactly that,
    and would also accept a spec that was only half regenerated after a bump.

    The count is the distance to the NEAREST accepted set, so the ratchet reads "components
    to fix" and a single-set reference counts exactly as it did before sets existed.
    """
    per_set = []
    for cset in sets:
        problems = {}
        for name, want in cset.components.items():
            got = published.get(name)
            if got is None:
                problems[name] = "missing"
            elif _canonical(got) != _canonical(want):
                problems[name] = "differs"
        per_set.append((cset, problems))

    single = len(sets) == 1
    label = (lambda s: s.version) if single else (lambda s: s.label)
    for cset, problems in per_set:
        if not problems:
            if single:
                return Finding(True, f"common.v1 components match the {cset.version} proto "
                                     f"projection", count=0)
            others = ", ".join(o.label for o in sets if o is not cset)
            return Finding(True, f"common.v1 components match the {cset.label} proto projection "
                                 f"({others} also accepted)", count=0)

    details, unmatched = [], False
    for name in sorted({n for s in sets for n in s.components}):
        states = [(s, p.get(name, "match")) for s, p in per_set if name in s.components]
        if all(state == "match" for _, state in states):
            continue
        holders = " and ".join(label(s) for s, _ in states)
        matched = " and ".join(label(s) for s, state in states if state == "match")
        differing = [label(s) for s, state in states if state == "differs"]
        if name not in published:
            unmatched = True
            details.append(f"{name}: missing from the spec but present in {holders}")
        elif matched:
            details.append(f"{name}: matches the {matched} proto projection but differs from "
                           f"{' and '.join(differing)}")
        else:
            unmatched = True
            plural = "s" if len(differing) > 1 else ""
            details.append(f"{name}: differs from the {' and '.join(differing)} proto "
                           f"projection{plural}")

    count = min(len(p) for _, p in per_set)
    if single:
        evidence = f"{count} component(s) stale against {sets[0].version}"
    else:
        distances = ", ".join(f"{len(p)} against {s.label}" for s, p in per_set)
        nearest = min(per_set, key=lambda sp: len(sp[1]))[0]
        if unmatched:
            evidence = (f"common.v1 components match no accepted proto projection "
                        f"({distances}; nearest is {nearest.label})")
        else:
            # Every component matches some accepted set, just not the same one.
            evidence = (f"common.v1 components mix proto vintages ({distances}): a spec must "
                        f"reproduce one accepted projection whole, so regenerate it from a "
                        f"single platform-contracts-go pin (nearest is {nearest.label})")
    return Finding(False, evidence, _capped(details), count)


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


def _foreign_owner_of(key: str) -> tuple:
    """Which other enforcer a key misfiled under `controls` belongs to.

    Shape first: ARCH-0003 is architecture-check's. Anything else that is not a control
    ID is a kebab-case ihq check name, which is the default rather than a guess, since
    `checks` is the only remaining vocabulary in this file.
    """
    for name, meta in BASELINE_FOREIGN_KEYS.items():
        shape = meta["id_shape"]
        if shape and re.fullmatch(shape, key):
            return name, meta
    return BASELINE_CHECKS_KEY, BASELINE_FOREIGN_KEYS[BASELINE_CHECKS_KEY]


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
    # A key that is not shaped like a control ID is almost certainly an ihq check name that
    # landed in the wrong member of this co-owned file. Saying "not a control in this catalog"
    # is true but dead-ends the reader, who then renames or deletes a line that another gate
    # depends on. Shape, not a copy of ihq's check list: a copy here would go stale, and the
    # run that forgot to update it is the run that needs this message.
    misfiled = [(k, *_foreign_owner_of(k)) for k in unknown
                if not re.fullmatch(r"API-\d+", k)]
    if misfiled:
        return [f"{repo.name}: {BASELINE_FILE} freezes {k} under {BASELINE_CONTROLS_KEY!r}, which "
                f"is not a control in this catalog and is not shaped like one. It belongs in the "
                f"{key!r} member of this same file, enforced by {owner['tool']}. Under "
                f"{BASELINE_CONTROLS_KEY!r} it is enforced by nothing: this gate has no such "
                f"control, and that tool does not read this member. Move the line to {key!r}, or "
                f"refreeze that gate with `{owner['refreeze']}`."
                for k, key, owner in misfiled]
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
    print(f"::group::api-contract: {repo.name} (baseline key: {BASELINE_CONTROLS_KEY!r})")
    for r in results:
        line = f"[{MARK[r['result']]}] {r['control']} [{r['severity']}] {r['title']}: {r['evidence']}"
        if r["result"] in ("frozen", "improved"):
            line += f" (baseline {r['baseline']}, now {r['count']})"
        print("  " + line)
    _render_foreign_checks(repo)
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


def _render_foreign_checks(repo: ServiceRepo) -> None:
    """Say that part of the baseline is enforced by a gate this script does not run.

    Without it, a developer whose CI failed here reads the remediation, runs the ihq
    CLI's --write-baseline -- which writes `checks` -- and fails again on the same
    control, with nothing in either output explaining why. That happened on
    inboxxhq-org-service and cost a day: pushes were blocked while the baseline the
    author had just refreshed was the wrong member of the right file.
    """
    foreign = repo.foreign_frozen()
    if not foreign:
        return
    for key, count in sorted(foreign.items()):
        tool = BASELINE_FOREIGN_KEYS[key]["tool"]
        n = ("entries this gate could not parse" if count < 0
             else f"{count} entr{'y' if count == 1 else 'ies'}")
        print(f"  note: {key!r} in {BASELINE_FILE} freezes {n} for {tool} and is NOT read here.")
    print(f"  note: this gate reads {BASELINE_CONTROLS_KEY!r} only, so lowering one member has no "
          f"effect on another.")


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


# --- the API-0007 reference artifact: floor agreement and the expand/contract window ---------

def _contracts_floor(floors_doc):
    """The platform-contracts-go floor as a string, or None when none is declared.

    Accepts both shapes check_module_pins.load_floors does: a mapping with `min`, or a bare
    version string.
    """
    floors = floors_doc.get("floors") if isinstance(floors_doc, dict) else None
    spec = floors.get(CONTRACTS_MODULE) if isinstance(floors, dict) else None
    if isinstance(spec, dict):
        spec = spec.get("min")
    return None if spec is None else str(spec)


def floor_problems(floors_doc, sets: list) -> list:
    """Reasons controls/module-floors.yaml and the reference disagree about the contract vintage.

    The floor must EQUAL the version of an accepted set that is not `next`: current, or
    `previous` during the grace after a promote. With one set that is the rule this check had
    before windows existed, floor == source.version.

    Membership, not "at most current". The floor is the version every repository is required
    to pin, so a repository pinned exactly at it has to project components some accepted set
    reproduces. A floor below the oldest set, or between two, admits a pin that produces
    neither. That stale floor is the disagreement this check exists to rule out, and it fails
    here as it always did. It follows that a window cannot close (--drop-set previous) while
    the floor still names previous: the floor moves to current first, or in the same change.

    Never `next`: the floor records the vintage the fleet has been moved to (module-floors.yaml,
    RAISING A FLOOR), and a floor there fails the pin policy for every repository the window
    exists to let bump one at a time. So a floor raised onto a promoted set comes back down in
    the change that rolls the promote back (a demote).

    The floor needs no history of its own. It can only name a set the artifact accepts, and the
    artifact is held against the last release (transition_problems), so the floor can only move
    to a vintage the fleet was measured against as current.
    """
    floor = _contracts_floor(floors_doc)
    if floor is None:
        return [f"controls/module-floors.yaml declares no floor for {CONTRACTS_MODULE}, so no "
                f"service is required to pin a vintage API-0007 accepts"]
    held = [s for s in sets if s.role != "next"]
    if any(s.version == floor for s in held):
        return []
    current = next(s for s in sets if s.role == CURRENT_SET)
    staged = next((s for s in sets if s.role == "next" and s.version == floor), None)
    if staged:
        return [f"controls/module-floors.yaml requires {CONTRACTS_MODULE} {floor}, which is the "
                f"{staged.label} set: the fleet has not been moved to it yet, and a floor there "
                f"fails every repository the window exists to let bump one at a time. Point the "
                f"floor at current {current.version}. Raise it once next is promoted "
                f"(--promote-next); a change that rolls a promote back takes the floor back with it."]
    targets = " or ".join(dict.fromkeys(s.version for s in held))
    accepts = (f"accepts only {current.label}" if len(sets) == 1
               else f"accepts {' and '.join(s.label for s in sets)}")
    floor_v = _stable_version(floor)
    held_v = [_stable_version(s.version) for s in held]
    if floor_v is None or any(v is None for v in held_v):
        why = (f"The floor must equal {targets}; versions that are not stable vX.Y.Z tags cannot "
               f"be ordered, and only equality says which set a repository pinned at the floor "
               f"produces.")
    elif floor_v > max(held_v):
        why = (f"Every repository pinned at the floor would project components API-0007 does not "
               f"accept. Point the floor at {targets}, or move the reference first: --stage-next "
               f"with emitter output for the new vintage, then --promote-next, then raise the floor.")
    elif floor_v < min(held_v):
        why = (f"The floor is stale: a repository pinned at it projects components no accepted set "
               f"reproduces, so nothing requires a vintage API-0007 accepts. Raise the floor to "
               f"{targets}"
               + (". A window closes (--drop-set previous) only with the floor on current, raised "
                  "first or in the same change." if len(sets) == 1 else "."))
    else:
        why = (f"The floor falls between the accepted sets, so a repository pinned at it projects "
               f"components none of them reproduces. Point the floor at {targets}.")
    return [f"controls/module-floors.yaml requires {CONTRACTS_MODULE} {floor}, but "
            f"controls/common-v1-components.json {accepts}. {why}"]


def _state(sets) -> str:
    return "{" + ", ".join(s.label for s in sets) + "}"


def transition_problems(base_sets, head_sets, closed_previous=None) -> tuple:
    """(problems, summary) for the change from the base's accepted sets to the head's.

    The base is the last release (resolve_event_base), because that is what @v1 consumers run
    and what one release carries them from. parse_reference judges a file on its own, so it
    accepts any `previous` older than current, whether or not the fleet was ever measured
    against it; only history knows that. A change may make exactly ONE window step:

      stage     {current}           -> {current, next}        --stage-next
      abandon   {current, next}     -> {current}              --drop-set next
      promote   {current, next}     -> {next as current, current as previous}   --promote-next
      close     {current, previous} -> {current}              --drop-set previous
      relabel   same roles, byte-identical components, provenance not moving backwards
      demote    {current, previous} -> {previous as current, current as next}   git revert of a promote
      reopen    {current}           -> {current, previous}    git revert of a close

    The two rollbacks are safe without a declaration. A demote accepts exactly the sets the base
    did, so no spec changes verdict; floor_problems then requires a floor raised onto the demoted
    set to come back to the new current in the same change. A reopen readmits only
    `closed_previous`: the `previous` set the last release before the close still carried beside
    this same current (see previous_before_close) - a projection the fleet ran as current until
    it was promoted away from, and exactly what the close dropped. Releases rather than commits,
    because commits between two releases never reached the fleet and a merge can carry any
    intermediate state.
    A current that replaced its predecessor in place, as every regeneration did before windows
    existed, left no grace set in any release, so nothing older can be reopened.

    Anything else fails. In particular: a `previous` that was neither current on the base nor
    `closed_previous` (a forged grace set, which would readmit an old projection); replacing
    current in place (regenerating the artifact over itself - a flag day for every spec, and
    during a window it also discards `next`); and two steps in one change, such as stage then
    promote, which would promote a vintage no service had the chance to adopt while it was still
    `next`. Because the base is the last release, a step that has landed on main but is not
    released yet counts toward the next change too, so a release never carries two steps.
    """
    if base_sets is None:
        return [], "the reference artifact is new in this change"
    b = {s.role: s for s in base_sets}
    h = {s.role: s for s in head_sets}
    bc, bn, bp = b[CURRENT_SET], b.get("next"), b.get("previous")
    hc, hn, hp = h[CURRENT_SET], h.get("next"), h.get("previous")
    roles_b, roles_h = set(b), set(h)

    def same(x, y) -> bool:
        return x is not None and y is not None and x.identity == y.identity

    if roles_b == roles_h and all(same(b[r], h[r]) for r in roles_b):
        return [], f"accepted sets unchanged {_state(head_sets)}"
    if roles_b == {CURRENT_SET} and roles_h == {CURRENT_SET, "next"} and same(bc, hc):
        return [], f"staged {hn.label} beside current {hc.vintage}: the window is open"
    if roles_b == {CURRENT_SET, "next"} and roles_h == {CURRENT_SET} and same(bc, hc):
        return [], f"dropped {bn.label}: the window is abandoned"
    if (roles_b == {CURRENT_SET, "next"} and roles_h == {CURRENT_SET, "previous"}
            and same(bn, hc) and same(bc, hp)):
        return [], f"promoted next to current {hc.vintage}; {hp.label} stays accepted"
    if roles_b == {CURRENT_SET, "previous"} and roles_h == {CURRENT_SET} and same(bc, hc):
        return [], f"dropped {bp.label}: the window is closed"
    if (roles_b == {CURRENT_SET, "previous"} and roles_h == {CURRENT_SET, "next"}
            and same(bp, hc) and same(bc, hn)):
        return [], (f"demoted current {bc.vintage} back to next beside {hc.vintage}: a promote rolled "
                    f"back, accepting the same two sets")
    if (roles_b == {CURRENT_SET} and roles_h == {CURRENT_SET, "previous"} and same(bc, hc)
            and same(hp, closed_previous)):
        return [], (f"reopened {hp.label}, which the last release before the close carried beside "
                    f"{bc.vintage}: a close rolled back")

    if roles_b == roles_h and all(_canonical(b[r].components) == _canonical(h[r].components)
                                  for r in roles_b):
        problems, relabelled = [], []
        for role in sorted(roles_b):
            old, new = b[role], h[role]
            if old.identity == new.identity:
                continue
            relabelled.append(f"{old.label} -> {new.vintage}")
            if old.module != new.module:
                problems.append(f"{old.label} changes module {old.module} -> {new.module}")
            for what, was, now in (("version", old.version, new.version),
                                   ("projector", old.projector_version, new.projector_version)):
                if was is None or _stable_version(was) is None:
                    continue  # nothing recorded to go backwards from
                if now is None or _stable_version(now) is None:
                    problems.append(f"{old.label} loses its recorded {what} {was}; regenerate with "
                                    f"an emitter that records it")
                elif _stable_version(now) < _stable_version(was):
                    problems.append(f"{old.label} relabels its {what} backwards, {was} -> {now}")
        if not problems:
            return [], f"relabelled {', '.join(relabelled)} with components unchanged"
        return [f"controls/common-v1-components.json relabels a set with unchanged components, "
                f"but {'; '.join(problems)}. A relabel corrects provenance and never moves it "
                f"backwards."], ""

    hints = []
    if hp is not None and not same(hp, bp) and not same(hp, bc):
        if roles_b == {CURRENT_SET} and same(hc, bc):
            earlier = (f"the last release before the close carried previous {closed_previous.vintage}"
                       if closed_previous is not None
                       else f"no release since {bc.vintage} became current carried a previous set")
            hints.append(f"{hp.label} is not the set a close could have dropped ({earlier}), so "
                         f"reopening it would readmit a projection no window step left accepted; only "
                         f"a git revert of the close that dropped a grace set reopens it")
        else:
            hints.append(f"{hp.label} was not the current set on the base, so it would readmit a "
                         f"projection the fleet was never measured against as current")
    if not same(hc, bc):
        if same(hc, bn):
            if hp is None:
                hints.append(f"next {bn.vintage} became current without keeping {bc.vintage} as "
                             f"previous, which is promote and close in one change")
        elif same(hc, bp):
            if not any(same(bc, s) for s in (hn, hp)):
                hints.append(f"current was rolled back to previous {bp.vintage} without keeping "
                             f"{bc.vintage} accepted, which fails every spec already on it; a promote "
                             f"is rolled back by demoting {bc.vintage} to next (git revert of the "
                             f"promote)")
        elif same(hp, bc):
            hints.append(f"{hc.vintage} became current although the base never accepted it as next, "
                         f"which is stage and promote in one change: no service could adopt it "
                         f"before it was promoted")
        else:
            hints.append(f"current was replaced in place ({bc.vintage} -> {hc.vintage}): regenerating "
                         f"the artifact over itself is a flag day for every spec that publishes "
                         f"common.v1 components; stage the emitter output with --stage-next instead")
    if hn is not None and bp is not None:
        hints.append("`previous` was dropped and `next` staged in one change")
    if hn is not None and bn is not None and not same(hn, bn):
        hints.append(f"next was replaced ({bn.vintage} -> {hn.vintage}); drop it with --drop-set "
                     f"next, then stage the new one")
    return [f"controls/common-v1-components.json goes from {_state(base_sets)} to "
            f"{_state(head_sets)}, which is not one window step"
            + (f": {'; '.join(hints)}" if hints else "")
            + ". One change may stage next (--stage-next, when no extra set is open), abandon it "
              "(--drop-set next), promote it (--promote-next), close the window (--drop-set "
              "previous), relabel a set whose components are unchanged, or roll back a promote or "
              "a close with git revert (README, \"Moving the reference\")."], ""


def _git_out(repo: Path, *args: str) -> tuple:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def release_tags(repo: Path, merged_into: str | None = None) -> list:
    """This repository's release tags (vX.Y.Z, as release.yaml cuts them), newest first.

    With `merged_into`, only the tags that commit contains.
    """
    args = ["tag", "--list", "v*"] + (["--merged", merged_into] if merged_into else [])
    rc, out, _ = _git_out(repo, *args)
    tags = [t for t in out.split() if STABLE_VERSION_RE.match(t)] if rc == 0 else []
    return sorted(tags, key=_stable_version, reverse=True)


def resolve_event_base(repo: Path, event_name: str, event: dict) -> tuple:
    """(base commit or None, reason, whether a base is required) for a GitHub event.

    The base is the newest release tag HEAD contains, whatever the event. release.yaml moves @v1
    onto every commit that lands on main once CI passes for THAT commit, and nothing requires CI
    to pass on main before the next commit lands. Held against its own before-SHA, a commit that
    turned CI red once would be released inside the next, unrelated commit, which reads as
    "unchanged". Held against the last release, an unreleased forbidden change stays red on every
    later commit until it is fixed, and a step that landed but is not released yet counts
    toward the next change, so no release carries two steps. release.yaml runs the same check
    against the tag it releases from, so a red commit on main cannot reach @v1 either way.

    A pull request is judged the same way: its checkout is the merge commit, which contains
    main and so main's last release. That also lets a revert of an unreleased forbidden change
    pass, where judging it against pull_request.base.sha would read the revert itself as a
    forbidden step.

    Only a clone with no release tag in HEAD's history (a test fixture) falls back to the
    event: pull_request.base.sha, a push's before-SHA, or the merge base with the default
    branch for a branch's first push. Not diffrange.resolve_range, whose pull request MERGE
    BASE would charge window steps main made since the branch point to this change.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from diffrange import ensure_commit, is_ancestor  # noqa: E402 - only this path needs git

    if event_name == "push" and event.get("deleted"):
        return None, "a branch deletion changes nothing", False
    if not release_tags(repo) and _git_out(repo, "remote", "get-url", "origin")[0] == 0:
        _git_out(repo, "fetch", "--quiet", "--tags", "origin")  # a clone made without tags
    contained = release_tags(repo, merged_into="HEAD")
    if contained:
        newest = release_tags(repo)[0]
        why = f"the last release {contained[0]}"
        if newest != contained[0]:
            why += f" this ref contains (the newest release, {newest}, is not in it)"
        return contained[0], why, True

    if event_name in ("pull_request", "pull_request_target"):
        base = str(((event.get("pull_request") or {}).get("base") or {}).get("sha") or "")
        if not base:
            return None, "the pull_request payload carries no base SHA", True
        if not ensure_commit(repo, base):
            return None, f"pull request base {base[:12]} is not in this clone", True
        return base, f"pull request base {base[:12]}", True
    if event_name == "push":
        before = str(event.get("before") or "")
        if (before and set(before) != {"0"} and not event.get("created") and not event.get("forced")
                and ensure_commit(repo, before) and is_ancestor(repo, before, "HEAD")):
            return before, f"push before-SHA {before[:12]}", True
        # The first push of a branch or a force push has no usable before-SHA. What such a branch
        # would change is what it holds against the default branch - unless it IS the default
        # branch, whose merge base with itself is itself and would check nothing.
        default = str((event.get("repository") or {}).get("default_branch") or "main")
        if event.get("ref") == f"refs/heads/{default}":
            return None, (f"a push to {default} without a usable before-SHA (created or forced) "
                          f"cannot be held against anything"), True
        ref = f"refs/remotes/origin/{default}"
        if _git_out(repo, "rev-parse", "--verify", "-q", ref)[0] != 0:
            _git_out(repo, "fetch", "--no-tags", "--quiet", "origin", f"+refs/heads/{default}:{ref}")
        rc, out, _ = _git_out(repo, "merge-base", ref, "HEAD")
        if rc == 0 and out.strip():
            return out.strip(), f"merge base {out.strip()[:12]} with origin/{default}", True
        return None, f"no usable before-SHA and no merge base with origin/{default}", True
    return None, f"a {event_name or 'local'} run describes no change to hold against a base", False


def _in_repo(path: Path) -> tuple:
    """(work tree root, path relative to it, error or None) for a file checked out in a git repository.

    Every git call below runs from the root: a pathspec is relative to the working directory,
    and `rev-list -- controls/x.json` run from controls/ matches nothing.
    """
    parent = Path(path).resolve().parent
    rc, top, err = _git_out(parent, "rev-parse", "--show-toplevel")
    if rc != 0:
        return parent, None, f"{parent} is not a git repository ({err.strip()})"
    root = Path(top.strip()).resolve()
    try:
        return root, Path(path).resolve().relative_to(root).as_posix(), None
    except ValueError:
        return root, None, f"{path} is not inside the git repository at {root}"


def _show(repo: Path, rev: str, rel: str):
    """The file's text at `rev`, or None when that commit does not have it."""
    if _git_out(repo, "cat-file", "-e", f"{rev}:{rel}")[0] != 0:
        return None
    rc, text, _ = _git_out(repo, "show", f"{rev}:{rel}")
    return text if rc == 0 else None


def _reference_sets(text):
    try:
        return parse_reference(json.loads(text))[0]
    except json.JSONDecodeError:
        return None


def last_usable_state(path: Path, base: str, parse) -> tuple:
    """(parsed, the commit it was read at, error) for the newest state of `path` at or before `base`
    that `parse` accepts; (None, None, None) when its history has none.

    A base where the file is missing or unusable is not a clean slate. Otherwise deleting the
    artifact (or breaking it) in one commit and adding a forged one in the next would read as
    "new in this change", and the base could not be held against at all. A release never holds
    an unusable artifact - CI and release.yaml both refuse one - so this matters for the event
    fallbacks, and walks back only when the base itself is unusable.
    """
    repo, rel, err = _in_repo(path)
    if err:
        return None, None, err
    if _git_out(repo, "cat-file", "-e", f"{base}^{{commit}}")[0] != 0:
        return None, None, f"base {base} is not a commit in this clone"
    # The base first, then each commit that changed the file, newest first: together those are
    # every distinct state the file had in the base's history.
    _, touched, _ = _git_out(repo, "rev-list", "--max-count=500", base, "--", rel)
    for rev in [base, *touched.split()]:
        text = _show(repo, rev, rel)
        parsed = None if text is None else parse(text)
        if parsed is not None:
            return parsed, rev, None
    return None, None, None


def previous_before_close(path: Path, base: str, current) -> tuple:
    """(set, release tag) for the `previous` a close dropped: the grace set of the newest release
    at or before `base` that is not simply `current` alone; (None, None) when that release was
    no grace state beside the same current, or when there is none.

    Releases are walked newest first, skipping those that hold `current`'s components with no
    `previous` (the closed state itself, a relabel of it, a window opened and abandoned since).
    The first other release is the one before the close, and only a grace state there -
    {current with these components, previous} - names something a close dropped. A current that
    arrived by replacing its predecessor in place left no such release, so an older projection
    cannot be readmitted by copying it from history. Releases, not commits: release.yaml releases
    a commit only through this check, while commits between two releases - or inside a merge - can
    hold any intermediate state and never reached @v1.
    """
    repo, rel, err = _in_repo(path)
    tags = [] if err else release_tags(repo, merged_into=base)
    if not tags:
        return None, None
    # One process resolves every release's blob; most releases share one, and only the
    # distinct blobs are read and parsed.
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname)"],
                          input="".join(f"{tag}:{rel}\n" for tag in tags),
                          capture_output=True, text=True)
    blobs = proc.stdout.splitlines() if proc.returncode == 0 else []
    parsed = {}
    for tag, blob in zip(tags, blobs):
        if blob.endswith(" missing"):
            return None, None  # before the artifact existed
        if blob not in parsed:
            rc, text, _ = _git_out(repo, "cat-file", "blob", blob)
            parsed[blob] = _reference_sets(text) if rc == 0 else None
        sets = parsed[blob]
        if sets is None:
            continue
        roles = {s.role: s for s in sets}
        same_current = _canonical(roles[CURRENT_SET].components) == _canonical(current.components)
        if same_current and "previous" not in roles:
            continue
        return (roles["previous"], tag) if same_current else (None, None)
    return None, None


def verify_transition(sets: list, base: str) -> int:
    base_sets, ref_rev, err = last_usable_state(Path(reference_path), base, _reference_sets)
    if err:
        print(f"::error::cannot read the base commit: {err}")
        return 2
    if ref_rev is not None and ref_rev != base:
        print(f"::notice::{base} has no usable reference artifact; holding this change against its "
              f"last usable state, at {ref_rev[:12]}")

    closed = None
    if (base_sets is not None and len(base_sets) == 1
            and any(s.role == "previous" for s in sets)):
        closed, tag = previous_before_close(Path(reference_path), base, base_sets[0])
        if closed is not None:
            print(f"the last release before the close ({tag}) carried previous {closed.vintage} "
                  f"beside current {base_sets[0].vintage}")
    problems, summary = transition_problems(base_sets, sets, closed)
    if summary:
        print(f"reference artifact since {base[:12]}: {summary}")

    for msg in problems:
        print(f"::error::{msg}")
    repo, _, _ = _in_repo(Path(reference_path))
    if problems and base in release_tags(repo):
        print(f"::notice::{base} is the last release, and every change since it counts toward this "
              f"one, because one release carries them all to @v1. If an earlier window step has "
              f"landed but is not released yet, wait for its release (re-run CI and Release for that "
              f"commit if they were cancelled), then re-run this job.")
    return 1 if problems else 0


def verify_floor(floors_path: Path, base: str | None = None, base_from_event: bool = False) -> int:
    sets, reason = load_canonical_components()
    if sets is None:
        print(f"::error::{reference_path} {reason}")
        return 2
    try:
        floors_doc = yaml.safe_load(Path(floors_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"::error::cannot read {floors_path}: {exc}")
        return 2
    problems = floor_problems(floors_doc, sets)
    for p in problems:
        print(f"::error::{p}")
    rc = 1 if problems else 0
    if not problems:
        floor = _contracts_floor(floors_doc)
        current = next(s for s in sets if s.role == CURRENT_SET)
        if floor == current.version:
            others = [s.label for s in sets if s is not current]
            print(f"floor and reference artifact agree on {floor}"
                  + (f" ({', '.join(others)} also accepted)" if others else ""))
        else:
            named = next(s for s in sets if s.role == "previous" and s.version == floor)
            print(f"floor and reference artifact agree on {floor}, the {named.label} set ({current.label} "
                  f"also accepted); the floor moves to {current.version} before or with --drop-set previous")

    if base_from_event:
        event = {}
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path:
            try:
                event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"::error::cannot read the event payload at {event_path}: {exc}")
                return 2
        base, why, required = resolve_event_base(Path(reference_path).resolve().parent,
                                                 os.environ.get("GITHUB_EVENT_NAME", ""), event)
        if base is None:
            if required:
                # Fail closed: a transition gate that cannot find its base must not pass.
                print(f"::error::cannot resolve the base to hold this change against: {why}")
                return 2
            print(f"::notice::{why}; only the floor rule above applies")
            return rc
        print(f"holding the reference artifact against {why}")
    if base:
        rc = max(rc, verify_transition(sets, base))
    return rc


def _reference_json(doc: dict) -> str:
    # Byte-identical to platform-shared-go's openapicontract.CanonicalJSON for this document:
    # sorted keys, 2-space indent, no HTML escaping, trailing newline. So a window that is
    # opened, promoted and closed leaves exactly the bytes the emitter prints for the new vintage.
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_reference(doc: dict, what: str) -> int:
    sets, reason = parse_reference(doc)
    if sets is None:
        print(f"::error::refusing to write {reference_path}: the result {reason}")
        return 1
    Path(reference_path).write_text(_reference_json(doc), encoding="utf-8")
    _canonical_cache.pop(str(reference_path), None)
    print(f"api-contract: {what}; {reference_path} now accepts "
          f"{' and '.join(s.label for s in sets)}")
    return 0


def _read_reference(path: Path) -> tuple:
    """(document, None) for a file that is a valid reference on its own, else (None, reason).

    Every edit starts from a valid file: rewriting an invalid one could launder it, since the
    result is only checked for being valid, not for being what the input meant.
    """
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path} is missing or unreadable ({exc})"
    _, reason = parse_reference(doc)
    return (None, f"{path} {reason}") if reason else (doc, None)


def stage_next(emitted_path: Path) -> int:
    """Open a window: accept the emitter's output for a newer vintage beside current."""
    doc, err = _read_reference(reference_path)
    emitted, err2 = _read_reference(emitted_path)
    if err or err2:
        print(f"::error::{err or err2}")
        return 2
    open_sets = [role for role in EXTRA_SET_ROLES if role in doc]
    if open_sets:
        print(f"::error::{reference_path} already carries `{open_sets[0]}`. Finish that window "
              f"first: --drop-set previous after the fleet moved, or --drop-set next to abandon it.")
        return 1
    if any(role in emitted for role in EXTRA_SET_ROLES):
        print(f"::error::{emitted_path} carries an accepted-set member; stage raw "
              f"emit-canonical-components output, not another reference file.")
        return 1
    staged = dict(doc, next=_set_body(emitted))
    return _write_reference(staged, f"staged next from {emitted_path}")


def _set_body(doc: dict) -> dict:
    """A set as the emitter printed it: components, source and, when it printed one, _comment.

    The comment travels with its set, so a promote puts the new vintage's comment on top and a
    closed window is still byte-identical to the emitter's output after the emitter's comment
    text changes between the two releases.
    """
    return {k: doc[k] for k in SET_KEYS if k in doc}


def promote_next() -> int:
    """Close the expand phase: next becomes current, the old current stays accepted as previous."""
    doc, err = _read_reference(reference_path)
    if err:
        print(f"::error::{err}")
        return 2
    if "next" not in doc:
        print(f"::error::{reference_path} carries no `next` set to promote.")
        return 1
    promoted = {k: v for k, v in doc.items() if k not in (*SET_KEYS, "next")}
    promoted.update(doc["next"], previous=_set_body(doc))
    return _write_reference(promoted, "promoted next to current")


def drop_set(role: str) -> int:
    """Drop `previous` to finish a window, or `next` to abandon one."""
    doc, err = _read_reference(reference_path)
    if err:
        print(f"::error::{err}")
        return 2
    if role not in doc:
        print(f"::error::{reference_path} carries no `{role}` set to drop.")
        return 1
    return _write_reference({k: v for k, v in doc.items() if k != role}, f"dropped {role}")


def main(argv: list) -> int:
    global reference_path
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
    ref = ap.add_argument_group(
        "API-0007 reference artifact",
        "The accepted common.v1 component sets. --verify-floor, --stage-next, --promote-next "
        "and --drop-set act on --components and exit.")
    ref.add_argument("--components", metavar="FILE", default=str(CANONICAL_COMPONENTS),
                     help="reference artifact API-0007 compares against (default: %(default)s)")
    ref.add_argument("--verify-floor", metavar="FLOORS",
                     help="fail unless FLOORS' platform-contracts-go floor equals the version of "
                          "the current or previous set (never next)")
    base = ref.add_mutually_exclusive_group()
    base.add_argument("--base", metavar="REF",
                      help="with --verify-floor: also fail unless the artifact moved from REF by "
                           "one window step (release.yaml passes the tag it releases from)")
    base.add_argument("--base-from-event", action="store_true",
                      help="like --base, with REF the newest release tag HEAD contains; only in a "
                           "clone with no release tag, the event in GITHUB_EVENT_NAME and "
                           "GITHUB_EVENT_PATH (pull request base, push before-SHA), and an event "
                           "describing no change checks only the floor rule")
    ref.add_argument("--stage-next", metavar="EMITTED",
                     help="accept EMITTED (raw emit-canonical-components output for a newer "
                          "vintage) as the next set beside current")
    ref.add_argument("--promote-next", action="store_true",
                     help="make next the current set, keeping the old current accepted as previous")
    ref.add_argument("--drop-set", choices=EXTRA_SET_ROLES,
                     help="stop accepting previous (window finished) or next (window abandoned)")
    args = ap.parse_args(argv)
    reference_path = Path(args.components)
    if (args.base or args.base_from_event) and not args.verify_floor:
        ap.error("--base and --base-from-event only apply to --verify-floor")

    if args.verify_floor:
        return verify_floor(Path(args.verify_floor), args.base, args.base_from_event)
    if args.stage_next:
        return stage_next(Path(args.stage_next))
    if args.promote_next:
        return promote_next()
    if args.drop_set:
        return drop_set(args.drop_set)

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

    # Refuse to run a control whose reference artifact is missing or malformed rather than let
    # it report a pass it never actually checked.
    if any(c.get("detector") == "canonical_components_current" for c in controls):
        sets, reason = load_canonical_components()
        if sets is None:
            print(f"::error::API-0007 is enabled but {reference_path} {reason}. Check out this "
                  "repository's controls/ directory, or regenerate the artifact with `go run "
                  "github.com/coderaxis/platform-shared-go/platform/openapicontract/commonv1policy/"
                  "cmd/emit-canonical-components@<tag>`; change which sets it accepts only with "
                  "--stage-next, --promote-next or --drop-set.")
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
