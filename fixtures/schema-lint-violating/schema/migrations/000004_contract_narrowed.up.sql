-- +goose Up
--
-- A constraint replaced by a NARROWER one ('gold' is refused now), validated, with no
-- marker. The drop is the replace idiom SCHEMA-0001 exempts; the widening check must
-- not exempt the re-add.
ALTER TABLE broken_windows DROP CONSTRAINT IF EXISTS chk_broken_windows_tier;
-- SCHEMA-0007 and SCHEMA-0002 at line 9.
ALTER TABLE broken_windows
    ADD CONSTRAINT chk_broken_windows_tier CHECK (tier IN ('bronze', 'silver'));

-- SCHEMA-0001 at line 13 and NOT SCHEMA-0007: a dropped CHECK only accepts more, so no
-- build one migration behind can trip on it.
ALTER TABLE broken_threads DROP CONSTRAINT chk_broken_threads_body;
