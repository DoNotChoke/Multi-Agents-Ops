#!/bin/sh
# Runs once on an empty volume: creates the opsteam_app/migrator/system roles.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d opsteam \
  -v app_password="$OPSTEAM_APP_DB_PASSWORD" \
  -v migrator_password="$OPSTEAM_MIGRATOR_DB_PASSWORD" \
  -v system_password="$OPSTEAM_SYSTEM_DB_PASSWORD" \
  -f /opsteam-sql/roles.sql
