-- +goose Up
--
-- Contracts with a ticket and no release named: SCHEMA-0001 is satisfied, SCHEMA-0007
-- is not. Each removes or narrows something a build one migration behind may still use.
-- expand-contract: PLAT-4490

-- SCHEMA-0007 at line 8: a function the release before may still call.
DROP FUNCTION IF EXISTS broken_lookup(uuid);

-- SCHEMA-0007 at line 11: an enum value renamed under a build that writes the old one.
ALTER TYPE broken_status RENAME VALUE 'a' TO 'alpha';

-- SCHEMA-0007 at line 15: a CHECK over a column the table already had.
ALTER TABLE broken_windows
    ADD CONSTRAINT chk_broken_windows_tier CHECK (tier IN ('bronze', 'silver', 'gold')) NOT VALID;
