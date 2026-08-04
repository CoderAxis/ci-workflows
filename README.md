# coderaxis/github-actions

Shared **reusable composite actions** and **reusable workflows** for the coderaxis
platform CI/CD.

Reference an action by its subfolder and a major-version tag:

```yaml
- uses: coderaxis/github-actions/module-auth@v1
 with:
 app-id: ${{ secrets.CODERAXIS_APP_ID }}
 private-key: ${{ secrets.CODERAXIS_APP_PRIVATE_KEY }}
```

This repo is **public** so workflows in every owner (`coderaxis`, `InboxxHQ-CoderAxis`,
`a-second-client`) can consume the actions. The actions contain **no secrets** — callers pass
credentials as inputs at call time.

## Actions

| Action | Purpose |
| ------ | ------- |
| [`module-auth`](module-auth/action.yaml) | Mint a short-lived GitHub App installation token (`coderaxis-module-reader`) and configure git for private module reads. Replaces long-lived `CROSS_REPO_TOKEN` / `WORKFLOW_GH_PAT`. |

Future actions (e.g. `docker-login`, `slack-notify`, `aws-login`) live as sibling folders.

## Reusable workflows

Whole workflows (job-level OIDC, `permissions`, `concurrency`, multi-job orchestration)
live under `.github/workflows/` and are consumed via `uses:` at the **job** level.

| Workflow | Purpose |
| -------- | ------- |
| [`deploy-reusable.yaml`](.github/workflows/deploy-reusable.yaml) | InboxxHQ GitOps delivery — CI orchestrates the central canonical build (`inboxxhq-build`), reads back the signed image **digest**, and pins it into the `dev` overlay (first consumer). staging/preprod/prod promote the same digest via the Promotion Controller. **Build once, deploy the digest.** |
| [`seed-contract-check.yaml`](.github/workflows/seed-contract-check.yaml) | Language-agnostic seeding-contract gate (seeding standard §6b) — Dockerfile seed-binary marker + `seed/data` copy, canonical `system/dev/staging/preprod/prod` tree, placeholder-only qualified envs, no `SEED_COMMAND=""` override. Runs the pinned [`scripts/check-seed-contract.py`](scripts/check-seed-contract.py) (SSOT) against the caller; stateless services self-skip. |
| [`schema-compatibility.yaml`](.github/workflows/schema-compatibility.yaml) | Schema-**path/layout** + schema-migration + canonical-outbox-conformance gate for every `*-core-postgres` repo. First, `check_schema_layout` fails fast (before touching any DB) if the repo carries a `.sql` file anywhere other than `schema/migrations/` (the migration chain), `sql/queries/` (sqlc query source), `schema/seed*.sql` (dev/role seed data), `seed/` (the platform seeding-standard tree), or `testdata/` (Go test fixtures) — or if `schema/migrations/` is missing the required `000001_init.up.sql` + `000001_init.down.sql` baseline pair (additional forward migrations, e.g. `000002_...`, are always welcome and never capped). It then spins an ephemeral `postgres:18`, applies the repo's migrations to HEAD via an auto-detecting ladder (goose round-trip test → `schema.GooseUpDSN` → embedded `schema.Migrate` → static lint; fixes the old "goose gap" where pure-goose repos never actually migrated), then runs the centrally-pinned canonical **outbox verifier** (RFC-0032 / ADR-0069) and fails closed on ANY semantic drift (columns/types/defaults/domain/PK/unique/checks/partitioning). Runs the SSOT [`scripts/schema-compat.sh`](scripts/schema-compat.sh) against the caller. The same layout rule is enforced locally/offline, fleet-wide at once, by `inboxxhq-infra`'s `scripts/check-migration-filename-consistency.py --only-core-postgres-init`. |
| [`dockerfile-standard.yaml`](.github/workflows/dockerfile-standard.yaml) | Enterprise Dockerfile Standard gate (ADR-0072) — static analysis only, never runs `docker build`/pushes an image (keeps ADR-0051 DM-0001 intact). Every caller declares its `capabilities` (comma list from `dockerfile-capability-matrix.yaml`: `http-api`, `db-owner`, `seed`, `backfill`, `canary`, `kafka-producer-dk`, `worker`, `gateway`, `stateless`). Checks the canonical two-stage layout, centrally pinned base-image versions, a numeric non-root `USER`, required OCI labels, exec-form `CMD`, `STOPSIGNAL`, a `db-owner` repo shipping a `dbtool` binary, capability-declaration-matches-repo-reality, no build-time codegen, no `ADD`, no freeform version ARGs, the BuildKit secret-mount pattern for private-module credentials, and an `apk add` package allow-list. Deliberately does not duplicate `seed-contract-check.yaml`'s hard enforcement (its seed check here is advisory-only). Runs the SSOT [`scripts/check-dockerfile-standard.py`](scripts/check-dockerfile-standard.py) against the caller's own Dockerfile + repo structure. |
| [`event-handling-compliance.yaml`](.github/workflows/event-handling-compliance.yaml) | Event-handling compliance gate — the executable form of `services/ENTERPRISE_NOTIFICATION_PATTERN.md` §7/§8. Every caller declares its family `role` (`P`\|`H`\|`DK`\|`Hybrid`\|`E`\|`Bridge`, per the §7 matrix): `P`/`H` (owns Postgres) may **never** construct a raw Kafka producer — domain events must flow exclusively through the transactional outbox (`platform-shared-go/outbox`) + Debezium CDC; `DK`/`Hybrid` may produce directly but only via the canonical `platform-shared-go/messaging/kafka` envelope, and every topic string found must be in the declared `allowed_topics`; `E`/`Bridge` are unrestricted (true exception / the sanctioned CDC-polling canonicalizer). Also reports (advisory, non-blocking) any raw `sarama.NewConsumerGroup` not wrapped by the shared `events.EnterpriseConsumer` (retry/DLQ/tracing/health). Pure static analysis via the SSOT [`scripts/event-compliance.sh`](scripts/event-compliance.sh) — no DB/broker needed. |
| [`docs-governance.yaml`](.github/workflows/docs-governance.yaml) | Documentation-governance gate (ADR-0081) — the generic contract every governed docs repo shares: well-formed/complete frontmatter, `OWNER_DIRECTORY.md`-registered ownership, controlled vocabulary, `related_*` block-list style + Related-Docs links, ADR/RFC supersession reciprocity, a freshness SLA, and single-client scope isolation (only where `governance/CLIENT_SCOPE.md` is present). Machine-readable catalog schema + generated-artifact drift (DOC-0010) is **delegated** to the repo's own `scripts/build_catalog.py --check` (domain logic stays local), never duplicated centrally. Shared logic, per-repo data; auto-detects catalog/client-scope capabilities. Runs the SSOT [`scripts/check-docs-governance.py`](scripts/check-docs-governance.py) against the caller (`--root`). |

