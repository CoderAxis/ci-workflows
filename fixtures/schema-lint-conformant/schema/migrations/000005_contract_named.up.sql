-- +goose Up
--
-- A contract migration as SCHEMA-0007 accepts it: the ticket, and on the same line the
-- release since which no build uses what this removes. A build one migration behind
-- the schema is that release or later, so nothing it calls or writes is gone.
-- expand-contract: PLAT-4480 unused-since: fixture-core v0.10.0
DROP FUNCTION IF EXISTS fixture_old_lookup(uuid);
ALTER TYPE fixture_attachment_type RENAME VALUE 'file' TO 'document';
