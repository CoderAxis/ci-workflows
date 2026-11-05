-- +goose Up
--
-- A constraint replaced by a NARROWER one ('gold' is refused now), with no marker. The
-- drop is the replace idiom SCHEMA-0001 exempts; the widening check must not exempt it.
ALTER TABLE broken_windows DROP CONSTRAINT IF EXISTS chk_broken_windows_tier;
-- SCHEMA-0007 at line 8.
ALTER TABLE broken_windows
    ADD CONSTRAINT chk_broken_windows_tier CHECK (tier IN ('bronze', 'silver')) NOT VALID;
