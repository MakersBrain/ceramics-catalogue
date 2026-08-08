-- Runs before the catalogue schema files.
--
-- The loader functions are declared `set search_path = pg_catalog, catalogue`,
-- so pgcrypto's digest() only resolves if the extension lives in `catalogue`
-- (or another schema on that path) — installing it into `public` breaks the
-- import with "function digest(bytea, unknown) does not exist".

create schema if not exists catalogue;

create extension if not exists pgcrypto with schema catalogue;
