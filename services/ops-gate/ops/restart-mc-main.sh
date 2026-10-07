#!/bin/sh
# Restart the main server (mc-main). OCI host. Disconnects every player.
# Equivalent to: cd /opt/ivrm/compose/minecraft-main && docker compose restart
# (The restart is run once; the status check afterwards only reads.)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

dir=${IVRM_MC_MAIN_DIR:-/opt/ivrm/compose/minecraft-main}
[ -f "$dir/compose.yml" ] || [ -f "$dir/docker-compose.yml" ] || fail "no compose file in $dir"

docker compose --project-directory "$dir" restart
wait_until "${IVRM_WAIT_SECONDS:-300}" 5 "mc-main running" container_ready mc-main
docker inspect mc-main --format 'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
