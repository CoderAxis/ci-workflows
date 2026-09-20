-- +goose Up
--
-- The violating fixture. Every finding schema-lint can produce appears here exactly
-- once, and the self-test pins each one by control id AND line number, so a rule that
-- silently stops matching cannot read as a pass.

CREATE TABLE IF NOT EXISTS broken_threads (
    id           UUID PRIMARY KEY,
    org_id       UUID NOT NULL,
    body         TEXT
);

-- +goose StatementBegin
DO $$
BEGIN
    -- SCHEMA-0003 at line 18: sqlc cannot see a CREATE TYPE through a DO block and
    -- generates interface{}, which fails at runtime in pgx.
    CREATE TYPE broken_status AS ENUM ('a', 'b');
END $$;
-- +goose StatementEnd
