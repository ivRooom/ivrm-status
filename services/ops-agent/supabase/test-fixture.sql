-- Test fixture: the real status_actor_valid_v1 plus stubs whose guard line is
-- byte-for-byte what the production create_* / publish_* / append_* functions use.
-- The stubs only model the actor check; they are not the real RPC bodies.

-- Roles that exist on Supabase but not on a plain Postgres.
create role anon nologin;
create role authenticated nologin;
create role service_role nologin;

create or replace function public.status_actor_valid_v1(p_actor_email text, p_actor_role text, p_actor_discord_user_id text)
 returns boolean
 language sql
 immutable
 set search_path to ''
as $function$
  select
    p_actor_role in ('administrator', 'owner')
    and (
      p_actor_email is null
      or (
        char_length(p_actor_email) between 3 and 320
        and p_actor_email = lower(btrim(p_actor_email))
      )
    )
    and (
      p_actor_discord_user_id is null
      or (
        char_length(p_actor_discord_user_id) between 17 and 20
        and p_actor_discord_user_id ~ '^[0-9]+$'
      )
    )
    and (p_actor_email is not null or p_actor_discord_user_id is not null);
$function$;

do $fixture$
declare
  name text;
begin
  foreach name in array array[
    'create_status_announcement_v1',
    'create_status_incident_v1',
    'create_status_maintenance_v1',
    'publish_status_announcement_v1',
    'publish_status_incident_v1',
    'publish_status_maintenance_v1',
    'append_status_incident_update_v1',
    'cancel_status_maintenance_v1',
    'archive_status_announcement_v1'
  ] loop
    execute format($f$
      create or replace function public.%I(p_actor_email text, p_actor_role text, p_actor_discord_user_id text)
       returns text
       language plpgsql
       security definer
       set search_path to 'public', 'extensions'
      as $body$
      begin
        if not public.status_actor_valid_v1(p_actor_email, p_actor_role, p_actor_discord_user_id) then
          raise exception 'actor_forbidden' using errcode = '42501';
        end if;
        return %L;
      end;
      $body$;
    $f$, name, name);
    execute format('revoke all on function public.%I(text, text, text) from public', name);
    execute format('grant execute on function public.%I(text, text, text) to service_role', name);
  end loop;
end
$fixture$;
