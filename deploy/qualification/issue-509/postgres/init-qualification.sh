#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

psql \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL
