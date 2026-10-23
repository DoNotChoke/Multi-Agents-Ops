-- Creates or updates the cluster roles; run with psql as a superuser against the opsteam database.
-- psql variables: app_password, migrator_password, system_password.
\set ON_ERROR_STOP on

select format('create role %I', r)
from unnest(array['opsteam_app', 'opsteam_migrator', 'opsteam_system']) as r
where not exists (select 1 from pg_roles where rolname = r)
\gexec

-- None of them may bypass RLS; the migrator creates databases only for throwaway test runs.
alter role opsteam_app login nosuperuser nobypassrls nocreatedb nocreaterole password :'app_password';
alter role opsteam_migrator login nosuperuser nobypassrls createdb nocreaterole password :'migrator_password';
alter role opsteam_system login nosuperuser nobypassrls nocreatedb nocreaterole password :'system_password';

alter database opsteam owner to opsteam_migrator;
