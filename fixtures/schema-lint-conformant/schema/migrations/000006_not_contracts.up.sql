-- +goose Up
--
-- Shapes that look like contracts and are not (SCHEMA-0007). In a file with no
-- `-- expand-contract:` marker, so each exemption is exercised on its own rather than
-- excused by a file-level marker.

-- 1. A constraint over a column this same migration adds: no build before it wrote
--    that column, so the constraint refuses nothing it writes.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS priority INTEGER;
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_priority CHECK (priority BETWEEN 0 AND 9) NOT VALID;

-- 2. A function rebuilt under its own name in one transaction: it never goes away.
DROP FUNCTION IF EXISTS fixture_lookup(uuid);
CREATE FUNCTION fixture_lookup(id uuid) RETURNS uuid LANGUAGE sql AS 'SELECT id';

-- 3. A replace the parser cannot compare (not one column IN a list), declared wider by
--    its author where the reviewer reads it.
ALTER TABLE fixture_threads DROP CONSTRAINT IF EXISTS chk_fixture_threads_body_present;
-- schema:allow reason=nothing-refused adr=ADR-0062
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_body_present CHECK (body IS NOT NULL OR archived) NOT VALID;
