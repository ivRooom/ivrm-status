-- Assertions run after ai-draft-actor.sql has been applied to the fixture.
\set ON_ERROR_STOP on

create or replace function pg_temp.expect_forbidden(fn text, email text, role text) returns void
language plpgsql as $$
declare outcome text;
begin
  begin
    execute format('select public.%I(%L, %L, null)', fn, email, role);
    outcome := 'allowed';
  exception when sqlstate '42501' then
    outcome := 'forbidden';
  end;
  if outcome <> 'forbidden' then
    raise exception 'expected % to reject (% / %) but it was allowed', fn, email, role;
  end if;
end $$;

create or replace function pg_temp.expect_allowed(fn text, email text, role text) returns void
language plpgsql as $$
begin
  execute format('select public.%I(%L, %L, null)', fn, email, role);
end $$;

do $$
declare fn text;
begin
  -- 1. The AI actor can create drafts of all three kinds.
  foreach fn in array array['create_status_announcement_v1','create_status_incident_v1','create_status_maintenance_v1'] loop
    perform pg_temp.expect_allowed(fn, 'ops-agent@ivrm.invalid', 'ai_agent');
  end loop;

  -- 2. The AI actor can NOT do anything else.
  foreach fn in array array[
    'publish_status_announcement_v1','publish_status_incident_v1','publish_status_maintenance_v1',
    'append_status_incident_update_v1','cancel_status_maintenance_v1','archive_status_announcement_v1'
  ] loop
    perform pg_temp.expect_forbidden(fn, 'ops-agent@ivrm.invalid', 'ai_agent');
  end loop;

  -- 3. Administrators keep working everywhere (no regression).
  foreach fn in array array[
    'create_status_announcement_v1','create_status_incident_v1','create_status_maintenance_v1',
    'publish_status_announcement_v1','append_status_incident_update_v1','archive_status_announcement_v1'
  ] loop
    perform pg_temp.expect_allowed(fn, 'admin@example.com', 'administrator');
  end loop;

  -- 4. The AI identity is fixed: another email or a Discord id is rejected, even on create.
  perform pg_temp.expect_forbidden('create_status_announcement_v1', 'someone-else@ivrm.invalid', 'ai_agent');
  perform pg_temp.expect_forbidden('create_status_announcement_v1', 'ops-agent@ivrm.invalid', 'viewer');
  perform pg_temp.expect_forbidden('create_status_incident_v1', 'admin@example.com', 'ai_agent');
  begin
    perform public.create_status_announcement_v1('ops-agent@ivrm.invalid', 'ai_agent', '12345678901234567');
    raise exception 'a discord id must not be accepted for the AI actor';
  exception when sqlstate '42501' then null;
  end;

  -- 5. The admin validator itself still refuses the AI role.
  if public.status_actor_valid_v1('ops-agent@ivrm.invalid', 'ai_agent', null) then
    raise exception 'status_actor_valid_v1 must keep rejecting ai_agent';
  end if;

  -- 6. The new validator is not callable by anon / authenticated.
  if has_function_privilege('public', 'public.status_ai_draft_actor_valid_v1(text,text,text)', 'execute') then
    raise exception 'PUBLIC must not execute the AI actor validator';
  end if;
end $$;

select 'ok: AI actor can only create drafts' as result;
