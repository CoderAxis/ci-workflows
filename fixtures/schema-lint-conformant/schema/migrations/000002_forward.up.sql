-- +goose Up
--
-- A forward migration against the table the baseline created. Everything here touches
-- a POPULATED table, so the safe forms are mandatory rather than exempt.

ALTER TABLE fixture_threads
    ADD COLUMN IF NOT EXISTS delivery_status TEXT
        CHECK (delivery_status IN ('queued', 'sent', 'failed'));

-- NOT VALID, then validated separately: the second statement takes SHARE UPDATE
-- EXCLUSIVE and does not block writes (SCHEMA-0002).
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_body_present
    CHECK (body IS NOT NULL) NOT VALID;

ALTER TABLE fixture_threads VALIDATE CONSTRAINT chk_fixture_threads_body_present;

-- The contract half of an expand/contract pair, declared with the SAME marker the
-- promotion gate reads (inboxxhq-infra/scripts/check-destructive-ddl.py).
-- The release since which no build reads the column is on the same line (SCHEMA-0007).
-- expand-contract: PLAT-4471 unused-since: fixture-core v0.9.0
ALTER TABLE fixture_threads DROP COLUMN IF EXISTS content_type;