Each service repo carries only a thin caller:

```yaml
# .github/workflows/deploy.yaml
on:
 push: { branches: [main] }
 workflow_dispatch: {}
permissions:
 contents: read
 id-token: write
jobs:
 deploy:
 uses: coderaxis/github-actions/.github/workflows/deploy-reusable.yaml@v1
 with:
 service_name: auth-service
 secrets: inherit
```

### Images composed from more than one repository

Almost every image is built from its own repo alone, and those repos need nothing beyond the
caller above. A repo whose image also composes **another** repository declares that in
`.platform/build-inputs.json`; the caller stays the same size:

```json
{
  "version": 1,
  "inputs": [
    { "repo": "coderaxis/core-docs", "ref": "main", "path": ".core-docs",
      "reason": "shared canonical docs composed into the site" }
  ]
}
```

The reusable workflow reads that file from the calling repo, checks each input out **inside** the
primary source tree at the declared path, resolves each `ref` to a concrete commit SHA, and reports
the complete resolved set to the canonical build. Two consequences follow, and they are the point
(ADR-0051 §8):

- **The build is hermetic.** Every input is inside the uploaded source revision, so the Dockerfile
  fetches nothing at build time and the artifact is reproducible from what was recorded.
- **The cache key and the provenance cover every input.** A change in *any* declared input produces
  a new key and therefore a new build, and the SLSA `materials` list names every revision that went
  in. With a single input the key is unchanged — `src-<sha[:12]>`, exactly as before.

Declaring inputs needs the App credentials that mint the short-lived cross-org read token:

```yaml
    secrets:
      CROSS_REPO_TOKEN: ${{ secrets.CROSS_REPO_TOKEN }}        # writes the GitOps digest pin
      CODERAXIS_APP_ID: ${{ secrets.CODERAXIS_APP_ID }}        # reads the declared inputs
      CODERAXIS_APP_PRIVATE_KEY: ${{ secrets.CODERAXIS_APP_PRIVATE_KEY }}
```

All declared inputs must currently share one owner, since one installation token is minted per
build. That is a deliberate limit; it fails loudly rather than half-working.

Stateful service repos also carry a thin seed-contract caller:

```yaml
# .github/workflows/seed-contract-check.yaml
on:
 push: { branches: ["**"] }
 pull_request:
 paths: ["Dockerfile", "cmd/seed/**", "internal/**/seed/**", ".github/workflows/seed-contract-check.yaml"]
permissions:
 contents: read
jobs:
 seed-contract:
 uses: coderaxis/github-actions/.github/workflows/seed-contract-check.yaml@v1
```

Every `*-core-postgres` repo carries a thin schema-compatibility caller (the only
per-repo input is the outbox table name; omit it for a repo without an outbox):

```yaml
# .github/workflows/schema-compatibility.yaml
on:
 pull_request:
 paths: ["schema/**", "sql/**", "sqlc.yaml", "go.mod", "go.sum", ".github/workflows/schema-compatibility.yaml"]
 push: { branches: ["**"] }
 workflow_dispatch: {}
permissions:
 contents: read
jobs:
 schema-compatibility:
 uses: coderaxis/github-actions/.github/workflows/schema-compatibility.yaml@v1
 with:
 table: auth_service_outbox # the repo's outbox table; omit to skip outbox conformance
 secrets: inherit # REQUIRED: inherits the module-read App creds for private go deps
```

Delivery logic changes are made **once** here and rolled out by moving the `@v1` tag —
never by editing ~40 service repos. The outbox **contract version** is likewise pinned
once here (`outbox_verify_version`), so tightening it is a one-line change in this repo,
not a fleet-wide `go.mod` bump.

Every deployable service repo also carries a thin event-handling-compliance caller (the
only per-repo input is its `role` from the `ENTERPRISE_NOTIFICATION_PATTERN.md` §7
matrix; `allowed_topics` is required only for `DK`/`Hybrid`):

```yaml
# .github/workflows/event-handling-compliance.yaml
on:
 pull_request:
 paths: ["**/*.go", "go.mod", "go.sum", ".github/workflows/event-handling-compliance.yaml"]
 push: { branches: ["**"] }
 workflow_dispatch: {}
permissions:
 contents: read
jobs:
 event-handling-compliance:
 uses: coderaxis/github-actions/.github/workflows/event-handling-compliance.yaml@v1
 with:
 role: P # P | H | DK | Hybrid | E | Bridge
 # allowed_topics: "inboxxhq.chat.messages" # required for DK/Hybrid only
```

