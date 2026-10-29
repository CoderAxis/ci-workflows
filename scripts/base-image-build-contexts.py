#!/usr/bin/env python3
"""Print BuildKit named contexts that pull a Dockerfile's ECR Public base images from Docker Hub.

FOR GITHUB-HOSTED RUNNERS ONLY. Dockerfiles in this fleet name their official base images as
public.ecr.aws/docker/library/<name>, and check-base-image-registry.py keeps it that way, because
the deploy lane builds in CodeBuild: from AWS, ECR Public is the registry that is not rate limited
and Docker Hub's anonymous quota is the one a fleet-wide rebuild spends.

From a GitHub-hosted runner the trade-off is the reverse. ECR Public caps anonymous pulls at 500 GB
a month per source address, hosted runners share their egress addresses with every other GitHub
customer, and once that allowance is spent every pull answers
"429 Too Many Requests / toomanyrequests: Data limit exceeded" before a single build step runs.
GitHub does not apply Docker Hub's rate limit to public-image pulls from its hosted runners, and
docker/library on ECR Public is a mirror of Docker Hub's official images that serves the same
digests. service-ci.yaml already pulls its Postgres service image from docker.io for this reason.

So a hosted build keeps the Dockerfile exactly as the deploy lane reads it and redirects each base
image at build time instead:

    docker buildx build \\
      --build-context public.ecr.aws/docker/library/node:22-alpine=docker-image://docker.io/library/node:22-alpine \\
      ...

A named context whose name matches a FROM reference replaces that image for the build. This
script computes those pairs from the Dockerfile, one per line, ready to pass as
`--build-context <line>`:

    python3 scripts/base-image-build-contexts.py [--build-arg NAME=VALUE ...] [DOCKERFILE]

It follows what BuildKit does with the file rather than what a grep would see:

* `FROM --platform=... <image> AS <name>`: flags are skipped and the stage name is recorded.
* `FROM <stage>`: a reference to an earlier stage pulls nothing and is not an image.
* `ARG IMAGE=public.ecr.aws/...` then `FROM ${IMAGE}`: global ARGs (declared before the first
  FROM) are expanded with their defaults, overridden by --build-arg exactly as the build would be.
  So pass the same --build-arg values the build is given.
* `<image>:<tag>@<digest>`: the digest is kept, so a pinned image stays pinned.
* `COPY --from=<image>` and `RUN --mount=...,from=<image>` pull an image too, and are redirected
  the same way.

Only public.ecr.aws/docker/library/<name> is redirected. That path is the mirror of Docker Hub's
official single-name images; every other ECR Public repository belongs to its own publisher and
has no Docker Hub twin, so it is left alone. A reference this script cannot resolve (an ARG it has
no value for) is reported as a warning and left alone too, which builds exactly as it did before.

Exit 0 with the pairs on stdout (possibly none), 2 on a bad invocation or an unreadable file.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ECR_LIBRARY = "public.ecr.aws/docker/library/"
DOCKER_HUB_LIBRARY = "docker.io/library/"

# The grammar of the reference that follows ECR_LIBRARY, from the distribution reference spec:
# one path component (official images have no namespace), an optional tag, an optional digest.
NAME_RE = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
TAG_RE = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST_RE = r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[0-9A-Fa-f]{32,}"
LIBRARY_REF_RE = re.compile(
    rf"^{re.escape(ECR_LIBRARY)}(?P<name>{NAME_RE})(?::(?P<tag>{TAG_RE}))?(?:@(?P<digest>{DIGEST_RE}))?$"
)

# BuildKit provides these in the global scope without a declaration. Their values depend on the
# builder and the --platform requested, which this script cannot know, so a reference that needs
# one is unresolvable unless --build-arg supplies it.
AUTOMATIC_PLATFORM_ARGS = frozenset({
    "BUILDPLATFORM", "BUILDOS", "BUILDARCH", "BUILDVARIANT",
    "TARGETPLATFORM", "TARGETOS", "TARGETARCH", "TARGETVARIANT",
})

DIRECTIVE_RE = re.compile(r"^#\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$")
HEREDOC_RE = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
HEREDOC_INSTRUCTIONS = frozenset({"RUN", "COPY", "ADD"})


class Unresolved(Exception):
    """A variable in a reference has no value this script can know."""


# The value of a global ARG whose default needs a value this script cannot know.
UNKNOWN = "\0unknown"


@dataclass
class Result:
    contexts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [f"{key}={value}" for key, value in sorted(self.contexts.items())]


# ---------------------------------------------------------------------------
# Reading the file the way the Dockerfile parser does
# ---------------------------------------------------------------------------

def instructions(text: str) -> list[tuple[int, str]]:
    """Logical instructions as (first line number, text), with continuations joined.

    Parser directives are read for the escape character. Comment lines are dropped, including
    those inside a continuation, as the Dockerfile parser drops them. Heredoc bodies of RUN, COPY
    and ADD are skipped, so a script that writes `FROM ...` into a file is not read as a stage.
    """
    lines = text.splitlines()
    escape = "\\"
    i = 0

    # Parser directives come first and stop at the first line that is not one.
    while i < len(lines):
        m = DIRECTIVE_RE.match(lines[i].strip())
        if not m:
            break
        if m.group(1).lower() == "escape" and m.group(2) in ("\\", "`"):
            escape = m.group(2)
        i += 1

    out: list[tuple[int, str]] = []
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        start = i + 1
        parts: list[str] = []
        line = raw
        while True:
            body = line.rstrip()
            if body.endswith(escape):
                parts.append(body[: -len(escape)])
                i += 1
                # Comment and blank lines inside a continuation are not part of the instruction.
                while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
                    i += 1
                if i >= len(lines):
                    break
                line = lines[i]
                continue
            parts.append(body)
            i += 1
            break

        instruction = " ".join(p.strip() for p in parts).strip()
        out.append((start, instruction))

        keyword = instruction.split(None, 1)[0].upper() if instruction else ""
        if keyword in HEREDOC_INSTRUCTIONS:
            for strip_tabs, _quote, delimiter in HEREDOC_RE.findall(instruction):
                # Skipped only when its terminator exists. A `<<word` that is really part of a
                # quoted shell string has none, and must not swallow the rest of the file.
                for j in range(i, len(lines)):
                    candidate = lines[j].lstrip("\t") if strip_tabs else lines[j]
                    if candidate == delimiter:
                        i = j + 1
                        break
    return out


# ---------------------------------------------------------------------------
# Variable expansion, as BuildKit applies it to FROM
# ---------------------------------------------------------------------------

def expand(word: str, args: dict[str, str | None]) -> str:
    """Expand $VAR, ${VAR} and the ${VAR:-x} / ${VAR-x} / ${VAR:+x} / ${VAR+x} forms.

    `args` maps a declared name to its value, or to None when it is declared without one (BuildKit
    then treats it as unset). A name that is not declared expands to the empty string, as in
    BuildKit, except an automatic platform ARG, whose value this script cannot know.
    """
    out: list[str] = []
    i = 0
    while i < len(word):
        c = word[i]
        if c == "\\" and i + 1 < len(word) and word[i + 1] == "$":
            out.append("$")
            i += 2
            continue
        if c != "$":
            out.append(c)
            i += 1
            continue
        if i + 1 < len(word) and word[i + 1] == "{":
            end = closing_brace(word, i + 2)
            out.append(substitute(word[i + 2:end], args))
            i = end + 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", word[i + 1:])
        if not m:
            out.append(c)
            i += 1
            continue
        out.append(value_of(m.group(0), args) or "")
        i += 1 + len(m.group(0))
    return "".join(out)


def closing_brace(word: str, start: int) -> int:
    depth = 1
    i = start
    while i < len(word):
        if word[i] == "\\":
            i += 2
            continue
        if word.startswith("${", i):
            depth += 1
            i += 2
            continue
        if word[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Unresolved(f"unterminated ${{ in {word!r}")


def substitute(inner: str, args: dict[str, str | None]) -> str:
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-+])(.*))?$", inner, re.DOTALL)
    if not m:
        raise Unresolved(f"unsupported substitution ${{{inner}}}")
    name, op, operand = m.group(1), m.group(2), m.group(3)
    value = value_of(name, args)
    if op is None:
        return value or ""
    if op == ":-":
        return value if value else expand(operand, args)
    if op == "-":
        return value if value is not None else expand(operand, args)
    if op == ":+":
        return expand(operand, args) if value else ""
    return expand(operand, args) if value is not None else ""  # "+"


def value_of(name: str, args: dict[str, str | None]) -> str | None:
    if name in args:
        if args[name] == UNKNOWN:
            raise Unresolved(f"${name}'s default depends on a value set by the builder")
        return args[name]
    if name in AUTOMATIC_PLATFORM_ARGS:
        raise Unresolved(f"${name} is set by the builder; pass --build-arg {name}=... to resolve it")
    return None


def arg_declarations(rest: str) -> list[tuple[str, str | None]]:
    """`ARG A=1 B="two" C` -> [(A, "1"), (B, "two"), (C, None)]."""
    decls: list[tuple[str, str | None]] = []
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)(?:=(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S*))?", rest):
        name, value = m.group(1), m.group(2)
        if value is not None and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        decls.append((name, value))
    return decls


# ---------------------------------------------------------------------------
# The redirect
# ---------------------------------------------------------------------------

def docker_hub_twin(reference: str) -> str | None:
    """docker-image://docker.io/library/<name>[:tag][@digest] for an ECR Public library reference."""
    m = LIBRARY_REF_RE.match(reference)
    if not m:
        return None
    target = DOCKER_HUB_LIBRARY + m.group("name")
    tag, digest = m.group("tag"), m.group("digest")
    if tag:
        target += f":{tag}"
    elif not digest:
        target += ":latest"
    if digest:
        target += f"@{digest}"
    return "docker-image://" + target


