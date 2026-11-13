#!/bin/sh
# One-shot: namespace `factory` (closed history kept 30 days) and the engine's search attributes (Keyword). The
# server creates neither by itself. Idempotent. Env (compose): TEMPORAL_ADDRESS.
set -eu
: "${TEMPORAL_ADDRESS:=temporal:7233}"
NS=factory

attempt=0
until temporal operator cluster health --address "${TEMPORAL_ADDRESS}" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "${attempt}" -lt 30 ] || { echo "temporal-setup: server never became healthy"; exit 1; }
  sleep 3
done
if ! temporal operator namespace describe --namespace "${NS}" --address "${TEMPORAL_ADDRESS}" >/dev/null 2>&1; then
  temporal operator namespace create --namespace "${NS}" --retention 720h \
    --description "software factory engine" --address "${TEMPORAL_ADDRESS}"
fi
# A namespace just created takes a moment to be visible to the search-attribute API.
attempt=0
until existing=$(temporal operator search-attribute list --namespace "${NS}" --address "${TEMPORAL_ADDRESS}" 2>/dev/null); do
  attempt=$((attempt + 1))
  [ "${attempt}" -lt 30 ] || { echo "temporal-setup: namespace ${NS} never became visible"; exit 1; }
  sleep 2
done
for name in FactoryRepo FactoryTeam; do
  case "${existing}" in
    *"${name}"*) ;;
    *) temporal operator search-attribute create --namespace "${NS}" --name "${name}" --type Keyword \
         --address "${TEMPORAL_ADDRESS}" ;;
  esac
done
echo "temporal-setup: done"
