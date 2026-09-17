#!/usr/bin/env bash
# Run the full suite *including* the server-backed conformance tests.
#
# Brings up the Postgres and MySQL services from docker/docker-compose.yml,
# waits for them to report healthy, exports the DSNs the fixtures look for, and
# runs pytest with the integration marker enabled.
#
# Without this (or a Docker daemon for testcontainers) the Postgres and MySQL
# conformance tests skip, which is how a whole tier of coverage used to go
# missing in CI.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

COMPOSE_FILE="docker/docker-compose.yml"
KEEP_UP="${REGISTRO_KEEP_CONTAINERS:-0}"

cleanup() {
  if [[ "$KEEP_UP" != "1" ]]; then
    docker compose -f "$COMPOSE_FILE" down -v >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "==> starting postgres + mysql"
docker compose -f "$COMPOSE_FILE" up -d postgres mysql

echo "==> waiting for health checks"
for _ in $(seq 1 60); do
  unhealthy="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Health}}' \
    | grep -cv '^healthy$' || true)"
  [[ "$unhealthy" == "0" ]] && break
  sleep 2
done

export REGISTRO_PG_DSN="${REGISTRO_PG_DSN:-postgres://registro:registro@localhost:5432/registro}"
export REGISTRO_MYSQL_DSN="${REGISTRO_MYSQL_DSN:-mysql://registro:registro@127.0.0.1:3306/registro}"
export REGISTRO_TEST_MARKERS="integration or not integration"

echo "==> pytest (integration enabled)"
exec "$HERE/tests/scripts/test.sh" "$@"
