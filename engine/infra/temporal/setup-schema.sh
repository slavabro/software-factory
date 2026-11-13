#!/bin/sh
# One-shot: create Temporal's two Postgres databases and bring their schema to this admin-tools image's version.
# Idempotent: `create` is a no-op on an existing database, `setup-schema -v 0.0` only seeds the version table and
# `update-schema` applies the migrations newer than the database. Env (compose): POSTGRES_SEEDS, POSTGRES_USER,
# POSTGRES_PWD, DB_PORT.
set -eu
: "${POSTGRES_SEEDS:?}" "${POSTGRES_USER:?}" "${POSTGRES_PWD:?}" "${DB_PORT:=5432}"
SCHEMA_ROOT=/etc/temporal/schema/postgresql/v12
export SQL_PASSWORD="${POSTGRES_PWD}"

sql_tool() {
  temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" -p "${DB_PORT}" "$@"
}

attempt=0
until nc -z -w 5 "${POSTGRES_SEEDS}" "${DB_PORT}"; do
  attempt=$((attempt + 1))
  [ "${attempt}" -lt 30 ] || { echo "temporal-schema: Postgres never became reachable"; exit 1; }
  sleep 2
done
for pair in "temporal temporal" "temporal_visibility visibility"; do
  set -- ${pair}
  echo "temporal-schema: ensuring database '$1'"
  sql_tool --db "$1" create
  sql_tool --db "$1" setup-schema -v 0.0
  sql_tool --db "$1" update-schema -d "${SCHEMA_ROOT}/$2/versioned"
done
echo "temporal-schema: done"
