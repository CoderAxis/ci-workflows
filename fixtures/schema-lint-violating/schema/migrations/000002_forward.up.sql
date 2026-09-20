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

-- The two cases below must keep FIRING. Each is a finding a past bug silently
-- swallowed, so each pins a fix from the suppression side, which recall alone could
-- not: a rule that stops reporting reads exactly like a repository that got cleaner.

-- SCHEMA-0004 at line 31: an enumerated column declared close after a CHECK. When
-- CHECK bodies were captured as a fixed 800-character window, that window ran past the
-- end of the expression and over this column, so `delivery_state` read as constrained.
CREATE TABLE broken_windows (
    id       UUID PRIMARY KEY,
    tier     TEXT NOT NULL CHECK (tier IN ('bronze', 'silver', 'gold')),
    delivery_state TEXT NOT NULL
);

-- SCHEMA-0004 at line 39: an enumerated column four lines below an unrelated marker.
-- A column marker reaches ONE line, because column definitions sit one line apart and
-- a wider reach let a single marker quietly excuse its neighbours.
-- schema:allow reason=free-form adr=ADR-0062
ALTER TABLE broken_windows ADD COLUMN operator_note TEXT;
ALTER TABLE broken_windows ADD COLUMN audit_kind TEXT;
