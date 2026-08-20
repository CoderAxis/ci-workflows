# Local git hooks

Nothing in this directory implements a check any more. The local gate is `ihq git guard`,
installed by `ihq git hooks install`, and the standard that governs it is
[local-verification-gate-standard.md](https://github.com/coderaxis/core-docs/blob/main/standards/delivery/local-verification-gate-standard.md).

```bash
ihq git hooks install --fleet   # wire every repository in the workspace
ihq git hooks status            # which repositories are current
ihq git guard                   # what the hook will run
```

`install-ihq-guard.sh` remains as a redirect: it forwards to `ihq git hooks install --fleet`
so anyone with the old command in their fingers or in a script lands in the right place.

## Why the shell library is gone

`ihq-guard.sh` used to hold the checks, and the installer **copied** it into 113
repositories. That is centralised authorship with fanned-out distribution, and the
difference showed the moment anything needed changing: a new check meant touching 113
repositories, so in practice the library stayed as it was and the checks stayed in CI.
Copies drifted, and what a repository actually checked depended on when somebody last
remembered to re-run the installer there.

The lever that fixed it was already present — every copy shelled out to the `ihq` binary.
So the checks moved into the binary and a repository's hook became a caller with no logic
in it: rebuilding `ihq` changes what all 124 repositories check, with no fan-out at all.
It is the same trade the fleet already made for CI, where a service's `ci.yaml` is three
lines calling `service-ci.yaml@v1` and the tag is what moves.

The library was kept for a while as a shim so that any hook still sourcing it would keep
working. Once no hook did, it was 113 copies of a file with no callers, which reads as
working machinery to the next person who opens it — so it went too.

## Where the reasoning went

The engineering rationale that used to live in this file is now held where it applies:

| Was here | Now |
| --- | --- |
| Why hooks are merged rather than replaced, and the `exit 0` reachability trap | The standard, rules 2 and 3 |
| That a hook running a generator must set `GOWORK=off`, and the 29-of-99 CI break behind it | The standard's fidelity rule, and `G-161` in the verification gap register |
| The `ihq validate` gaps CLI-1 to CLI-5 | All five are implemented; the workarounds went with them |
| Fan-out of dependency bumps on release | [ADR-0082](https://github.com/coderaxis/core-docs/blob/main/adrs/ADR-0082-artifact-graph-and-central-release-cascade.md), which owns the release cascade |
