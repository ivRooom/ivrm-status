#!/bin/sh
# Start the resource server (mc-resource). OCI host.
# Equivalent to: cd /opt/ivrm/compose/minecraft-resource && docker compose up -d
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

dir=${IVRM_MC_RESOURCE_DIR:-/opt/ivrm/compose/minecraft-resource}
[ -f "$dir/compose.yml" ] || fail "compose file not found: $dir/compose.yml"

docker compose --project-directory "$dir" -f "$dir/compose.yml" up -d
wait_until "${IVRM_WAIT_SECONDS:-180}" 5 "mc-resource running" container_ready mc-resource
docker inspect mc-resource --format 'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
