-- +goose Up
--
-- The constraint-replace idiom, in a file of its own ON PURPOSE.
--
-- `-- expand-contract:` is a FILE-level marker, matching the promotion gate's own
-- semantics, so putting this beside a marked statement would let that marker excuse it
-- and the exemption below would never be exercised. Mutation-testing the self-test is
-- what caught that: dropping the exemption did not fail while this lived in 000002.
--
-- A CHECK cannot be extended in place, so dropping and re-adding it under the SAME
-- name in one migration keeps the table constrained for the whole transaction and
-- loses nothing. SCHEMA-0001 exempts exactly that pairing, by constraint name.
ALTER TABLE fixture_threads DROP CONSTRAINT IF EXISTS chk_fixture_threads_kind;
ALTER TABLE fixture_threads
    ADD CONSTRAINT chk_fixture_threads_kind
    CHECK (kind IN ('conversation', 'official', 'group')) NOT VALID;
