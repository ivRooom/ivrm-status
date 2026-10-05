#!/usr/bin/env bash
# Verifies ai-draft-actor.sql against a throwaway local Postgres (needs Docker).
# It does not touch any real database.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="ivrm-ai-actor-test-$$"
image="${POSTGRES_IMAGE:-postgres:17}"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" -e POSTGRES_PASSWORD=test "$image" >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$name" pg_isready -U postgres >/dev/null

run() { docker exec -i "$name" psql -U postgres -v ON_ERROR_STOP=1 -q "$@"; }

echo "== load fixture"
run <"$here/test-fixture.sql"

echo "== baseline: before the migration the AI actor is rejected everywhere"
run -c "do \$\$ begin perform public.create_status_announcement_v1('ops-agent@ivrm.invalid','ai_agent',null); raise exception 'unexpectedly allowed'; exception when sqlstate '42501' then null; end \$\$;"

echo "== apply migration"
run <"$here/ai-draft-actor.sql"

echo "== idempotency: the second run must abort instead of double-patching"
if run <"$here/ai-draft-actor.sql" 2>/tmp/second-run.err; then
  echo "second run unexpectedly succeeded" >&2
  exit 1
fi
grep -q "guard line expected once" /tmp/second-run.err

echo "== assertions"
run <"$here/test-assertions.sql"
