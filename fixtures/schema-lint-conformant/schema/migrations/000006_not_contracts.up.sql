-- +goose Up
--
-- Shapes that look like contracts and are not (SCHEMA-0007). In a file with no
-- `-- expand-contract:` marker, so each exemption is exercised on its own rather than
-- excused by a file-level marker.

-- 1. A constraint gated on a column this same migration adds, over columns the table
--    had: an older build leaves the new column NULL, so the constraint refuses
--    nothing it writes. NOT VALID too, which on its own is not a contract either.
ALTER TABLE fixture_threads ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_closed_has_body CHECK (closed_at IS NULL OR body IS NOT NULL) NOT VALID;

-- 2. A function rebuilt under its own name in one transaction: it never goes away.
DROP FUNCTION IF EXISTS fixture_lookup(uuid);
CREATE FUNCTION fixture_lookup(id uuid) RETURNS uuid LANGUAGE sql AS 'SELECT id';

-- 3. A UNIQUE over a set that holds the primary key: it can refuse nothing.
ALTER TABLE fixture_threads ADD CONSTRAINT uq_fixture_threads_org_id_id UNIQUE (org_id, id);

-- 4. A UNIQUE over existing columns the parser cannot judge, declared by its author,
--    where the reviewer reads it, to refuse nothing an older build writes.
-- schema:allow reason=nothing-refused adr=ADR-0062
ALTER TABLE fixture_threads ADD CONSTRAINT uq_fixture_threads_org_channel_kind UNIQUE (org_id, channel_type, kind, id);
