-- +goose Up
--
-- What an older build's own statements trip on (SCHEMA-0007): each class was shown on
-- PostgreSQL 18, the older statement passing before this migration and failing after.

-- A unique index rebuilt on other columns: ON CONFLICT (id, tier) finds no index.
DROP INDEX IF EXISTS uq_broken_windows_tier;
CREATE UNIQUE INDEX uq_broken_windows_tier ON broken_windows (id, tier, delivery_state);

-- A unique index new on a table an older build writes: its duplicate is refused.
CREATE UNIQUE INDEX uq_broken_threads_note ON broken_threads (note);

-- NOT VALID leaves the rows already there alone and still checks every new one.
ALTER TABLE broken_threads ADD CONSTRAINT chk_broken_threads_note CHECK (length(btrim(note)) > 0) NOT VALID;

-- A default an older INSERT relies on.
ALTER TABLE broken_windows ALTER COLUMN tier DROP DEFAULT;

-- Row level security forced on the table owner's own statements.
ALTER TABLE broken_threads FORCE ROW LEVEL SECURITY;

-- A privilege the older build's role uses.
REVOKE UPDATE ON broken_threads FROM PUBLIC;

-- A function back with another signature: a call of broken_count(uuid) fails.
DROP FUNCTION IF EXISTS broken_count(uuid);
CREATE FUNCTION broken_count(id uuid, org uuid) RETURNS bigint LANGUAGE sql AS 'SELECT 1';
