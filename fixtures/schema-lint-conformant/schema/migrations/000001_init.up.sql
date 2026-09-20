-- +goose Up
--
-- The conformant fixture. Every shape schema-lint judges appears here in the form it
-- accepts, so a change that starts reporting findings on this file has introduced a
-- false positive, and the self-test fails on the count being non-zero rather than on
-- any particular line.

-- An enum type at top level, where sqlc can see it (SCHEMA-0003).
CREATE TYPE fixture_attachment_type AS ENUM ('image', 'video', 'file');

CREATE TABLE IF NOT EXISTS fixture_threads (
    id            UUID PRIMARY KEY,
    org_id        UUID NOT NULL,
    -- Enumerated shape WITH a value constraint (SCHEMA-0004).
    channel_type  TEXT NOT NULL CHECK (channel_type IN ('chat', 'voice', 'sms')),
    -- Enumerated shape, constrained at table level further down.
    kind          TEXT NOT NULL DEFAULT 'conversation',
    -- Enumerated shape whose set is genuinely open, declared as such.
    -- schema:allow reason=provider-vocabulary adr=ADR-0062  (Twilio call status)
    provider_status TEXT,
    -- MIME, excluded by name rather than by suffix.
    content_type  TEXT,
    body          TEXT,
    CONSTRAINT chk_fixture_threads_kind CHECK (kind IN ('conversation', 'official'))
);

-- A constraint on a table created in THIS migration needs no NOT VALID: there are no
-- rows to validate (SCHEMA-0002 exemption).
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_org_not_zero
    CHECK (org_id <> '00000000-0000-0000-0000-000000000000');
