-- PROPOSED migration for Supabase `ivrm-core` (NOT applied).
--
-- Lets a dedicated AI actor create status DRAFTS (announcement / incident /
-- maintenance) and nothing else.
--
-- Background: status_actor_valid_v1 is shared by all nine status RPCs
-- (create / publish / append / cancel / archive) and only accepts the roles
-- administrator / owner. Adding 'ai_agent' to it would also let the AI publish.
-- So instead:
--   1. add a separate validator that accepts exactly one fixed AI identity, and
--   2. let ONLY the three create_* functions accept it, by patching their single
--      guard line. publish_* / append_* / cancel_* / archive_* are not touched and
--      keep rejecting 'ai_agent'.
--
-- The patch is applied by rewriting each function's current definition and aborts
-- unless the guard line occurs exactly once, so it cannot silently drift.
--
-- Note: every status RPC is executable only by service_role, so the actor is a plain
-- argument. The real boundary is the server that calls them (api.ivrm.jp): the AI
-- credential must only reach an endpoint that hard-codes the actor below and calls
-- the create_* functions. This migration is defense in depth for that boundary.

begin;

create or replace function public.status_ai_draft_actor_valid_v1(
  p_actor_email text,
  p_actor_role text,
  p_actor_discord_user_id text
) returns boolean
language sql
immutable
set search_path = ''
as $$
  select p_actor_role = 'ai_agent'
    and p_actor_email = 'ops-agent@ivrm.invalid'  -- reserved TLD: a marker, not a mailbox
    and p_actor_discord_user_id is null;
$$;

revoke all on function public.status_ai_draft_actor_valid_v1(text, text, text) from public, anon, authenticated;

do $migration$
declare
  fn text;
  def text;
  guard constant text :=
    'if not public.status_actor_valid_v1(p_actor_email, p_actor_role, p_actor_discord_user_id) then';
  replacement constant text :=
    'if not (public.status_actor_valid_v1(p_actor_email, p_actor_role, p_actor_discord_user_id) '
    || 'or public.status_ai_draft_actor_valid_v1(p_actor_email, p_actor_role, p_actor_discord_user_id)) then';
  matches int;
  occurrences int;
begin
  foreach fn in array array[
    'create_status_announcement_v1',
    'create_status_incident_v1',
    'create_status_maintenance_v1'
  ] loop
    select count(*) into matches
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = fn;
    if matches <> 1 then
      raise exception '% must have exactly one definition, found %', fn, matches;
    end if;

    select pg_get_functiondef(p.oid) into def
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = fn;

    occurrences := (length(def) - length(replace(def, guard, ''))) / length(guard);
    if occurrences <> 1 then
      raise exception '% guard line expected once, found % (definition changed?)', fn, occurrences;
    end if;

    execute replace(def, guard, replacement);
  end loop;
end
$migration$;

commit;

-- ---------------------------------------------------------------------------
-- Rollback (restores the original guard and removes the validator):
--
--   begin;
--   do $rollback$
--   declare fn text; def text;
--     guard constant text := '...same text as `guard` above...';
--     replacement constant text := '...same text as `replacement` above...';
--   begin
--     foreach fn in array array['create_status_announcement_v1','create_status_incident_v1','create_status_maintenance_v1'] loop
--       select pg_get_functiondef(p.oid) into def from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--         where n.nspname = 'public' and p.proname = fn;
--       execute replace(def, replacement, guard);
--     end loop;
--   end $rollback$;
--   drop function public.status_ai_draft_actor_valid_v1(text, text, text);
--   commit;
--
-- Post-apply checks (run as service_role / postgres):
--   -- must be false: the admin validator still refuses the AI role
--   select public.status_actor_valid_v1('ops-agent@ivrm.invalid', 'ai_agent', null);
--   -- must be true
--   select public.status_ai_draft_actor_valid_v1('ops-agent@ivrm.invalid', 'ai_agent', null);
--   -- must raise 42501 (publish still rejects the AI actor)
--   select public.publish_status_announcement_v1('ANN-000000000000', gen_random_uuid(), 'ops-agent@ivrm.invalid', 'ai_agent', null);
