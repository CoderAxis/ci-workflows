#!/usr/bin/env python3
"""Org-versus-tenant vocabulary gate: "org" everywhere, "tenant" only where
explicitly declared with a documented reason.

The policy is documented in data-scope-classification-standard.md but nothing
verified it until now. This is the verifier following the same pattern as the
UUID version policy gate: canonical choice required everywhere, deviation 
possible but never silent or accidental.

Every occurrence of "tenant" must be either fixed or explicitly declared in
controls/org-vocabulary.yaml with a stated reason. Default is failure.

Exit codes follow the house convention:
  0  clean, or no files to scan
  1  at least one finding from a control at stage `enforce`
  2  the checker could not do its job (missing catalog, unparseable config, etc.)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyYAML required: python3 -m pip install PyYAML") from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTROL = REPO_ROOT / "controls" / "org-vocabulary.yaml"

REQUIRED_CONTROL_FIELDS = (
    "id", "title", "owner", "scope", "status", "severity", "stage",
    "policy", "rationale", "remediation", "detector", "refs",
)
VALID_SEVERITY = {"critical", "major", "minor"}
VALID_STAGE = {"enforce", "warn", "observe"}
VALID_STATUS = {"active", "deprecated", "superseded"}

# File extensions to scan for text content
TEXT_EXTENSIONS = {
    '.py', '.go', '.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.rb', '.php',
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.cs', '.vb', '.rs', '.kt', 
    '.swift', '.m', '.mm', '.pl', '.sh', '.bash', '.zsh', '.fish', '.ps1',
    '.sql', '.yaml', '.yml', '.json', '.xml', '.html', '.htm', '.css', '.scss',
    '.sass', '.less', '.md', '.rst', '.txt', '.cfg', '.conf', '.ini', '.toml',
    '.dockerfile', '.tf', '.hcl', '.proto', '.thrift', '.avro', '.jsonnet',
    '.makefile', '.mk', '.cmake', '.gradle', '.sbt', '.pom'
}

# Binary/generated file patterns to skip
SKIP_PATTERNS = [
    '*.pb.go',      # protobuf generated
    '*.pb.cc',      # protobuf generated 
    '*.pb.h',       # protobuf generated
    '*.generated.*', # general generated marker
    '*_generated.*', # underscore generated marker
    '*.min.js',     # minified JavaScript
    '*.min.css',    # minified CSS
    '*.bundle.*',   # bundled assets
    'node_modules/**',
    'vendor/**',
    '.git/**',
    'dist/**',
    'build/**',
    '.terraform/**',
    '__pycache__/**',
    '*.pyc',
    '*.pyo',
    '*.class',
    '*.jar',
    '*.war',
    '*.ear',
    '*.exe',
    '*.dll',
    '*.so',
    '*.dylib',
    '*.a',
    '*.lib',
    'testdata/**'
]


def cannot_run(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"::error::{message}")
    sys.exit(2)


@dataclass
class Finding:
    control: str
    severity: str
    stage: str
    file: str
    line: int
    column: int
    match: str
    message: str
    remediation: str = ""
    refs: list = field(default_factory=list)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}:{self.column}" if self.file else "(repository)"


@dataclass 
class Exception:
    path: str
    pattern: str | None
    reason: str

    def matches(self, file_path: str, text: str) -> bool:
        """Check if this exception covers the given file and text."""
        # Check path pattern match - use Unix-style path separators
        normalized_path = file_path.replace('\\', '/')
        if not fnmatch.fnmatch(normalized_path, self.path):
            return False
        
        # If no specific pattern, the path match is sufficient
        if self.pattern is None:
            return True
            
        # Check if the pattern matches the text (case-insensitive)
        try:
            return bool(re.search(self.pattern, text, re.IGNORECASE))
        except re.error:
            return False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(path: Path) -> dict:
    if not path.exists():
        cannot_run(f"org-vocabulary control catalog not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        cannot_run(f"cannot read the org-vocabulary catalog at {path}: {exc}")
    if not isinstance(doc, dict):
        cannot_run(f"{path}: expected a mapping at the top level")

    errors: list[str] = []
    seen: set[str] = set()
    controls = doc.get("controls")
    if not isinstance(controls, list) or not controls:
        cannot_run(f"{path}: invalid catalog (expected a non-empty 'controls:' list)")
    for i, c in enumerate(controls):
        cid = (c or {}).get("id", f"#{i}")
        missing = [f for f in REQUIRED_CONTROL_FIELDS if not c.get(f)]
        if missing:
            errors.append(f"{cid}: missing required field(s): {', '.join(missing)}")
        if c.get("severity") not in VALID_SEVERITY:
            errors.append(f"{cid}: severity must be one of {sorted(VALID_SEVERITY)}")
        if c.get("stage") not in VALID_STAGE:
            errors.append(f"{cid}: stage must be one of {sorted(VALID_STAGE)}")
        if c.get("status") not in VALID_STATUS:
            errors.append(f"{cid}: status must be one of {sorted(VALID_STATUS)}")
        if cid in seen:
            errors.append(f"{cid}: duplicate control id")
        seen.add(cid)

    # Validate exceptions structure
    exceptions = doc.get("exceptions", [])
    if not isinstance(exceptions, list):
        errors.append("'exceptions' must be a list")
    else:
        for i, exc in enumerate(exceptions):
            if not isinstance(exc, dict):
                errors.append(f"exception #{i}: must be a mapping")
                continue
            if not exc.get("path"):
                errors.append(f"exception #{i}: missing required 'path' field")
            if not exc.get("reason"):
                errors.append(f"exception #{i}: missing required 'reason' field")

    if errors:
        for e in errors:
            print(f"::error::org-vocabulary catalog invalid: {e}")
        sys.exit(2)
    return doc


def controls_by_id(doc: dict) -> dict:
    return {c["id"]: c for c in doc["controls"]}


# ---------------------------------------------------------------------------
# Scanner exclusions (prevent reading our own checkout)
# ---------------------------------------------------------------------------

def _within(path: Path, base: Path) -> bool:
    """Check if path is within base directory."""
    return path == base or base in path.parents


def excluded_paths(root: Path) -> list[Path]:
    """Directories under `root` that must not be scanned.

    This repository, when CI has checked it out INSIDE the tree under test. The
    reusable workflow does exactly that - `actions/checkout` cannot place a
    repository outside the workspace - so a walk of the caller's whole repository
    also walks the checker's own tree, including controls files that contain
    "tenant" in policy documentation.

    A computed path rather than a directory name in the catalog's `skip_dirs`.
    The name is the workflow's choice and differs per job (`.org-tools` today,
    but jobs rename), so a hardcoded name is stale the moment a job is renamed.
    `REPO_ROOT` is always this very checkout wherever it lives, so it excludes
    exactly the right directory without knowing its name. Same instrument as
    check-uuid-version-policy.py and check-ci-identity.py; same reason.

    Empty when the scanned root is inside this repository: that is how the
    self-test runs, and there the violations ARE the subject.
    """
    if _within(root, REPO_ROOT):
        return []
    return [REPO_ROOT] if _within(REPO_ROOT, root) else []


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def should_skip_file(file_path: Path, skip_patterns: list[str]) -> bool:
    """Check if file should be skipped based on patterns."""
    path_str = str(file_path)
    
    # Check against skip patterns
    for pattern in skip_patterns:
        if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(file_path.name, pattern):
            return True
    
    # Skip if not a text file extension and no extension is explicitly allowed
    if file_path.suffix and file_path.suffix.lower() not in TEXT_EXTENSIONS:
        # Special case: files without extensions might be scripts
        if file_path.suffix == '':
            # Check if it looks like a text file by reading first few bytes
            try:
                with file_path.open('rb') as f:
                    sample = f.read(512)
                    # Simple heuristic: if it's mostly printable ASCII, treat as text
                    text_chars = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
                    return text_chars < 0.8 * len(sample) if sample else True
            except (OSError, PermissionError):
                return True
        else:
            return True
    
    return False


def find_files_to_scan(root: Path, skip_dirs: list[str], excluded: list[Path]) -> Iterator[Path]:
    """Find all files that should be scanned for tenant usage."""
    all_skip_patterns = SKIP_PATTERNS + [f"{d}/**" for d in skip_dirs]
    
    for path in root.rglob("*"):
        if not path.is_file():
            continue
            
        # Skip if in excluded directories
        if any(_within(path, ex) for ex in excluded):
            continue
            
        # Convert to relative path for pattern matching
        try:
            rel_path = path.relative_to(root)
        except ValueError:
            continue
            
        if should_skip_file(rel_path, all_skip_patterns):
            continue
            
        yield path


# ---------------------------------------------------------------------------
# ORG-0002: identifiers only
# ---------------------------------------------------------------------------
# ORG-0001 above matches the word anywhere, prose included, which is why it is
# at `warn` and cannot be promoted: the standard that declares "tenant" legacy
# has to say the word to say what it is deprecating. ORG-0002 is the narrow
# control that CAN reach enforce, and it earns that by answering a different
# question - is there an IDENTIFIER named after the legacy term - rather than by
# exempting the documentation that ORG-0001 trips over.

# Only code. No .md, .rst or .txt: prose is ORG-0001's subject, and re-reading it
# here under a stricter stage is how a narrow control becomes a broad one.
IDENTIFIER_EXTENSIONS = {
    '.go', '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.scala', '.rb', '.php',
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.cs', '.rs', '.kt', '.swift',
    '.sh', '.bash', '.zsh', '.sql', '.yaml', '.yml', '.json', '.tf', '.hcl',
    '.proto', '.tmpl',
}

# Four case shapes, each anchored on a NON-WORD character before "tenant".
#
# The anchor is the whole design. `Tenant[A-Z]` without it matches
# TestSharedSpecIsTenantNeutral and TestCrossTenantIsolation - Go test names
# that use the word as prose in a sentence, which is precisely what this control
# must not re-flag. With the anchor, "Tenant" has to START the identifier, so
# `TenantID` matches and `IsTenantNeutral` does not. The same anchor is what
# separates `tenant_id` from `resolve_tenant_id_from_header`... which is a
# deliberate omission: an identifier whose LEADING word is the legacy term is a
# declaration of a tenant-shaped thing, while one that merely contains it is a
# description, and only the first is a structural fact about the data model.
IDENTIFIER_PATTERNS = (
    ("snake_case", re.compile(r'(?<![A-Za-z0-9_])tenant_[a-z0-9]+(?:_[a-z0-9]+)*')),
    ("lowerCamelCase", re.compile(r'(?<![A-Za-z0-9_])tenant[A-Z][A-Za-z0-9]*')),
    ("UpperCamelCase", re.compile(r'(?<![A-Za-z0-9_])Tenant[A-Z][A-Za-z0-9]*')),
    ("SCREAMING_SNAKE_CASE", re.compile(r'(?<![A-Za-z0-9_])TENANT_[A-Z0-9]+(?:_[A-Z0-9]+)*')),
)

LINE_COMMENT_TOKENS = {
    '.go': ("//",), '.js': ("//",), '.ts': ("//",), '.jsx': ("//",), '.tsx': ("//",),
    '.java': ("//",), '.scala': ("//",), '.c': ("//",), '.cpp': ("//",), '.cc': ("//",),
    '.cxx': ("//",), '.h': ("//",), '.hpp': ("//",), '.cs': ("//",), '.rs': ("//",),
    '.kt': ("//",), '.swift': ("//",), '.php': ("//", "#"), '.proto': ("//",),
    '.py': ("#",), '.rb': ("#",), '.sh': ("#",), '.bash': ("#",), '.zsh': ("#",),
    '.yaml': ("#",), '.yml': ("#",), '.tf': ("#", "//"), '.hcl': ("#", "//"),
    '.tmpl': ("//",), '.sql': ("--",), '.json': (),
}


def mask_non_code(text: str, suffix: str) -> str:
    """Blank out comments and string literals, preserving line and column layout.

    Without this the control would report a documentation FILENAME as an
    identifier. Measured 2026-08-12: 92 of the 116 fleet-wide matches for these
    patterns are the literal 'docs/workflows/TENANT_ONBOARDING_FLOW.md' inside
    the legacy per-repo scripts/validate_docs.py - a string naming a markdown
    file, in 92 copies of a script central CI already absorbed. A further three
    are the words `tenant_role_bindings` written in a comment explaining a seed
    step. None of the 95 declares anything, and a control that reported them
    would be ORG-0001 with extra steps.

    Deliberately a lexer-shaped approximation rather than a parser. It has to
    serve seven language families at once, and the failure direction is safe:
    mis-masking hides a finding from an advisory control, it cannot invent one.
    """
    line_tokens = LINE_COMMENT_TOKENS.get(suffix, ("#", "//"))
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None       # the delimiter we are inside, if any
    in_block = False               # inside /* ... */
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append("\n")
            i += 1
            continue
        if in_block:
            if text.startswith("*/", i):
                in_block = False
                out.append("  ")
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if quote is not None:
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                out.append("  ")
                i += 2
                continue
            if text.startswith(quote, i):
                out.append(" " * len(quote))
                i += len(quote)
                quote = None
                continue
            out.append(" ")
            i += 1
            continue
        # Not in a string or block comment.
        if suffix != ".json" and text.startswith("/*", i):
            in_block = True
            out.append("  ")
            i += 2
            continue
        started = False
        for tok in line_tokens:
            if text.startswith(tok, i):
                end = text.find("\n", i)
                end = n if end == -1 else end
                out.append(" " * (end - i))
                i = end
                started = True
                break
        if started:
            continue
        for delim in ('"""', "'''", '"', "'", "`"):
            # Triple quotes are Python's docstrings, which is where a self-test
            # describing `tenantID` lives; backticks are Go raw strings and JS
            # templates. All three span lines, so they are handled by the same
            # state as ordinary quotes rather than line by line.
            if delim in ('"""', "'''") and suffix != ".py":
                continue
            if text.startswith(delim, i):
                quote = delim
                out.append(" " * len(delim))
                i += len(delim)
                started = True
                break
        if started:
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def scan_file_for_identifiers(file_path: Path, root: Path,
                              exceptions: list[Exception]) -> list[Finding]:
    """ORG-0002: identifiers named after the legacy term, in code only."""
    if file_path.suffix.lower() not in IDENTIFIER_EXTENSIONS:
        return []
    try:
        text = file_path.read_text(encoding='utf-8', errors='replace')
    except (OSError, UnicodeDecodeError):
        return []
    if "tenant" not in text.lower():
        return []

    rel_path = str(file_path.relative_to(root))
    for exc in exceptions:
        if exc.matches(rel_path, text):
            return []

    findings: list[Finding] = []
    for line_num, line in enumerate(mask_non_code(text, file_path.suffix.lower()).splitlines(), 1):
        for shape, rx in IDENTIFIER_PATTERNS:
            for match in rx.finditer(line):
                findings.append(Finding(
                    control="ORG-0002",
                    severity="major",
                    stage="enforce",
                    file=rel_path,
                    line=line_num,
                    column=match.start() + 1,
                    match=match.group(),
                    message=(f"{shape} identifier '{match.group()}' is named after the "
                             f"legacy scope term; the canonical term is 'org'"),
                ))
    return findings


def scan_file_for_tenant(file_path: Path, root: Path, exceptions: list[Exception]) -> list[Finding]:
    """Scan a single file for 'tenant' usage and return findings."""
    try:
        text = file_path.read_text(encoding='utf-8', errors='replace')
    except (OSError, UnicodeDecodeError):
        return []
    
    findings = []
    rel_path = str(file_path.relative_to(root))
    
    # Check all exceptions first to see if this file/content is allowed
    for exc in exceptions:
        if exc.matches(rel_path, text):
            return []  # File is excepted, no findings
    
    # Search for "tenant" case-insensitively
    lines = text.splitlines()
    for line_num, line in enumerate(lines, 1):
        # Find all occurrences of "tenant" in the line (case-insensitive)
        for match in re.finditer(r'tenant', line, re.IGNORECASE):
            column = match.start() + 1
            
            # Check if this specific match is covered by an exception with a pattern
            match_text = line[max(0, match.start()-20):match.end()+20]  # Context around match
            is_excepted = False
            for exc in exceptions:
                if exc.pattern and fnmatch.fnmatch(rel_path, exc.path):
                    if re.search(exc.pattern, match_text, re.IGNORECASE):
                        is_excepted = True
                        break
            
            if not is_excepted:
                findings.append(Finding(
                    control="ORG-0001",
                    severity="major",
                    stage="enforce", 
                    file=rel_path,
                    line=line_num,
                    column=column,
                    match=match.group(),
                    message=f"Use of '{match.group()}' should be replaced with 'org' according to vocabulary standard"
                ))
    
    return findings


# ---------------------------------------------------------------------------
# Main scanning logic
# ---------------------------------------------------------------------------

def scan_repository(root: Path, doc: dict) -> tuple[list[Finding], dict]:
    """Scan the repository for tenant usage violations."""
    scan_config = doc.get("scan", {})
    skip_dirs = scan_config.get("skip_dirs", [])
    
    # Parse exceptions
    exceptions = []
    for exc_data in doc.get("exceptions", []):
        exceptions.append(Exception(
            path=exc_data["path"],
            pattern=exc_data.get("pattern"),
            reason=exc_data["reason"]
        ))
    
    excluded = excluded_paths(root)
    files_to_scan = list(find_files_to_scan(root, skip_dirs, excluded))
    
    all_findings = []
    files_scanned = 0
    
    for file_path in files_to_scan:
        # Two independent passes over the same file, one per control. ORG-0002
        # is ADDITIVE: it does not narrow, filter or suppress anything ORG-0001
        # reports, because ORG-0001 is at the strictest setting the fleet chose
        # and quietly making it narrower under the cover of adding a second
        # control would be a policy change wearing a refactor's clothes.
        all_findings.extend(scan_file_for_tenant(file_path, root, exceptions))
        all_findings.extend(scan_file_for_identifiers(file_path, root, exceptions))
        files_scanned += 1
    
    scan_stats = {
        "files_scanned": files_scanned,
        "excluded_paths": [str(p) for p in excluded]
    }
    
    return all_findings, scan_stats


# ---------------------------------------------------------------------------
# Rules and evaluation
# ---------------------------------------------------------------------------

def evaluate(findings: list[Finding], doc: dict) -> list[Finding]:
    """Apply control rules and return final findings."""
    controls = controls_by_id(doc)

    final_findings = []
    for finding in findings:
        control = controls.get(finding.control)
        if not control or control["status"] != "active":
            continue

        # Stage, remediation, and refs come from the catalog, not the scanner.
        # The scanner hardcodes "enforce" as a default; the catalog overrides it.
        # This is the fix for the bug where stage: warn still exited non-zero.
        finding.stage = control["stage"]
        finding.remediation = control["remediation"]
        finding.refs = control["refs"]
        final_findings.append(finding)

    return final_findings


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def emit_text(findings: list[Finding], scan_stats: dict, excluded: list[Path]) -> int:
    """Emit findings in text format and return exit code.

    enforce -> ::error::  annotation, exit 1
    warn    -> ::warning:: annotation, exit 0
    observe -> no annotation, counted in summary only, exit 0
    """
    enforced = [f for f in findings if f.stage == "enforce"]
    warned   = [f for f in findings if f.stage == "warn"]
    observed = [f for f in findings if f.stage == "observe"]

    for skipped in excluded:
        print(f"[skip] {skipped.name}/: the checker's own checkout, not the caller's code")

    for f in enforced:
        print(f"::error file={f.file},line={f.line},col={f.column}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")
    for f in warned:
        print(f"::warning file={f.file},line={f.line},col={f.column}::[{f.control}][{f.severity}] "
              f"{f.location}: {f.message}. Fix: {f.remediation.strip()} "
              f"({', '.join(f.refs)})")
    # observe: no annotation emitted; count appears in summary

    files_scanned = scan_stats.get("files_scanned", 0)
    if enforced:
        print(f"org-vocabulary: FAILED - {len(enforced)} enforced violation(s), "
              f"{len(warned)} advisory, {len(observed)} observed, "
              f"{files_scanned} file(s) scanned.")
        return 1
    print(f"org-vocabulary: OK - no enforced violations, {len(warned)} advisory, "
          f"{len(observed)} observed, {files_scanned} file(s) scanned.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".", help="repository to check")
    ap.add_argument("--control", default=str(DEFAULT_CONTROL), help="org-vocabulary control catalog")
    args = ap.parse_args()

    root = Path(args.repo_root).resolve()
    if not root.is_dir():
        cannot_run(f"--repo-root {root} is not a directory")
    
    doc = load_catalog(Path(args.control).resolve())
    
    excluded = excluded_paths(root)
    findings, scan_stats = scan_repository(root, doc)
    
    if scan_stats.get("files_scanned", 0) == 0:
        print("org-vocabulary: no files to scan in this repository; skipping.")
        return 0
    
    final_findings = evaluate(findings, doc)
    
    return emit_text(final_findings, scan_stats, excluded)


if __name__ == "__main__":
    raise SystemExit(main())