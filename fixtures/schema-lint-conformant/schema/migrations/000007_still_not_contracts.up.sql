-- +goose Up
--
-- More shapes that refuse nothing an older build does (SCHEMA-0007), in a file with no
-- `-- expand-contract:` marker so each is exercised on its own.

-- 1. A unique index on a column this migration adds: an older build leaves it NULL, and
--    NULLs do not collide.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS external_ref TEXT;
CREATE UNIQUE INDEX uq_fixture_threads_external_ref ON fixture_threads (external_ref);

-- 2. A partial unique index whose predicate no row of an older build meets.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX uq_fixture_threads_one_pinned ON fixture_threads (org_id) WHERE pinned;

-- 3. A unique index on an identity column: the database numbers every row.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS seq BIGINT GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX uq_fixture_threads_seq ON fixture_threads (seq);

-- 4. A CHECK over an existing column that allows every value its earlier CHECK did
--    (000003: conversation, official, group).
ALTER TABLE fixture_threads ADD CONSTRAINT chk_fixture_threads_kind_known
    CHECK (kind IN ('conversation', 'official', 'group', 'broadcast')) NOT VALID;

-- 5. A privilege taken from a function this migration creates.
CREATE FUNCTION fixture_secret() RETURNS int LANGUAGE sql AS 'SELECT 1';
REVOKE ALL ON FUNCTION fixture_secret() FROM PUBLIC;

-- 6. A replace that only adds a condition on a column this migration adds: for an older
--    build it is the rule it replaces.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS pinned_by UUID;
ALTER TABLE fixture_threads DROP CONSTRAINT IF EXISTS chk_fixture_threads_closed_has_body;
ALTER TABLE fixture_threads ADD CONSTRAINT chk_fixture_threads_closed_has_body
    CHECK ((closed_at IS NULL OR body IS NOT NULL) AND (pinned_by IS NULL OR pinned)) NOT VALID;
