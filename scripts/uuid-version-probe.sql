-- Value-level UUID version probe for the canonical outbox.
--
-- WHAT THIS IS FOR, AND WHAT IT IS NOT FOR.
--
-- check-uuid-version-policy.py closes the producing-repo half of the policy: it
-- decides, from source, that no deterministic value is handed to event_id and no
-- fresh value is handed to idempotency_key. It cannot decide the other half.
-- A UUID that arrives over Kafka or gRPC from another service and is copied onto
-- an outbox column has no syntactic origin in the repo that writes it, so no
-- static pass can know its version. Only a row can answer that.
--
-- This is therefore NOT a pull-request gate. The CI database is created by
-- applying migrations to an empty cluster, so it contains no rows and this probe
-- would report a clean bill of health over zero data -- the exact false pass that
-- makes a gate worse than nothing. It is an operational check, to be run against
-- a POPULATED environment, and it is the missing detection for
-- OINV-OUTBOX-IDEMPOTENCY-KEY-PER-OCCURRENCE
-- (docs/core-docs/catalog/operational-invariants/postgres.yaml), which is
-- registered severity: critical with detection.state: absent.
--
-- POSTGRES VERSION. uuid_extract_version(uuid) is built in from PostgreSQL 18,
-- which is what the fleet's schema-compatibility job pins
-- (public.ecr.aws/docker/library/postgres:18) and what the DDL's DEFAULT uuidv7()
-- already requires. Verified on 18.4: uuidv7() -> 7, gen_random_uuid() -> 4, a
-- v5 value -> 5. On an older server, substitute the bit-masking form, which
-- reads the version nibble straight out of the hex text and needs no builtin:
--
--     ('x' || substr(replace(u::text, '-', ''), 13, 1))::bit(4)::int
--
-- Verified equivalent on 18.4 for the same inputs.
--
-- NULL IS NOT A VIOLATION. uuid_extract_version() returns NULL for the all-zero
-- UUID, which RFC-0032 §4.1 uses as the deliberate platform-global org sentinel
-- (outbox.PlatformGlobalOrgID). Every predicate below is written so a NULL
-- version is counted separately and never silently folded into a violation.
--
-- SCOPE. Only the three columns for which a document actually states a version:
-- id and event_id (v7) and idempotency_key (v5, the sanctioned exception).
-- outbox-ddl-standard §2.1 records the other six UUID columns as "Unspecified -
-- no doc states a version" and says the standard MUST NOT invent one, so this
-- probe does not either.
--
-- USAGE
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/uuid-version-probe.sql
-- Every offending row is reported with its identity, so a finding is actionable
-- rather than a count.

\set ON_ERROR_STOP on

-- Discovered the same way scripts/schema-compat.sh discovers them, so the probe
-- and the schema gate never disagree about what an outbox table is.
DO $probe$
DECLARE
  -- Each entry is (column, required version, control id, why it matters).
  checks CONSTANT text[][] := ARRAY[
    ['event_id',        '7', 'UUID-0001',
     'ADR-0071 decision 1: fresh per-occurrence identity. The shared publisher mints it only when the caller leaves it zero, so a supplied non-v7 value reaches the column untouched and DEFAULT uuidv7() never fires'],
    ['idempotency_key', '5', 'UUID-0002',
     'ADR-0071 decision 2: deterministic dedup identity. A fresh value satisfies NOT NULL and outboxwritepath''s non-null check while silently disabling every consumer ledger that keys on it'],
    ['id',              '7', 'UUID-0007',
     'RFC-0032 section 3: time-sortable storage key. A non-v7 value means an INSERT named the column and bypassed DEFAULT uuidv7()']
  ];
  tbl       text;
  chk       text[];
  offenders bigint;
  detail    text;
  total     bigint := 0;
  scanned   int    := 0;
BEGIN
  IF current_setting('server_version_num')::int < 180000 THEN
    RAISE EXCEPTION
      'uuid_extract_version() needs PostgreSQL 18+ (this server is %); use the bit-masking form documented at the top of this file',
      current_setting('server_version');
  END IF;

  -- Outbox tables are discovered the same way scripts/schema-compat.sh discovers
  -- them, so the probe and the schema gate never disagree about what one is.
  FOR tbl IN
    SELECT c.relname
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p')
       AND NOT c.relispartition
       AND c.relname LIKE '%\_outbox'
     ORDER BY 1
  LOOP
    scanned := scanned + 1;
    FOREACH chk SLICE 1 IN ARRAY checks LOOP
      EXECUTE format(
        'SELECT count(*) FROM %I WHERE uuid_extract_version(%I) IS DISTINCT FROM %s',
        tbl, chk[1], chk[2]) INTO offenders;
      CONTINUE WHEN offenders = 0;

      total := total + offenders;
      RAISE WARNING '[%] %.%: % row(s) are not UUIDv% - %',
        chk[3], tbl, chk[1], offenders, chk[2], chk[4];

      -- Report the offending rows, not just a count: a count cannot be acted on.
      EXECUTE format($q$
        SELECT string_agg(
                 format('    %%s=%%s version=%%s event_type=%%s created_at=%%s',
                        %L, %I, coalesce(uuid_extract_version(%I)::text, 'nil (all-zero sentinel)'),
                        event_type, created_at),
                 E'\n' ORDER BY created_at DESC)
          FROM (SELECT * FROM %I
                 WHERE uuid_extract_version(%I) IS DISTINCT FROM %s
                 ORDER BY created_at DESC LIMIT 20) s
      $q$, chk[1], chk[1], chk[1], tbl, chk[1], chk[2]) INTO detail;
      IF detail IS NOT NULL THEN
        RAISE WARNING E'\n%', detail;
      END IF;
    END LOOP;
  END LOOP;

  IF scanned = 0 THEN
    RAISE EXCEPTION
      'no outbox table found in schema public; refusing to report a pass from an empty scan';
  END IF;

  IF total > 0 THEN
    RAISE EXCEPTION 'uuid-version-probe: % row-level violation(s) across % outbox table(s)',
      total, scanned;
  END IF;

  RAISE NOTICE 'uuid-version-probe: OK - % outbox table(s), no row violates a documented UUID version',
    scanned;
END
$probe$;
