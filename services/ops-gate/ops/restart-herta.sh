#!/bin/sh
# Restart Herta (bot AND worker, which belong together). Lightsail host.
# Equivalent to:
#   cd /app/herta && docker compose --env-file .env.production -f docker-compose.prod.yml restart bot worker
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

dir=${IVRM_HERTA_DIR:-/app/herta}
health_url=${IVRM_HERTA_HEALTH_URL:-http://127.0.0.1:3000/healthz}
[ -f "$dir/docker-compose.prod.yml" ] || fail "compose file not found: $dir/docker-compose.prod.yml"
[ -f "$dir/.env.production" ] || fail "env file not found: $dir/.env.production"

# Absolute paths: the gate does not run from the application directory.
COMPOSE_ARGS="--project-directory $dir --env-file $dir/.env.production -f $dir/docker-compose.prod.yml"
# shellcheck disable=SC2086
docker compose $COMPOSE_ARGS restart bot worker

wait_until "${IVRM_WAIT_SECONDS:-120}" 5 "bot and worker running" compose_services_running bot worker
wait_until "${IVRM_WAIT_SECONDS:-120}" 3 "healthz responds" curl -fsS --max-time 5 "$health_url"
log "herta restarted and healthy"
