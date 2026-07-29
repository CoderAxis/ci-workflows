# Contract hooks (ihq-driven)

`ihq-guard.sh` runs the platform's contract checks locally, so a violation is caught before the
commit rather than in CI. `install-ihq-guard.sh` puts it into every repository in the workspace.

```bash
./install-ihq-guard.sh --dry-run    # show what would change
./install-ihq-guard.sh              # wire every repo
```

## What runs, and when

| Hook | Mode | Runs |
| --- | --- | --- |
| `pre-commit` | `fast` | Only when a staged file is `docs/openapi*.json`, `docs/openapi*.go`, or under `.github/workflows/` |
| `pre-push` | `full` | Always |

Both call one command:

```bash
ihq validate --repo "$(git rev-parse --show-toplevel)" --fail-on error --details
```

That covers operationId presence, uniqueness, ADR-0006 naming and the lockfile; request and
response examples; response schemas; the RFC-0001 envelope and its binding to `common.v1`;
RFC-0002 error codes; RFC-0003 pagination; no committed legacy swagger file; and that every call
to a central workflow pins `@v1`.

Errors block, warnings and info findings do not. Raise the bar for a repo that has earned it with
`IHQ_GUARD_FAIL_ON=warning`. A repository that commits no contract and serves no HTTP is checked
for the repo-level rules only — the CLI scopes that itself, so the hook is the same everywhere.

Bypass one run with `SKIP_IHQ_GUARD=1`, or everything with `git commit --no-verify`.

## Why this is a sourced library, not a hook

The fleet had five distinct `pre-commit` variants across 34 repositories and 84 repositories with
no hooks at all. Replacing them would have discarded gates that already work — auth's auth-core
sync check, the oasdiff baseline comparison in `pre-push`. So the installer appends a
marker-guarded block that sources the guard, and writes a hook from scratch only where none
exists. Re-running replaces the block rather than duplicating it.

One detail worth keeping: every one of the 68 pre-existing hooks ends in `exit 0`, so appending
at the end left the guard **present in the file and never executed**. The installer inserts
before the last top-level `exit` instead. A hook that is silently skipped is worse than no hook,
because the commit looks checked.

## `go.work` makes a local check lie

Every service domain has a `go.work` that points the shared modules at the working copies. Any
hook that runs a **generator** therefore regenerates from uncommitted local code, and reports a
result CI cannot reproduce — green locally, red in CI, over a diff that exists in no pushed
commit. Auth's `pre-commit` had exactly this: it ran the tests with `GOWORK=off` and the contract
check without it.

A hook that runs a generator or resolves a module pin must set `GOWORK=off`, because that is what
CI resolves. The `ihq validate` guard is unaffected — it reads the committed spec and runs no
generator — so this applies to service-specific hook steps, not to the shared guard.

## What the guard used to need from the CLI

CLI-1 through CLI-5 were the gaps this guard worked around in shell and Python. All five are now
implemented, and the workarounds are gone with them:

| | Gap | Now |
| --- | --- | --- |
| CLI-1 | No way to validate a single repository; a standalone clone silently validated the configured workspace instead | `--repo <path>` resolves without a workspace |
| CLI-2 | Exit 1 if **any** service in the fleet had errors, so the guard parsed `--json` and counted its own findings | exit code covers the selection |
| CLI-3 | `ihq validate auth` also matched `inboxxhq-authz-service` | `--repo` takes one path or one exact name |
| CLI-4 | No workflow-pin check, so the guard ran `grep` | `workflow-pin` check, in the default set |
| CLI-5 | No way for a clean repo to hold the line at warnings | `--fail-on error\|warning\|info` |

The guard lost its `python3` dependency and about 70 lines along the way.

### Still CI-only

`ihq validate` reads the committed spec, so the controls in `controls/api-contract.yaml` that are
about Go source or cross-repo state cannot be checked from a spec alone: no runtime docs surface
(API-0001), conformance imported from the shared suite (API-0003), and the `common.v1` components
matching the current proto projection (API-0007). API-0002 (one committed spec) and API-0006 (no
swaggo annotations) would be cheap to add — API-0006 partly exists already as `no-legacy-swagger`.
