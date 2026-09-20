-- +goose Up
--
-- Shapes that LOOK like violations and are not. Each one was a false positive this
-- gate actually produced, found by running the CI command against scratch repositories
-- rather than by reading the code, and each is pinned here so the exemption that fixes
-- it cannot be removed without the self-test going red.
--
-- In a file of its own, with no `-- expand-contract:` marker: that marker is
-- file-level, so beside a marked statement these would be excused for the wrong reason
-- and the exemptions below would never be exercised.

-- 1. DEFAULT written BEFORE NOT NULL. This is the canonical safe column addition and
--    the more common of the two orderings; the gate accepted `NOT NULL DEFAULT false`
--    and refused this one, because the lookahead that searched for DEFAULT only saw
--    text after the words NOT NULL.
ALTER TABLE fixture_threads ADD COLUMN archived boolean DEFAULT false NOT NULL;

-- 2. DDL keywords inside a string LITERAL. The words are data, not statements: a seed
--    row and a column comment, neither of which drops anything.
INSERT INTO fixture_change_log (id, note)
    VALUES (1, 'drop column legacy_note in the contract release');
COMMENT ON COLUMN fixture_threads.body IS 'we will drop column legacy soon';

-- 3. A scratch table created, used and dropped inside one migration. Nothing outside
--    the migration ever saw it, so dropping it loses nothing and needs no expand
--    release. DROP TABLE carries no ALTER TABLE to read the name from, which is why
--    the same-migration check had to learn to read it from the DROP itself.
CREATE TABLE fixture_tmp_backfill (id uuid PRIMARY KEY, v text);
UPDATE fixture_threads SET body = 'x' WHERE id IS NOT NULL;
DROP TABLE fixture_tmp_backfill;

-- 4. A marker written as a TRAILING comment. Requiring it strictly above refused the
--    most natural placement for a column.
ALTER TABLE fixture_threads
    ADD COLUMN note_kind TEXT; -- schema:allow reason=free-form adr=ADR-0062

-- 5. A marker above the full goose preamble, which is FOUR lines and not three - the
--    marker, `-- +goose StatementBegin`, `DO $$`, `BEGIN`, then the statement. The
--    reach constant was documented as existing for exactly this idiom and was one
--    short of it, so a marker written where its own comment said to write it was
--    refused as an orphan.
--
--    The claim the marker makes is the documented `empty-table` case the parser
--    cannot see for itself: the table is created by a function this migration calls,
--    so there are no rows for the constraint to validate, and no CREATE TABLE
--    statement for created_here to find.
SELECT fixture_create_audit_table();

-- schema:allow reason=empty-table adr=ADR-0062
-- +goose StatementBegin
DO $$
BEGIN
    ALTER TABLE fixture_audit
        ADD CONSTRAINT chk_fixture_audit_actor CHECK (actor_id IS NOT NULL);
END $$;
-- +goose StatementEnd