Every Go backend deployable repo (`services/**`, `gateways/**`) also carries a thin
dockerfile-standard caller (the only per-repo input is its `capabilities`, from
[`dockerfile-capability-matrix.yaml`](https://github.com/coderaxis/microservices/blob/main/docs/core-docs/standards/infrastructure/dockerfile-capability-matrix.yaml)
in `coderaxis/microservices`):

```yaml
# .github/workflows/dockerfile-standard.yaml
on:
 pull_request:
 paths: ["Dockerfile", ".github/workflows/dockerfile-standard.yaml"]
 push: { branches: ["**"] }
 workflow_dispatch: {}
permissions:
 contents: read
jobs:
 dockerfile-standard:
 uses: coderaxis/github-actions/.github/workflows/dockerfile-standard.yaml@v1
 with:
 capabilities: "http-api,db-owner,seed" # from dockerfile-capability-matrix.yaml
 # fail_on: minor # default is major; tighten once a repo's
 # remediation batch is green (see the
 # fleet audit report)
```

The canonical Dockerfile every repo derives from lives at
[`templates/Dockerfile.service`](templates/Dockerfile.service) — copy it in, fill in the
`__PLACEHOLDER__` tokens (service name, repo name, capability list, port), uncomment the
capability blocks this repo declares, and delete the rest. Every version referenced by it
is centrally pinned in
[`dockerfile-version-matrix.yaml`](https://github.com/coderaxis/microservices/blob/main/docs/core-docs/standards/infrastructure/dockerfile-version-matrix.yaml)
(coderaxis/microservices) — a fleet-wide bump is a one-line change there plus a bump of
this repo's `EXPECTED_BUILDER_TAG`/`EXPECTED_RUNTIME_TAG` constants, never ~40 individual
Dockerfile edits.

## Delivery model (enforced, not just documented)

The delivery model is **build once, deploy the digest**. The architectural policy is
owned by the ADR/RFC (single source of truth) — the workflow implements it and a CI
guard **enforces** it:

- Architecture SSOT (in the `core-docs` repo): `ADR-0051` (Artifact Promotion,
 Digest-Pinned Deployment, and Registry Segregation) and `RFC-0020` (Supply-Chain
 Integrity and Artifact Promotion).
- Implementation: [`deploy-reusable.yaml`](.github/workflows/deploy-reusable.yaml).
- Enforcement: [`scripts/check-delivery-model.py`](scripts/check-delivery-model.py) run
 by the `delivery-model` job in this repository's own
 [`ci.yaml`](.github/workflows/ci.yaml) on every change.

This closes the gap where the model existed only as header comments that could drift
from the implementation. The checker is the **executable form of ADR-0051**.

### Control catalog (policy-as-code)

The controls are declared in
[`controls/delivery-model.yaml`](controls/delivery-model.yaml) — the catalog defines
**policy only** (never how detection is implemented). Each control carries a stable id,
`policy`, `rationale`, `remediation`, `severity`, `scope`, `owner`, and lifecycle
`status`; the checker binds each `detector` to an implementation that may evolve
(regex → AST → CodeQL) without touching the catalog. **critical/major** controls fail
CI; **minor** controls are advisory. Tune with `--fail-on {critical,major,minor}`.

Control IDs (`DM-NNNN`) are **stable and permanent** — never rename or recycle; retire a
control via `status: deprecated|superseded`. The table below is **generated** from the
catalog (drift-gated in CI via `--verify-docs`), so docs and policy never diverge:

<!-- BEGIN delivery-controls (generated: scripts/check-delivery-model.py --write-docs) -->

_Generated from `controls/delivery-model.yaml` by `scripts/check-delivery-model.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Owner | Status |
| ------- | ------ | -------- | ----- | ----- | ------ |
| DM-0001 | CI orchestrates the central canonical build and must never build or publish a container image itself. The central build executor is the sole publish identity. | critical | reusable-workflow | platform-infrastructure | active |
| DM-0002 | A single immutable artifact is built once and promoted unchanged. There must be no second build for another environment or build variant. | critical | reusable-workflow | architecture-review-board | active |
| DM-0003 | Dev is the first consumer. staging / preprod / prod receive the same digest via the Promotion Controller and must never be pinned, built, or written by this workflow. | critical | reusable-workflow | platform-infrastructure | active |
| DM-0004 | AWS auth uses OIDC to assume the ci-build orchestrator role (inputs.ci_build_role_arn). Superseded per-env deploy / terraform-apply role ARNs must never be referenced. | critical | reusable-workflow | security | active |
| DM-0005 | The workflow must not pass swagger/docs build-variant flags (e.g. GO_BUILD_TAGS=swagger, -tags swagger) to the canonical build. The same swagger-less artifact is promoted to every environment. | critical | reusable-workflow | architecture-review-board | active |
| DM-0006 | Workflow and job permissions are a subset of {contents: read, id-token: write}. No write scope beyond id-token (no packages: write, no contents: write). | major | reusable-workflow | security | active |
| DM-0007 | A run step must fail the build when the ref is not main. | major | reusable-workflow | platform-infrastructure | active |
| DM-0008 | An OIDC configure-aws-credentials step must be present and id-token: write must be granted, so credentials are short-lived and keyless. | major | reusable-workflow | security | active |
| DM-0009 | The workflow must pin the built image (by digest/ref) into the GitOps infra repo dev overlay. This asserts the pin behaviour exists; it does not mandate a specific helper-script name. | major | reusable-workflow | platform-infrastructure | active |
| DM-0010 | The reusable workflow exposes a contract_version workflow_call output (versioned public API). | major | reusable-workflow | platform-infrastructure | active |
| DM-0011 | The reusable workflow exposes the promoted image_digest as a workflow_call output. | minor | reusable-workflow | platform-infrastructure | active |
| DM-0012 | The header references ADR-0051 and RFC-0020 so implementation and policy SSOT cannot drift apart. | minor | reusable-workflow | architecture-review-board | active |
| DM-0013 | A repository whose image is composed from more than its own source must be able to declare those inputs in a validated manifest. The workflow must resolve each declared input to an immutable commit SHA, report the complete resolved set to the canonical build, and must NOT compute or pass the image tag itself - the build executor derives the cache key, because only it knows the rest of what that key must cover. | critical | reusable-workflow | platform-infrastructure | active |

<!-- END delivery-controls -->

Controls are stated as **behaviour** ("never publish a container artifact", "pin the
digest into GitOps"), and every run produces **evidence** (with line numbers) plus
actionable **remediation** on failure. The checker emits a machine-readable report for
dashboards / compliance, and regenerates its own docs:

```bash
# evaluate + JSON report (uploaded as a CI artifact by the guard workflow)
python3 scripts/check-delivery-model.py .github/workflows/deploy-reusable.yaml \
 --format json --report delivery-model-report.json

# regenerate the control table in this README from the catalog
python3 scripts/check-delivery-model.py --write-docs README.md
```

Consumers can assert the behavioral contract via the workflow outputs:

```yaml
jobs:
 deploy:
 uses: coderaxis/github-actions/.github/workflows/deploy-reusable.yaml@v1
 with: { service_name: auth-service }
 secrets: inherit
 verify:
 needs: deploy
 runs-on: ubuntu-latest
 steps:
 - run: test "${{ needs.deploy.outputs.contract_version }}" = "v1"
```

### API docs (Swagger) and the single artifact

There is **one** canonical image for all environments — there is **no** "with Swagger"
and "without Swagger" build. Swagger is **never compiled into the canonical image**
(`GO_BUILD_TAGS=""`; the `//go:build !swagger` no-op stub is linked), so the *same*
swagger-less digest is promoted to `dev → staging → preprod → prod`. Building a second
docs/no-docs image would break build-once and is rejected by **DM-0002** and **DM-0005**.

Developers still get docs — just not from the deployed service:

- **Locally**: `go run -tags swagger …` links the real docs implementation.
- **Centrally**: `docs/openapi.json` is published to the API contract registry
 (`inboxxhq-api-contracts`) and served from a central OpenAPI/Swagger portal.
- **Defense-in-depth**: even if a swagger-tagged build were ever deployed, the runtime
 `swaggerpolicy.DocsEnabled(environment)` policy (dev/staging on, preprod/prod off)
 gates the endpoints. The compile-time exclusion is the primary control; this is the
 backstop. (See `platform/openapiroutes` and `platform/swaggerpolicy` in
 `platform-shared-go`.)

The delivery-model checker is the twin of
[`scripts/check-seed-contract.py`](scripts/check-seed-contract.py): both encode an
enterprise standard as a language-agnostic, stdlib-light gate rather than prose.

## Dockerfile standard (one canonical pattern, capability-declared variation)

Policy SSOT (in `coderaxis/microservices`): `ADR-0072` (Enterprise Dockerfile Standard) and
the [Enterprise Dockerfile Standard](https://github.com/coderaxis/microservices/blob/main/docs/core-docs/standards/infrastructure/dockerfile-standard.md).
Implementation: [`templates/Dockerfile.service`](templates/Dockerfile.service).
Enforcement: [`scripts/check-dockerfile-standard.py`](scripts/check-dockerfile-standard.py)
run by [`dockerfile-standard.yaml`](.github/workflows/dockerfile-standard.yaml) against every
caller, and self-checked by the `dockerfile-standard` job in this repository's own
[`ci.yaml`](.github/workflows/ci.yaml) on every
change to the catalog/checker/template. Static analysis only — never runs `docker build`,
so it cannot conflict with DM-0001 above.

### Control catalog (policy-as-code)

The controls are declared in
[`controls/dockerfile-standard.yaml`](controls/dockerfile-standard.yaml) — same
policy-only-catalog / evolvable-detector split as `controls/delivery-model.yaml`. Each
control additionally carries `applies_to_capabilities`: an empty list means it applies to
every caller; a non-empty list means it is only evaluated when the caller's declared
`capabilities` intersects it (e.g. `db-owner`-only controls don't fire for a stateless
gateway). The table below is generated from the catalog (drift-gated via `--verify-docs`):

<!-- BEGIN dockerfile-standard-controls (generated: scripts/check-dockerfile-standard.py --write-docs) -->

_Generated from `controls/dockerfile-standard.yaml` by `scripts/check-dockerfile-standard.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Capability scope | Owner | Status |
| ------- | ------ | -------- | ----------------- | ----- | ------ |
| DS-0001 | Exactly one builder stage named `builder` (golang base) and exactly one unnamed runtime stage (alpine base). No third stage. | critical | all | platform-infrastructure | active |
| DS-0002 | The builder stage's FROM MUST use `--platform=$BUILDPLATFORM`, and the build MUST set `GOARCH=${TARGETARCH}` (or equivalent) so the target architecture is cross-compiled, not emulated. | major | all | platform-infrastructure | active |
| DS-0003 | The builder and runtime image tags MUST resolve to the values pinned in dockerfile-version-matrix.yaml (currently `1.26-alpine3.24` / `3.24`). The tag may be written literally or threaded through the sanctioned BUILDER_IMAGE_TAG / RUNTIME_IMAGE_TAG ARGs, but if an ARG is used it MUST carry a default, because an ARG with no default has no version to check. A Dockerfile MUST NOT pin a different version independently, and MUST NOT use a floating tag that omits the Alpine sub-version (e.g. bare `golang:1.26-alpine`). | major | all | platform-infrastructure | active |
| DS-0004 | No `FROM ...:latest` (implicit or explicit) and no unpinned `FROM` (a bare `image_name` with no tag at all, which Docker resolves to `:latest`). | critical | all | platform-security | active |
| DS-0005 | The runtime stage MUST create its user with an explicit, pinned numeric UID/GID (matching dockerfile-version-matrix.yaml `runtime_user`, currently 10001:10001) and the final `USER` instruction MUST reference that numeric UID:GID, never a bare symbolic username with no numeric pin, and never root (UID 0 / no USER instruction at all). | critical | all | platform-security | active |
| DS-0006 | A HEALTHCHECK instruction MUST be present, and its target port MUST equal the port in EXPOSE. | major | http-api, gateway | platform-infrastructure | active |
| DS-0007 | A `worker`-only caller (no `http-api`) MUST NOT use an HTTP HEALTHCHECK (no HTTP port is bound) but MUST still declare SOME HEALTHCHECK (process/file/socket-based) - omitting HEALTHCHECK entirely is not an acceptable substitute. | minor | worker | platform-infrastructure | active |
| DS-0008 | The runtime stage MUST carry LABEL org.opencontainers.image.title, .source, .vendor, .licenses, .revision, .created, and com.coderaxis.capabilities. | major | all | platform-infrastructure | active |
| DS-0009 | CMD (or ENTRYPOINT) MUST be JSON exec form (`["./binary"]`), never a shell string. No ENTRYPOINT shell-script wrapper (`entrypoint.sh`) is permitted - the compiled binary IS the entrypoint. | critical | all | platform-infrastructure | active |
| DS-0010 | `STOPSIGNAL SIGTERM` MUST be declared explicitly, even though it is Docker's default. | minor | all | platform-infrastructure | active |
| DS-0011 | A caller that declares (or structurally has - see DS-0013) the `db-owner` capability MUST build `./cmd/dbtool` into an image binary, COPY it into the runtime stage, and carry the marker comment `# dbtool binary path: /app/<binary>`. | critical | db-owner | platform-architecture | active |
| DS-0012 | A caller that declares the `seed` capability SHOULD build/ship a seed binary with the standard marker. This is intentionally ADVISORY, not a hard gate: seed-contract- check.yml is the authoritative, hard-enforcing check for the full seeding contract (binary + data tree + placeholder-only qualified envs). This control exists only so the two checks' findings can be compared, never to duplicate seed-contract-check.yaml's enforcement. | minor | seed | platform-infrastructure | active |
| DS-0013 | The `--capabilities` the caller declares MUST match what the repo structurally contains: `db-owner` iff go.mod requires a `*-core-postgres` module; `seed` iff `cmd/seed` exists; `backfill` iff `cmd/backfill` exists; `canary` iff `cmd/canary` exists. | critical | all | platform-architecture | active |
| DS-0014 | No `sqlc generate`, `protoc`/`buf generate`, `swag init`, or `openapi-generator` may run inside the Dockerfile. Generated code is committed; the image only compiles it. | critical | all | platform-architecture | active |
| DS-0015 | Use `COPY`, never `ADD` (ADD's implicit remote-URL-fetch and auto-extract behavior is unpinned, unaudited functionality this standard does not permit). | minor | all | platform-security | active |
| DS-0016 | A Dockerfile MUST NOT declare an ARG outside the set named in dockerfile-version-matrix.yaml `dockerfile_args` for anything that pins a tool/image/language version. | major | all | platform-infrastructure | active |
| DS-0017 | Private Go module credentials MUST be supplied via `--mount=type=secret,id=gh_token` and MUST NOT appear as a plain `ARG`/`ENV` or be baked into any layer. | critical | all | platform-security | active |
| DS-0018 | `apk add` in the builder stage installs only from {ca-certificates, git, tzdata}; in the runtime stage, only from {ca-certificates, tzdata, wget}. Any other package requires an ADR amendment (recorded in the version matrix). | minor | all | platform-security | active |

<!-- END dockerfile-standard-controls -->

## Documentation governance (one contract for every governed docs repo)

Policy SSOT (in `coderaxis/core-docs`): `ADR-0081` (Centralized, reusable
documentation-governance CI).
Implementation: [`scripts/check-docs-governance.py`](scripts/check-docs-governance.py) run by
[`docs-governance.yaml`](.github/workflows/docs-governance.yaml) against every governed docs repo,
and self-checked by the `docs-governance` job in this repository's own
[`ci.yaml`](.github/workflows/ci.yaml) on
every change to the catalog / checker / fixture. Static analysis only — no repo code is executed.

Every governed documentation repository (the shared-engine `core-docs`, and each client
`*-platform-docs`) carries only a thin caller:

```yaml
# .github/workflows/docs-governance.yaml
on:
 pull_request:
 paths: ["**/*.md", "catalog/**", "generated/**", "governance/**", ".github/workflows/docs-governance.yaml"]
 push: { branches: ["**"] }
 workflow_dispatch: {}
permissions:
 contents: read
jobs:
 docs-governance:
 uses: coderaxis/github-actions/.github/workflows/docs-governance.yaml@v1
 with:
 docs_root: . # a docs-in-monorepo repo passes its subdir, e.g. docs/core-docs
 # fail_on: major # default; tighten to `minor` once a repo is clean
```

**Shared logic, per-repo data.** The checker logic is central and identical; a repo owns only its
data: `governance/OWNER_DIRECTORY.md`, an optional `governance/CLIENT_SCOPE.md`, and its
`catalog/` + `catalog/schema/`. The checker **auto-detects** capabilities — no `CLIENT_SCOPE.md`
skips client-scope isolation (so the shared engine, which legitimately names every client, is
never subject to it); no `catalog/` skips catalog governance. A repo may **extend** (never shrink)
the doc-type / doc-root vocabulary via an optional `governance/docs-governance.yaml`.

### Control catalog (policy-as-code)

The controls are declared in [`controls/docs-governance.yaml`](controls/docs-governance.yaml) —
same policy-only-catalog / evolvable-detector split as `controls/delivery-model.yaml`. Each
control carries `applies_when` (`always` | `client-scope` | `catalog`); a control is skipped when
its capability is absent. **critical/major** controls fail CI; **minor** controls are advisory
(`--fail-on`). Catalog schema + generated-artifact drift (**DOC-0010**) is domain-specific and is
**delegated** to the repo's own `scripts/build_catalog.py --check` invoked by the workflow, never
re-implemented centrally. Control IDs (`DOC-NNNN`) are **stable and permanent**. The table below is
**generated** from the catalog (drift-gated via `--verify-docs`):

<!-- BEGIN docs-governance-controls (generated: scripts/check-docs-governance.py --write-docs) -->

_Generated from `controls/docs-governance.yaml` by `scripts/check-docs-governance.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Applies when | Owner | Status |
| ------- | ------ | -------- | ----- | ------------ | ----- | ------ |
| DOC-0001 | Every Markdown document under a governed doc root that carries YAML frontmatter must open and close its `---` fence, parse as a YAML mapping, contain no tab characters, and declare every required key (owner, status, last_reviewed, review_cycle, related_services, related_rfcs, related_adrs) with the correct type. | critical | document | always | platform-architecture | active |
| DOC-0002 | Each governed document's `owner` must be a slug defined in governance/OWNER_DIRECTORY.md. Template documents (under a templates/ path) are exempt. | critical | document | always | platform-architecture | active |
| DOC-0003 | `status`, `review_cycle`, and (when present) `doc_type`, `tier`, `service_tier`, `criticality` must be drawn from the platform's controlled vocabularies, and `last_reviewed` must be an ISO (YYYY-MM-DD) date. A repo may EXTEND doc_type/doc_roots via its optional docs-governance config, never shrink the shared vocabulary. | major | document | always | platform-architecture | active |
| DOC-0004 | related_services / related_rfcs / related_adrs must each be declared exactly once, in block-list style (`key:` followed by ` - item` lines) or empty `[]`. Legacy singular keys (related_rfc, related_adr) are forbidden. | major | document | always | platform-architecture | active |
| DOC-0005 | When any related_* frontmatter list is non-empty, the document body must contain a "Related Docs" section that links each referenced item. | major | document | always | platform-architecture | active |
| DOC-0006 | A decision record with status `superseded` must declare `superseded_by`; declaring `superseded_by` implies status `superseded`; and `supersedes`/`superseded_by` must be reciprocal and reference decision records that exist. | major | decision-record | always | platform-architecture | active |
| DOC-0007 | A document whose `review_cycle` is quarterly/semiannual/annual must have been reviewed within that window (today <= last_reviewed + cycle window + grace). `event-driven` docs have no calendar SLA and are exempt. | minor | document | always | platform-infrastructure | active |
| DOC-0008 | In a repository that declares client scope (governance/CLIENT_SCOPE.md), no document may contain a forbidden sibling-client term (whole-word, case-insensitive) listed in that policy's `client_scope.forbidden_terms`. | critical | repository | client-scope | platform-security | active |
| DOC-0009 | Every slug registered in governance/OWNER_DIRECTORY.md should own at least one governed document. Advisory: unused slugs are reported, not blocked. | minor | repository | always | platform-architecture | active |
| DOC-0010 | A repository with a catalog/ directory must keep every catalog file valid against its JSON Schema, its cross-references resolvable, and its generated artifacts regenerated from source (no drift). Because the catalog domain model and its renderers are repo-specific, detection is DELEGATED to the repo's own scripts/build_catalog.py --check, invoked by the docs-governance reusable workflow - this checker never re-implements it. | critical | catalog | catalog | platform-infrastructure | active |
| DOC-0011 | Document folders are plural - adrs/, rfcs/, prds/ - never adr/, rfc/, prd/ or product/. A doc root the repository explicitly declares in extra_doc_roots must exist on disk. Default roots may be absent, since no repository carries all of them. | critical | repository | always | platform-architecture | active |
| DOC-0012 | A governed document that cites a source file in backticks must cite a path that exists, and must not append a line number. Repo-local paths are resolved against the docs repository; paths into other repositories are resolved only when the run supplies a checkout via --source-root (or the source_roots config key), and are skipped otherwise so the control never fails on an unavailable tree. | minor | document | always | platform-architecture | active |
| DOC-0013 | A document in adrs/, rfcs/ or prds/ is named PREFIX-NNNN-kebab-case-slug.md, where PREFIX agrees with the folder (ADR/RFC/PRD), NNNN is exactly four zero-padded digits, and the slug is lowercase alphanumerics separated by single hyphens. Folder READMEs, generated indexes such as ADR_INDEX.md, `_`-prefixed partials and templates/ are exempt. Supporting prose in nested subfolders (rfcs/api/, rfcs/data/docker/) is exempt unless it claims a PREFIX, so a malformed ADR-*.md is caught wherever it is filed. | critical | document | always | platform-architecture | active |
| DOC-0014 | No two documents in one documentation namespace may claim the same PREFIX-NNNN id. Uniqueness is scoped to the namespace, never across namespaces: each namespace (core-docs, and each client hub) allocates its own numbers, so core-docs ADR-0001 and a client hub's ADR-0001 are both legitimate documents and are not duplicates. | critical | repository | always | platform-architecture | active |
| DOC-0015 | A bare document citation (ADR-0069) MUST resolve to a document in the citing repository's own namespace, or to an identifier declared in governance/RESERVED_IDS.md. A qualified citation (Core ADR-0069) MUST resolve in the namespace its qualifier names. Frontmatter, fenced code blocks and markdown link targets are not citations. A peer namespace not present in the run is reported as skipped, never as passing. | major | document | always | platform-architecture | active |
| DOC-0016 | A citation that leaves its namespace MUST use that namespace's registered qualifier from controls/doc-namespaces.yaml, spelled exactly (Core, not core or core-docs), and its first mention in each document MUST additionally be a resolvable markdown link. Later mentions in the same document need only the qualifier. | major | document | always | platform-architecture | active |
| DOC-0017 | A document in adrs/, rfcs/ or prds/ MUST declare `id: PREFIX-NNNN` in its frontmatter, and that value MUST equal the identifier in its filename. The filename is authoritative when the two disagree. Documents with no identifier - standards, runbooks, hubs, indexes, templates - MUST NOT declare the key. | critical | document | always | platform-architecture | active |
| DOC-0018 | Every identifier below the highest allocated one in a namespace MUST be either an existing document or an entry in governance/RESERVED_IDS.md (reserved_ids or known_gaps). Identifiers at or above 9000 are a reserved band for transitional documents and are outside the sequence. Allocation is per namespace; namespaces never coordinate numbers. | major | repository | always | platform-architecture | active |
| DOC-0019 | A relative markdown link in a governed document MUST resolve to a file that exists, and if it carries an anchor, that anchor MUST match a heading in the target document. External URLs and template placeholders containing '{{' are out of scope. | minor | document | always | platform-architecture | active |
| DOC-0020 | Every ```mermaid block in a governed document MUST compile with @mermaid-js/mermaid-cli. Executed by the docs-governance reusable workflow, not by the Python checker. | minor | document | always | platform-architecture | active |
| DOC-0021 | Governed documents MUST pass the shared markdownlint ruleset at config/docs/.markdownlint.jsonc. A repository MAY add rules, and SHOULD NOT remove them. Executed by the docs-governance reusable workflow. | minor | document | always | platform-architecture | active |
| DOC-0022 | Governed prose MUST pass cspell using the shared configuration at config/docs/cspell.json and the shared vocabulary at config/docs/dictionaries/platform.txt. Platform-wide terms belong in the shared dictionary; a repository MAY add cspell-words.txt for terms specific to it. Code spans, fenced blocks, link targets, frontmatter and document identifiers are excluded. Executed by the docs-governance reusable workflow. | minor | document | always | platform-architecture | active |
| DOC-0023 | Every ADR MUST declare a `domain` in frontmatter, and any document that declares one MUST draw the value from the vocabulary its namespace publishes as `domain_vocabulary` in docs-governance.yaml. The vocabulary is namespace-scoped by design: the platform namespace groups decisions by architectural concern (api, messaging, security) and a client hub groups them by business domain (conversations, eventing, encryption). A value naming a component or a service rather than a concern MUST NOT be added. RFCs and PRDs are not required to declare a domain - they carry tier/capability instead - but are validated when they do. | major | document | always | platform-architecture | active |
| DOC-0024 | A namespace MAY declare `platform_concern_domains` - a closed subset of its domain vocabulary for decisions that provision or constrain the substrate rather than realizing a business bounded context (cloud account topology, infrastructure repository layout, wire transport). An ADR tagged with one of those values MUST declare `core_authority`: the platform-namespace decision ids that bind it, or an explicit empty list recording that none does. Declared ids MUST resolve in the platform namespace when it is present in the run. The key names the namespace, so ids are written bare and do not collide with the local sequence. | major | document | always | platform-architecture | active |

<!-- END docs-governance-controls -->

## Service API contract (does the service obey the contract decisions, or merely have a spec?)

`openapi-contract.yaml` already proved a service's spec exists, lints, and that the service's own
contract tests pass. Every one of those gates is opt-in via a caller-supplied command, so a service
could satisfy the workflow completely while shipping a Swagger UI, inventing its own response
envelope, hand-rolling a copy of the shared conformance suite, and committing four rival spec
files — and 37 of 38 services did at least one of those.

`scripts/check-api-contract.py` runs as an **unconditional step inside that same workflow**, with no
caller input to disable it and its catalog checked out from this repository, so a service cannot
weaken the policy it is judged by. `check-workflow-centralization.py` separately forbids forking the
workflow, so it cannot escape by keeping its own copy either.

**The ratchet is what makes it enforceable today.** Every control is a count compared against the
repo's committed `.api-contract-baseline.json`. A count that rises fails the build; a count that
holds is tolerated; a count that falls prompts a baseline rewrite. **A repo with no baseline is held
to zero**, so a service created tomorrow cannot introduce any of this, while the services already
carrying debt are frozen rather than broken. Deleting the last baseline in a repo is the moment its
migration is provably finished.

```bash
python3 scripts/check-api-contract.py path/to/service        # check
python3 scripts/check-api-contract.py path/to/service --write-baseline   # freeze / ratchet down
```

### The contract-vintage gap, and the two controls that close it

The chain behind a published spec is `proto -> generated Go -> the service's OpenAPI components`.
Two of those links were already gated and one was not, in a way no per-repo check could have found.

`platform-contracts` CI gates the first link properly: `buf lint`, `buf breaking` against the base
branch, and a codegen-drift job that re-runs the generator and diffs the committed `.pb.go` and
TypeScript output. The third link is gated per repository by the byte-exact `--check` and by API-0004.

The gap was in between. **Every service projects its `common.v1` components from its own
`platform-contracts-go` pin**, so a per-repo drift check can only ever prove internal consistency —
two services on different pins both pass while publishing different envelopes. Measured on
2026-07-29 the fleet held four concurrent pins (v0.1.0 in 13 repos, v0.2.0 in 3, v0.3.0 in 63,
v0.4.0 in 1), and `platform-shared-go`, which *hosts* the projection, was three releases behind at
v0.1.0. Concretely, `common.v1.ErrorCode` had 28 values under the engine's pin and 30 under the
reference service's: the fleet disagreed about which error codes exist, with every gate green.

Two paired controls close it, because either alone is escapable:

- **`controls/module-floors.yaml`**, enforced by `check_module_pins.py`, sets the minimum
  `platform-contracts-go` version. Below the floor is a warning on dev/staging and an error on
  preprod/prod, so a stale contract vintage cannot reach a deployed environment while the fleet is
  still being moved. A floor is a floor and not an exact pin because pinning 86 repos to one version
  would make every module release a fleet-wide breaking change.
- **API-0007** compares the spec's `common.v1.*` components byte-for-byte against
  `controls/common-v1-components.json`. That artifact is generated, never hand-written:

```bash
# in platform-shared-go, redirected into this repo
go run ./platform/openapicontract/commonv1policy/cmd/emit-canonical-components \
  > ../github-actions/controls/common-v1-components.json
```

The floor makes a service *depend on* a current contract module; API-0007 makes it actually *publish*
current components. API-0007 exits 2 rather than passing if the reference artifact is missing, since
a version gate that reports success because it could not find its reference is worse than no gate.

### Control catalog (policy-as-code)

Generated from `controls/api-contract.yaml`, drift-gated via `--verify-docs`:

<!-- BEGIN api-contract-controls (generated: scripts/check-api-contract.py --write-docs) -->

_Generated from `controls/api-contract.yaml` by `scripts/check-api-contract.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Applies | Owner | Status |
| --- | --- | --- | --- | --- | --- | --- |
| API-0001 | No runtime Go source may import swaggerpolicy or gin-swagger, call openapiroutes.Register*, carry a `swagger` build tag, or register a /swagger or /api/docs route. The contract reaches consumers through the API Contract Registry and the API Portal's per-environment mirror, never from the service process. | critical | source | always | platform-architecture | active |
| API-0002 | docs/ may contain openapi.json (generated), openapi.base.json (authored metadata only), and the operationId lock and schema. Any other openapi*.json is a rival spec and is prohibited. | major | spec | always | platform-architecture | active |
| API-0003 | A test that validates live traffic against the OpenAPI document must import platform/openapicontract/conformance. Driving kin-openapi directly - constructing a gorillamux router or calling openapi3filter.ValidateResponse - is a hand-rolled copy of the shared suite and is prohibited. | major | source | http-api | platform-architecture | active |
| API-0004 | Every 2xx JSON response must reference common.v1.SuccessResponse and every 4xx/5xx JSON response must reference common.v1.ErrorResponse. The envelope is owned by proto/common/v1 and is not redefinable per service. | critical | spec | http-api | platform-architecture | active |
| API-0005 | Every operation carries a unique operationId, and the service commits docs/openapi.operationids.lock.json recording each id with the version that introduced it, its deprecation state, and its visibility. | major | spec | http-api | platform-architecture | active |
| API-0007 | Every common.v1.* component in the service's spec must be byte-identical to the projection in controls/common-v1-components.json, which is generated from the proto SSOT by commonv1policy and carries the platform-contracts-go version it was projected from. A service publishing no common.v1 components is governed by API-0004 instead, not failed twice here. | major | spec | http-api | platform-architecture | active |
| API-0006 | cmd/server/swagger_main.go and any other swaggo annotation source is prohibited. The canonical generator reflects Go types through the shared contract engine. | minor | source | always | platform-architecture | active |
| API-0008 | A GET returning a single mutable resource must declare a strong ETag response header, and a PUT, PATCH or DELETE on such a resource must declare the If-Match request header together with 412 Precondition Failed and 428 Precondition Required responses. | major | spec | http-api | platform-architecture | active |
| API-0009 | Runtime code that sets Cache-Control with a directive permitting storage must set Vary in the same handler, naming every request header the body depends on, including Authorization when the response is authenticated. A no-store response needs no Vary, and neither does a response that is genuinely identical for every caller and marked public. | major | source | always | platform-architecture | active |
| API-0010 | X-RateLimit-* response headers are prohibited. A rate-limited response carries the standard rate-limit fields, and a 429 also carries Retry-After. | major | source | always | platform-architecture | active |
| API-0011 | An operation using the PATCH method must declare application/merge-patch+json or application/json-patch+json as a request media type, and must not declare bare application/json. | major | spec | http-api | platform-architecture | active |
| API-0012 | An operation whose handler honours Idempotency-Key must declare that header as a parameter, and must declare whether it is required. | major | service | http-api | platform-architecture | active |
| API-0013 | docs/openapi.json must declare the OpenAPI version this platform has pinned. The pinned value is 3.0.3 and is owned by RFC-0038 section 1. | critical | spec | http-api | platform-architecture | active |
| API-0014 | Runtime code that makes an outbound HTTP request must use the shared instrumented client or transport. http.DefaultClient, http.Get/Post/Head and a bare &http.Client{} literal are prohibited in runtime code. | major | source | always | platform-architecture | active |
| API-0015 | A repository serving a browser-reachable surface must set Strict-Transport-Security, X-Content-Type-Options, Referrer-Policy and Permissions-Policy, and must set Content-Security-Policy if it serves HTML. | major | source | http-api | platform-architecture | active |

<!-- END api-contract-controls -->

## Workflow centralization (are the central workflows actually being used?)

Every catalog above governs what a central workflow **does** once it is called. None of them asks
whether a repository **calls** it at all — so a repo could keep an entire bespoke implementation of a
workflow published here and violate nothing, because all the controls are scoped to the reusable
workflow rather than to its adoption. Centralization was a convention, and conventions decay
quietly: the local copy keeps passing while the central version gains a check or a fix, so the repo
runs an older policy than the fleet believes it runs, and CI being green is exactly what hides it.

[`controls/workflow-centralization.yaml`](controls/workflow-centralization.yaml) closes that gap for
the part of the boundary that is unambiguous. The central set is **derived**, not listed: any
workflow here that declares `on.workflow_call` is automatically in scope, so publishing or retiring
a reusable workflow needs no edit to the catalog.

```bash
# check one repo, or many
python3 scripts/check-workflow-centralization.py path/to/repo
python3 scripts/check-workflow-centralization.py ../services/*/*  --format json

# regenerate the control table in this README from the catalog
python3 scripts/check-workflow-centralization.py --write-docs README.md
```

What it deliberately does **not** claim: WFC-0001 matches on workflow filename, which is a strong
signal with no false positives across the current fleet but is not proof — a repo that reimplements a
central workflow's logic under an unrelated name is not detected. It also does not assert that every
repo must call every published workflow, since applicability is a property of the repo (a docs repo
has no schema to check) and a required-adoption matrix would duplicate the archetype registry
(ADR-0064). The narrower rule is provable: if you keep your own copy of something published here,
that is drift.

There is still no ADR defining the intended **scope** of fleet-wide CI centralization — which
workflow families belong here and which are legitimately per-repo. ADR-0081 covers documentation
governance only. These controls enforce the boundary that is already clear and stay silent on the
open question.

### Control catalog (policy-as-code)

<!-- BEGIN workflow-centralization-controls (generated: scripts/check-workflow-centralization.py --write-docs) -->

_Generated from `controls/workflow-centralization.yaml` by `scripts/check-workflow-centralization.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Owner | Status |
| ------- | ------ | -------- | ----- | ----- | ------ |
| WFC-0001 | A consumer repository MUST NOT contain a workflow whose filename matches a workflow that coderaxis/github-actions publishes as reusable (`on.workflow_call`) unless that workflow is a caller of it — that is, unless it contains `uses: coderaxis/github-actions/.github/workflows/<name>@<ref>`. | major | caller-workflow | platform-infrastructure | active |
| WFC-0002 | A call to a coderaxis/github-actions reusable workflow MUST reference a major version tag (`@v1`, `@v2`, ...). It MUST NOT reference a branch (`@main`), an exact patch tag (`@v1.6.0`), or a commit SHA. | major | caller-workflow | platform-infrastructure | active |
| WFC-0004 | A job whose `uses:` targets a reusable workflow MUST grant, at job level, every permission that the called workflow declares at its own workflow level, at or above the declared access (`write` satisfies `read`; `read` does not satisfy `write`). A job that declares no `permissions:` block inherits the calling workflow's, and that inherited set is what is checked. | critical | caller-workflow | platform-infrastructure | active |
| WFC-0005 | A repository MUST NOT contain a workflow that performs a dependency bump, nor one that subscribes to a `repository_dispatch` event of the form `<module>-released`. Version propagation is performed by the central release, which resolves consumers from the platform artifact graph and opens the pull request directly. | major | caller-workflow | platform-infrastructure | active |
| WFC-0006 | A repository's `.github/workflows` directory MUST contain only `ci.yaml`, `release.yaml`, workflows that the repository itself PUBLISHES as reusable (`on.workflow_call`), and thin callers - a file named after a workflow this platform publishes that does nothing but call that same workflow. Any other workflow file is a finding, whether or not its behaviour duplicates something central. Every YAML file on the platform MUST use the `.yaml` extension; `.yml` is a finding on its own, independent of the filename. | major | caller-workflow | platform-infrastructure | active |

<!-- END workflow-centralization-controls -->

## CI identity (`controls/ci-identity.yaml`)

Guards the GitHub-OIDC `sub` patterns in the Terraform trust policies that let CI assume AWS roles.

GitHub's immutable `sub` claim embeds numeric owner and repository ids —
`repo:OWNER@OWNER-ID/REPO@REPO-ID:ref:...` — and a repository adopts that format when it is created,
renamed or transferred. A policy matching only the older name-based spelling therefore stops admitting
a repository at the moment somebody renames it, with no change to the policy and no signal until a
deploy fails somewhere else. That has cost four separate outages here, each found by the outage rather
than by a check, and the first fix was applied to one role while three others with identical exposure
sat in the same directory.

Two things these controls deliberately do not claim. They read Terraform **source**, so a green result
says the configuration is right, not that the account is — and those have differed here, since one
earlier fix was applied with `aws iam update-assume-role-policy` before its Terraform landed.
Detecting that needs AWS read access and is a separate control. They also parse text rather than
evaluating HCL, so a construct the checker cannot decompose is reported rather than skipped: a
credential boundary it cannot read is not evidence that the boundary is sound.

```bash
./scripts/check-ci-identity.py path/to/inboxxhq-infra
./scripts/check-ci-identity.py path/to/repo --format json
```

### Control catalog (policy-as-code)

<!-- BEGIN ci-identity-controls (generated: scripts/check-ci-identity.py --write-docs) -->

_Generated from `controls/ci-identity.yaml` by `scripts/check-ci-identity.py --write-docs` — do not edit by hand._

| Control | Policy | Severity | Scope | Owner | Status |
| ------- | ------ | -------- | ----- | ----- | ------ |
| CID-0001 | Every `token.actions.githubusercontent.com:sub` condition MUST offer at least one pattern in the immutable-identifier spelling — an owner segment carrying `@<owner-id>`. The classic name-only spelling MAY remain alongside it, and generally should, because repositories created before 2026-07-15 keep emitting it until they are renamed, transferred or opted in. | major | terraform | platform-infrastructure | active |
| CID-0002 | Within one `sub` condition, the set of subject suffixes (everything after the repository segment — `ref:refs/heads/main`, `pull_request`, `environment:production`) reachable under the classic spelling MUST equal the set reachable under the immutable spelling. | major | terraform | platform-infrastructure | active |
| CID-0003 | In an immutable-spelling pattern the owner id MUST be a pinned value — literal digits, or an interpolation of a variable holding them. `@*` in the owner segment is a finding. | critical | terraform | platform-infrastructure | active |
| CID-0004 | The repository segment of an immutable-spelling pattern MUST be either `*` (the role trusts the owner's repositories collectively) or `*@<pinned-repo-id>` (the role trusts exactly one repository). A literal repository name in that segment is a finding, whether the id beside it is pinned, wildcarded or absent. | critical | terraform | platform-infrastructure | active |

<!-- END ci-identity-controls -->

## Versioning

- Consumers pin the **major** tag `@v1`, which is a moving tag updated to the latest `v1.x.y`.
- Breaking changes bump to `@v2`.
