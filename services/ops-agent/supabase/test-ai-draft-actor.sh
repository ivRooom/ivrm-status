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
# Either check may stop it first: the validator no longer matches the original, or the guard line is gone.
grep -Eq "has changed since this migration was written|guard line expected once" /tmp/second-run.err

echo "== assertions"
run <"$here/test-assertions.sql"

echo "== drift: if production changed the admin validator, the migration must refuse"
docker exec "$name" createdb -U postgres drift
run_drift() { docker exec -i "$name" psql -U postgres -d drift -v ON_ERROR_STOP=1 -q "$@"; }
run_drift <"$here/test-fixture.sql"
# Someone added a role to the validator after this migration was written.
run_drift -c "create or replace function public.status_actor_valid_v1(p_actor_email text, p_actor_role text, p_actor_discord_user_id text) returns boolean language sql immutable set search_path to '' as \$f\$ select p_actor_role in ('administrator', 'owner', 'moderator') and (p_actor_email is not null or p_actor_discord_user_id is not null); \$f\$;"
if run_drift <"$here/ai-draft-actor.sql" 2>/tmp/drift.err; then
  echo "migration unexpectedly overwrote a changed validator" >&2
  exit 1
fi
grep -q "has changed since this migration was written" /tmp/drift.err
# Nothing was applied: the failed migration rolled back completely.
run_drift -c "do \$\$ begin if to_regprocedure('public.status_ai_draft_actor_valid_v1(text,text,text)') is not null then raise exception 'partial apply'; end if; end \$\$;"
