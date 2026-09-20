-- +goose Up
--
-- Forward migration against the populated table from the baseline.

-- SCHEMA-0004 at line 7: enumerated shape, no CHECK, no enum type.
ALTER TABLE broken_threads
    ADD COLUMN IF NOT EXISTS channel_type TEXT;

-- SCHEMA-0002 at line 11: ADD CONSTRAINT with no NOT VALID on a populated table.
ALTER TABLE broken_threads
    ADD CONSTRAINT chk_broken_threads_body
    CHECK (body IS NOT NULL);

-- SCHEMA-0001 at line 15: a column dropped with no expand release named.
ALTER TABLE broken_threads DROP COLUMN IF EXISTS body;

-- SCHEMA-0005 at line 18: a marker citing a reason the catalog does not sanction.
-- schema:allow reason=because-i-said-so adr=ADR-0062
ALTER TABLE broken_threads ADD COLUMN IF NOT EXISTS note TEXT;
