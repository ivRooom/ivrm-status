#!/bin/sh
# Start the resource server (mc-resource). OCI host.
# Equivalent to: cd /opt/ivrm/compose/minecraft-resource && docker compose up -d
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

dir=${IVRM_MC_RESOURCE_DIR:-/opt/ivrm/compose/minecraft-resource}
file=$(compose_file "$dir") || fail "no compose file in $dir"

docker compose --project-directory "$dir" -f "$file" up -d
wait_until "${IVRM_WAIT_SECONDS:-180}" 5 "mc-resource running" container_ready mc-resource
docker inspect mc-resource --format 'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
