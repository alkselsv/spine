#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${SPINE_MIGRATION_ROLE:?SPINE_MIGRATION_ROLE is required}"
: "${SPINE_MIGRATION_PASSWORD:?SPINE_MIGRATION_PASSWORD is required}"
: "${SPINE_RUNTIME_ROLE:?SPINE_RUNTIME_ROLE is required}"
: "${SPINE_RUNTIME_PASSWORD:?SPINE_RUNTIME_PASSWORD is required}"
: "${SPINE_DISPATCHER_ROLE:?SPINE_DISPATCHER_ROLE is required}"
: "${SPINE_DISPATCHER_PASSWORD:?SPINE_DISPATCHER_PASSWORD is required}"
: "${SPINE_BACKUP_ROLE:?SPINE_BACKUP_ROLE is required}"
: "${SPINE_BACKUP_PASSWORD:?SPINE_BACKUP_PASSWORD is required}"
: "${SPINE_RESTORE_ROLE:?SPINE_RESTORE_ROLE is required}"
: "${SPINE_RESTORE_PASSWORD:?SPINE_RESTORE_PASSWORD is required}"
: "${SPINE_VERIFIER_ROLE:?SPINE_VERIFIER_ROLE is required}"
: "${SPINE_VERIFIER_PASSWORD:?SPINE_VERIFIER_PASSWORD is required}"
: "${SPINE_TEMPORAL_ROLE:?SPINE_TEMPORAL_ROLE is required}"
: "${SPINE_TEMPORAL_PASSWORD:?SPINE_TEMPORAL_PASSWORD is required}"
: "${SPINE_TEMPORAL_DATABASE:?SPINE_TEMPORAL_DATABASE is required}"
: "${SPINE_TEMPORAL_VISIBILITY_DATABASE:?SPINE_TEMPORAL_VISIBILITY_DATABASE is required}"

psql \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 \
  --set=spine_database="$POSTGRES_DB" \
  --set=migration_role="$SPINE_MIGRATION_ROLE" \
  --set=migration_password="$SPINE_MIGRATION_PASSWORD" \
  --set=runtime_role="$SPINE_RUNTIME_ROLE" \
  --set=runtime_password="$SPINE_RUNTIME_PASSWORD" \
  --set=dispatcher_role="$SPINE_DISPATCHER_ROLE" \
  --set=dispatcher_password="$SPINE_DISPATCHER_PASSWORD" \
  --set=backup_role="$SPINE_BACKUP_ROLE" \
  --set=backup_password="$SPINE_BACKUP_PASSWORD" \
  --set=restore_role="$SPINE_RESTORE_ROLE" \
  --set=restore_password="$SPINE_RESTORE_PASSWORD" \
  --set=verifier_role="$SPINE_VERIFIER_ROLE" \
  --set=verifier_password="$SPINE_VERIFIER_PASSWORD" \
  --set=temporal_role="$SPINE_TEMPORAL_ROLE" \
  --set=temporal_password="$SPINE_TEMPORAL_PASSWORD" \
  --set=temporal_database="$SPINE_TEMPORAL_DATABASE" \
  --set=temporal_visibility_database="$SPINE_TEMPORAL_VISIBILITY_DATABASE" \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;

SELECT format('CREATE ROLE %I LOGIN', :'migration_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migration_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'runtime_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'dispatcher_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'dispatcher_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'backup_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'backup_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'restore_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'restore_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'verifier_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'verifier_role')\gexec
SELECT format('CREATE ROLE %I LOGIN', :'temporal_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'temporal_role')\gexec

ALTER ROLE :"migration_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'migration_password';
ALTER ROLE :"runtime_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'runtime_password';
ALTER ROLE :"dispatcher_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'dispatcher_password';
ALTER ROLE :"backup_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'backup_password';
ALTER ROLE :"restore_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'restore_password';
ALTER ROLE :"verifier_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'verifier_password';
ALTER ROLE :"temporal_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'temporal_password';

ALTER DATABASE :"spine_database" OWNER TO :"migration_role";
REVOKE ALL ON DATABASE :"spine_database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"spine_database" TO :"migration_role", :"runtime_role", :"dispatcher_role", :"backup_role", :"restore_role", :"verifier_role";

SELECT format('CREATE DATABASE %I OWNER %I', :'temporal_database', :'temporal_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'temporal_database')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'temporal_visibility_database', :'temporal_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'temporal_visibility_database')\gexec
SQL