def build_contexts(text: str, build_args: dict[str, str] | None = None, source: str = "Dockerfile") -> Result:
    build_args = build_args or {}
    result = Result()
    # A platform ARG needs no declaration to be read by FROM. Its value is the builder's, which
    # this script knows only when the caller states it.
    global_args: dict[str, str | None] = {
        name: build_args[name] for name in AUTOMATIC_PLATFORM_ARGS if name in build_args
    }
    stages: set[str] = set()
    seen_from = False

    def consider(reference: str, line: int, what: str) -> None:
        if not reference.startswith(ECR_LIBRARY):
            return
        twin = docker_hub_twin(reference)
        if twin is None:
            result.warnings.append(
                f"{source}:{line}: {what} {reference!r} is under {ECR_LIBRARY} but is not a "
                f"single-name official image reference; it is pulled from ECR Public as written"
            )
            return
        result.contexts[reference] = twin

    def resolve(word: str, line: int, what: str) -> str | None:
        try:
            return expand(word, global_args)
        except Unresolved as why:
            result.warnings.append(
                f"{source}:{line}: {what} {word!r} cannot be resolved ({why}); "
                f"it is pulled as the build resolves it, without a redirect"
            )
            return None

    for line, instruction in instructions(text):
        head = instruction.split(None, 1)
        keyword = head[0].upper()
        rest = head[1].strip() if len(head) > 1 else ""

        if keyword == "ARG":
            if seen_from:
                continue  # a stage-scoped ARG cannot reach a FROM
            for name, default in arg_declarations(rest):
                if name in build_args:
                    global_args[name] = build_args[name]
                elif default is None and name in AUTOMATIC_PLATFORM_ARGS:
                    continue  # declaring it without a value keeps the builder's value, still unknown
                elif default is None:
                    global_args[name] = None
                else:
                    try:
                        global_args[name] = expand(default, global_args)
                    except Unresolved:
                        global_args[name] = UNKNOWN
            continue

        if keyword == "FROM":
            seen_from = True
            tokens = rest.split()
            words = [t for t in tokens if not t.startswith("--")]
            if not words:
                continue
            for j, tok in enumerate(words[:-1]):
                if tok.upper() == "AS":
                    stage_after = words[j + 1].lower()
                    break
            else:
                stage_after = None
            reference = resolve(words[0], line, "base image")
            if reference is not None and reference.lower() not in stages and reference != "scratch":
                consider(reference, line, "base image")
            if stage_after:
                stages.add(stage_after)
            continue

        if keyword in ("COPY", "ADD", "RUN"):
            sources = re.findall(r"--from=(\S+)", rest) if keyword != "RUN" else []
            if keyword == "RUN":
                for mount in re.findall(r"--mount=(\S+)", rest):
                    for opt in mount.split(","):
                        k, _, v = opt.partition("=")
                        if k == "from" and v:
                            sources.append(v)
            for src in sources:
                src = src.strip("\"'")
                if src.isdigit() or src.lower() in stages:
                    continue
                reference = resolve(src, line, "--from image")
                if reference is not None and reference.lower() not in stages:
                    consider(reference, line, "--from image")

    return result


def parse_build_args(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        name, sep, val = value.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"--build-arg {value!r} is not NAME=VALUE")
        out[name] = val
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Print --build-context pairs that pull a Dockerfile's "
                    "public.ecr.aws/docker/library base images from docker.io instead.")
    ap.add_argument("dockerfile", nargs="?", default="Dockerfile", help="default: ./Dockerfile")
    ap.add_argument("--build-arg", action="append", default=[], metavar="NAME=VALUE",
                    help="the same build arguments the build is given; repeatable")
    args = ap.parse_args(argv)

    try:
        build_args = parse_build_args(args.build_arg)
    except ValueError as why:
        print(f"::error::{why}", file=sys.stderr)
        return 2

    path = Path(args.dockerfile)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as why:
        print(f"::error::cannot read {path}: {why.strerror or why}", file=sys.stderr)
        return 2

    result = build_contexts(text, build_args, source=str(path))
    for warning in result.warnings:
        print(f"::warning::{warning}", file=sys.stderr)
    for line in result.lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
