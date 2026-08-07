#!/usr/bin/env python3
"""Enforce how a repository is allowed to pin the platform's own Go modules.

WHY THIS EXISTS
---------------
This replaces scripts/check_core_module_versions.sh, which hardcoded the two module paths it
checked. That made the policy correct only for the single repository the script was copied
into: a service that pinned a core the script did not name was simply never checked, and the
gate still reported success. Two copies already existed and had diverged.

The module list now comes from the catalog (resolve_service_identity.py --> required_modules),
following the dependency DAG upward, so it cannot fall out of step with what the service is.

TWO CLASSES OF RULE
-------------------
REQUIRED modules must be present. These come from the catalog's dependency DAG: a deployable
pins both the core and the schema adapter, an adapter pins its core.

GOVERNED modules must be well-formed IF present - every pin matching a platform prefix,
whether or not the catalog names it. This exists because platform-shared-go is pinned by 86
repositories and appears in no catalog entry at all, so a required-only check covered none of
them. Enumerating 86 consumers would just be the hand-maintained matrix problem again, one
layer up; matching the prefix covers the dependency the fleet actually has, and covers the
next platform module without anyone remembering to add it.

WHAT IT ENFORCES, AND WHY EACH RULE EXISTS
------------------------------------------
1. DIRECT REQUIREMENT (required only). If a module arrives only transitively, Go resolves it
   to whatever another dependency happens to ask for, so a bump to it would change nothing
   here and the service would silently keep old code. §1's "diamond".

2. NO REPLACE DIRECTIVE (both classes). A local-path replace makes the build depend on a
   working tree that exists on one machine, so what CI validates is not what ships (§4).

3. STABLE TAGS ON RELEASE BRANCHES (both classes). A pseudo-version names an untagged commit.
   Shipping one means the exact source cannot be recovered from the version alone, which
   breaks the provenance claim the release pipeline makes (§8).

4. VERSION FLOORS (both classes), from controls/module-floors.yaml. A floor may be fleet-wide or
   scoped to a role with applies_to, and the distinction is load-bearing. The governed sweep
   above is deliberately broad about which PINS it looks at; it is not a statement that every
   repository has the same REQUIREMENTS. Collapsing the two is not theoretical - the
   platform-shared-go floor was raised for GW-0003, a control whose own definition reads
   applies_when: gateway, and because the sweep applied it to every pin under the platform
   prefix it landed on 78 of the 88 Go modules in the fleet. All six gateways already met it.
   The result was that every consumer bump PR opened by a contracts release failed the pin
   policy, 43 red jobs per release, none of them about a gateway.

Advisory off a release branch, blocking on preprod/prod, matching the original script's
posture so this swap does not quietly tighten the fleet in the same change that centralises it.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from typing import NamedTuple

# v0.0.0-20260518192409-9f39cdd313ed - timestamp + short sha, i.e. no tag exists.
PSEUDO = re.compile(r"^v\d+\.\d+\.\d+-\d{14}-[0-9a-f]{12}$")
STABLE = re.compile(r"^v\d+\.\d+\.\d+$")
STRICT_MODES = {"preprod", "prod"}

# The roles a floor may be scoped to. "service" is everything that is not a gateway; both are
# resolvable from a checkout, which is what makes them usable as a scope.
ROLES = {"gateway", "service"}
GATEWAY_SUFFIXES = ("-gateway", "-bff")

SELF_REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_FLOORS = SELF_REPO / "controls" / "module-floors.yaml"


def semver(version: str) -> tuple[int, int, int] | None:
    """Parse a stable vX.Y.Z tag. Returns None for anything else."""
    if not STABLE.match(version):
        return None
    return tuple(int(p) for p in version[1:].split("."))  # type: ignore[return-value]


class Floor(NamedTuple):
    """A declared minimum version, and the role it is declared for.

    An empty applies_to means fleet-wide: every repository pinning the module is held to it.
    Anything else names the one role that has to meet it.
    """
    minimum: str
    applies_to: str = ""


def load_floors(path: pathlib.Path) -> dict[str, Floor]:
    """Return {module: minimum version}, or {} when no floors file is present.

    A missing file means no floors are declared, which is a legitimate configuration. A
    file that exists but cannot be read is an error, never a skip - a floor that silently
    stops being enforced is worse than one that was never declared, because the report
    still says the pin policy passed.
    """
    if not path.is_file():
        return {}
    try:
        import yaml  # imported lazily: only a repo declaring floors needs PyYAML
    except ModuleNotFoundError:
        raise SystemExit(f"::error::{path} declares module floors but PyYAML is not installed; "
                         "install it (python3 -m pip install pyyaml) rather than running "
                         "without floor enforcement")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    floors = doc.get("floors") or {}
    if not isinstance(floors, dict):
        raise SystemExit(f"::error::{path}: expected a 'floors:' mapping of module -> version")
    out: dict[str, Floor] = {}
    for module, spec in floors.items():
        if not isinstance(spec, dict):
            out[module] = Floor(str(spec))
            continue
        scope = str(spec.get("applies_to") or "")
        if scope and scope not in ROLES:
            raise SystemExit(
                f"::error::{path}: {module} declares applies_to: {scope}, which is not a role this "
                f"check can resolve. Known roles: {', '.join(sorted(ROLES))}. A floor scoped to a "
                "role nothing resolves to reads as declared and is enforced nowhere."
            )
        out[module] = Floor(str(spec["min"]), scope)
    return out


def detect_role(go_mod: pathlib.Path) -> str:
    """Decide which role a checkout is, for floors that are scoped to one.

    This has to answer the question the same way check-gateway-baseline.py does, because a floor
    scoped to gateways and the control that enforces it on gateways have to agree about the set.
    That script reads the service contract first and falls back to the name, so this does too.

    The name test is a backstop rather than the primary, but it is not optional: a repository with
    no readable contract must not be able to leave the gateway set just by being unparseable.
    """
    root = go_mod.resolve().parent
    contract = root / "service.contract.yaml"
    if contract.is_file():
        try:
            import yaml
            service = (yaml.safe_load(contract.read_text(encoding="utf-8")) or {}).get("service")
            if isinstance(service, dict):
                if str(service.get("type", "")).lower() in ("gateway", "bff"):
                    return "gateway"
                if str(service.get("name", "")).endswith(GATEWAY_SUFFIXES):
                    return "gateway"
        except Exception:  # noqa: BLE001 - an unreadable contract falls through to the name test
            pass

    module = ""
    try:
        for line in go_mod.read_text(encoding="utf-8").splitlines():
            if line.startswith("module "):
                module = line.split()[1]
                break
    except OSError:
        pass
    if module.endswith(GATEWAY_SUFFIXES) or root.name.endswith(GATEWAY_SUFFIXES):
        return "gateway"
    return "service"


def parse_go_mod(text: str) -> tuple[dict[str, str], set[str]]:
    """Return {module: version} for direct requires, and the set of replaced modules.

    Indirect requires are excluded deliberately: rule 1 is specifically about the pin being
    direct, so counting an indirect require as satisfying it would defeat the check.
    """
    requires: dict[str, str] = {}
    replaced: set[str] = set()
    block = None

    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        indirect = "// indirect" in raw
        if not line:
            continue

        if line.startswith("require (") or line == "require(":
            block = "require"
            continue
        if line.startswith("replace (") or line == "replace(":
            block = "replace"
            continue
        if line == ")":
            block = None
            continue

        parts = line.split()
        if line.startswith("require ") and len(parts) >= 3:
            if not indirect:
                requires[parts[1]] = parts[2]
            continue
        if line.startswith("replace ") and len(parts) >= 2:
            replaced.add(parts[1])
            continue
        if block == "require" and len(parts) >= 2 and not indirect:
            requires[parts[0]] = parts[1]
        elif block == "replace" and parts:
            replaced.add(parts[0])

    return requires, replaced


def _describe(version: str) -> str:
    if PSEUDO.match(version):
        return "pseudo-version (no tag exists for that commit)"
    if STABLE.match(version):
        return "released"
    return "non-standard version"


def check(go_mod: pathlib.Path, modules: list[str], mode: str,
         governed_prefix: str = "",
         floors: dict[str, Floor | str] | None = None,
         role: str = "") -> tuple[list[str], list[str], list[str]]:
    """Return (errors, warnings, report).

    Floor violations are warnings in advisory modes and errors in strict ones, matching this
    script's existing posture: a stale contract vintage should not silently reach preprod or
    prod, but it should not block a dev branch while the fleet is still being moved onto the
    floor.

    A floor scoped with applies_to is checked only against a repository of that role. The
    governed sweep deliberately covers every pin under the platform prefix, which is what brings
    platform-shared-go's consumers under policy without enumerating them - but that breadth is
    about which PINS are looked at, not about which REQUIREMENTS every repository has to meet.
    Without the distinction the two collapse, and a floor raised for one role becomes a floor for
    the whole fleet the moment it is written down.
    """
    if not go_mod.is_file():
        return ([f"{go_mod} not found"], [], [])
    requires, replaced = parse_go_mod(go_mod.read_text(encoding="utf-8"))
    strict = mode in STRICT_MODES
    # A bare version string is accepted as a fleet-wide floor. load_floors does not produce that
    # shape, but callers that build the mapping themselves do, and a caller passing the shape this
    # function has always taken should not have to know that scoping was added underneath it.
    floors = {m: f if isinstance(f, Floor) else Floor(str(f))
              for m, f in (floors or {}).items()}
    role = role or detect_role(go_mod)

    errors: list[str] = []
    warnings: list[str] = []
    report: list[str] = []

    def check_floor(module: str, version: str, label: str) -> None:
        """A pin below the declared floor is a stale contract vintage, not a style problem.

        Version skew on a contract module is invisible in every per-repo gate: each service
        projects its OpenAPI components from its own pin, so both a current and a stale
        service pass their own drift check while publishing different envelopes.
        """
        spec = floors.get(module)
        if not spec:
            return
        if spec.applies_to and spec.applies_to != role:
            # Reported rather than passed over in silence. A scoped floor that simply vanished
            # from the output would be indistinguishable from no floor being declared, which is
            # the reading this file's own header warns against.
            report.append(f"{label}: floor {spec.minimum} applies to {spec.applies_to}, "
                          f"not to this {role}; not enforced here")
            return
        floor = spec.minimum
        want, got = semver(floor), semver(version)
        if want is None:
            errors.append(f"{label}: declared floor {floor} is not a stable vX.Y.Z tag")
            return
        sink = errors if strict else warnings
        if got is None:
            sink.append(
                f"{label}: pinned at {version}, which cannot be compared against the required "
                f"floor {floor}. Pin a released tag so the floor is checkable."
            )
            return
        if got < want:
            sink.append(
                f"{label}: pinned at {version}, below the required floor {floor}. "
                f"Run `go get {module}@{floor}` and regenerate any derived artifacts "
                f"(the published OpenAPI components are projected from this module)."
            )

    def well_formed(module: str, version: str, label: str) -> None:
        if module in replaced:
            errors.append(
                f"{label}: overridden by a replace directive. The published module must be what "
                "CI validates, so replace is not permitted on a platform module."
            )
            return
        spec = floors.get(module)
        # Only name the floor when it binds this repository. Printing it unconditionally reads as
        # a requirement being applied, which for a scoped floor is the opposite of what happened.
        binding = spec and (not spec.applies_to or spec.applies_to == role)
        suffix = f", floor {spec.minimum}" if binding else ""
        report.append(f"{label}: {version} ({_describe(version)}{suffix})")
        if strict and not STABLE.match(version):
            errors.append(
                f"{label}: {mode} requires a stable tag, found {version}. Release the module and "
                "re-pin; a pseudo-version cannot be resolved back to a release."
            )
        check_floor(module, version, label)

    for module in modules:
        label = module.rsplit("/", 1)[-1]
        version = requires.get(module)
        if version is None:
            errors.append(
                f"{label}: not a direct requirement in go.mod. The catalog says this repo pins "
                f"{module} directly; resolved transitively, a bump to it would not change this "
                "build."
            )
            continue
        well_formed(module, version, label)

    # Everything else under the platform prefix. Not required to be present - many repos legitimately
    # do not pin platform-shared-go - but held to the same shape when it is.
    if governed_prefix:
        for module, version in sorted(requires.items()):
            if module.startswith(governed_prefix) and module not in modules:
                well_formed(module, version, f"{module.rsplit('/', 1)[-1]} (governed)")

    return errors, warnings, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--go-mod", default="go.mod")
    ap.add_argument(
        "--modules",
        default=os.environ.get("REQUIRED_MODULES", "[]"),
        help="JSON array of module paths, from the resolver's required_modules output.",
    )
    ap.add_argument(
        "--mode",
        default="policy",
        choices=["policy", "dev", "staging", "preprod", "prod"],
        help="Blocking on preprod/prod; advisory elsewhere.",
    )
    ap.add_argument(
        "--governed-prefix",
        default="github.com/coderaxis/",
        help="Every pin under this prefix must be well-formed, catalog-named or not. This is what "
             "brings platform-shared-go's 86 consumers under policy without enumerating them.",
    )
    ap.add_argument(
        "--floors",
        default=str(DEFAULT_FLOORS),
        help="YAML declaring the minimum version for contract-bearing modules. A pin below its "
             "floor fails. Absent file means no floors are declared.",
    )
    ap.add_argument(
        "--role",
        default="",
        choices=["", *sorted(ROLES)],
        help="Role of the repository under test, for floors scoped with applies_to. Resolved from "
             "the checkout when not given; pass it only to override that.",
    )
    args = ap.parse_args()

    try:
        modules = json.loads(args.modules)
    except json.JSONDecodeError as exc:
        print(f"::error::--modules is not valid JSON: {exc}", file=sys.stderr)
        return 2

    # An empty required list is a correct answer for a core module, which sits at the bottom of the
    # DAG - but the governed sweep still applies, because a core pins platform-shared-go too.
    errors, warnings, report = check(
        pathlib.Path(args.go_mod), list(modules), args.mode, args.governed_prefix,
        load_floors(pathlib.Path(args.floors)), args.role,
    )

    lines = [f"## Platform module pin policy ({args.mode})", ""]
    lines += [f"- {r}" for r in report]
    lines += [f"- {w} (warning: blocks on {'/'.join(sorted(STRICT_MODES))})" for w in warnings]
    lines += [f"- **{e}**" for e in errors]
    lines.append(
        f"- Enforcement: {'blocking' if args.mode in STRICT_MODES else 'advisory'} for {args.mode}"
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    for r in report:
        print(r)
    for w in warnings:
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
