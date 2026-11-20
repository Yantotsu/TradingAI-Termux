-- Restricted support role: may inspect component health only, never user data or balances.
create table if not exists public.monitoring_admins (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

create or replace function public.prevent_admin_role_overlap()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_table_name = 'monitoring_admins' then
    if exists (select 1 from public.account_admins a where a.user_id = new.user_id) then
      raise exception 'monitoring_role_cannot_be_full_admin' using errcode = '23514';
    end if;
  elsif exists (select 1 from public.monitoring_admins m where m.user_id = new.user_id) then
    raise exception 'full_admin_cannot_be_monitoring_only' using errcode = '23514';
  end if;
  return new;
end;
$$;
revoke all on function public.prevent_admin_role_overlap() from public, anon, authenticated;

create trigger prevent_monitor_role_overlap
before insert or update of user_id on public.monitoring_admins
for each row execute function public.prevent_admin_role_overlap();

create trigger prevent_full_admin_role_overlap
before insert or update of user_id on public.account_admins
for each row execute function public.prevent_admin_role_overlap();

create table if not exists public.service_monitoring (
  component text primary key check (component in ('site', 'auth', 'database', 'market_feed', 'paper_engine')),
  status text not null check (status in ('healthy', 'degraded', 'offline', 'not_configured')),
  checked_at timestamptz not null default now(),
  latency_ms integer check (latency_ms is null or latency_ms between 0 and 60000),
  build_id text check (build_id is null or (length(build_id) <= 40 and build_id ~ '^[A-Za-z0-9._-]+$'))
);

alter table public.monitoring_admins enable row level security;
alter table public.service_monitoring enable row level security;
revoke all on table public.monitoring_admins, public.service_monitoring from anon, authenticated;
grant select on table public.service_monitoring to authenticated;
grant all on table public.monitoring_admins, public.service_monitoring to service_role;

create or replace function public.is_current_user_monitoring_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select auth.uid() is not null and exists (
    select 1
      from public.monitoring_admins m
      join public.account_profiles p on p.user_id = m.user_id
     where m.user_id = auth.uid() and p.access_status = 'active'
  );
$$;
revoke all on function public.is_current_user_monitoring_admin() from public, anon;
grant execute on function public.is_current_user_monitoring_admin() to authenticated;

create policy "monitoring admins can read service health"
  on public.service_monitoring as permissive for select to authenticated
  using (public.is_current_user_monitoring_admin());

-- Restrictive policy prevents a future permissive policy from exposing rows at AAL1.
create policy "service health requires aal2"
  on public.service_monitoring as restrictive for select to authenticated
  using (coalesce((select auth.jwt() ->> 'aal'), 'aal1') = 'aal2');

comment on table public.monitoring_admins is
  'Restricted system-health role; do not add monitoring accounts to account_admins.';
comment on table public.service_monitoring is
  'Component status only. Never store user IDs, balances, orders, tokens, or free-form logs here.';
