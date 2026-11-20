-- Private account foundation. This ledger is explicitly simulation-only.
-- Real deposits, custody, withdrawals, and live trading are intentionally absent.
create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.account_profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  access_status text not null default 'active'
    check (access_status in ('active', 'blocked')),
  display_name text check (display_name is null or length(display_name) <= 80),
  created_at timestamptz not null default now()
);

create table if not exists public.account_admins (
  user_id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

create table if not exists public.access_invites (
  id uuid primary key default gen_random_uuid(),
  code_hash bytea not null unique,
  created_by uuid not null references auth.users(id),
  invite_email text not null check (length(invite_email) <= 254 and invite_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  claimed_by uuid unique references auth.users(id),
  claimed_at timestamptz,
  disabled_at timestamptz,
  check (expires_at > created_at),
  check ((claimed_by is null) = (claimed_at is null))
);

create table if not exists public.demo_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id),
  currency_code text not null default 'SIM_IDR' check (currency_code = 'SIM_IDR'),
  amount_idr bigint not null check (amount_idr <> 0),
  entry_type text not null check (entry_type in ('admin_demo_credit', 'admin_demo_debit', 'paper_trade')),
  memo text not null check (length(memo) between 1 and 240),
  idempotency_key text not null unique check (length(idempotency_key) between 8 and 120),
  created_by uuid not null references auth.users(id),
  created_at timestamptz not null default now()
);
create index if not exists demo_ledger_user_created_idx
  on public.demo_ledger(user_id, created_at desc);

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'demo_ledger'
  ) then
    alter publication supabase_realtime add table public.demo_ledger;
  end if;
end;
$$;

alter table public.account_profiles enable row level security;
alter table public.account_admins enable row level security;
alter table public.access_invites enable row level security;
alter table public.demo_ledger enable row level security;

revoke all on table public.account_profiles, public.account_admins,
  public.access_invites, public.demo_ledger from anon, authenticated;
grant select on table public.account_profiles, public.demo_ledger to authenticated;

create policy "read own account profile"
  on public.account_profiles for select to authenticated
  using (user_id = (select auth.uid()));
create policy "read own demo ledger while active"
  on public.demo_ledger for select to authenticated
  using (
    user_id = (select auth.uid()) and exists (
      select 1 from public.account_profiles p
      where p.user_id = (select auth.uid()) and p.access_status = 'active'
    )
  );

create or replace view public.demo_balance
with (security_invoker = true)
as
  select p.user_id, 'SIM_IDR'::text as currency_code,
         coalesce(sum(l.amount_idr), 0)::bigint as balance_idr
  from public.account_profiles p
  left join public.demo_ledger l on l.user_id = p.user_id
  where p.access_status = 'active'
  group by p.user_id;
revoke all on public.demo_balance from anon, authenticated;
grant select on public.demo_balance to authenticated;

create or replace function public.issue_access_invite(p_ttl_days integer, p_email text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_code text;
  v_email text := nullif(lower(trim(p_email)), '');
begin
  if auth.uid() is null or not exists (
    select 1 from public.account_admins a where a.user_id = auth.uid()
  ) then
    raise exception 'not_authorized' using errcode = '42501';
  end if;
  if v_email is null or length(v_email) > 254 or v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then
    raise exception 'invalid_invite_email' using errcode = '22023';
  end if;
  v_code := upper(encode(extensions.gen_random_bytes(24), 'hex'));
  insert into public.access_invites(code_hash, created_by, invite_email, expires_at)
  values (
    extensions.digest(convert_to(v_code, 'UTF8'), 'sha256'),
    auth.uid(), v_email,
    now() + make_interval(days => greatest(1, least(coalesce(p_ttl_days, 7), 30)))
  );
  return v_code;
end;
$$;
revoke all on function public.issue_access_invite(integer, text) from public, anon;
grant execute on function public.issue_access_invite(integer, text) to authenticated;


create or replace function public.validate_access_invite(p_code text, p_email text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select case
    when p_code is null or length(trim(p_code)) <> 48
      or trim(p_code) !~ '^[A-Fa-f0-9]{48}$'
      or p_email is null or length(trim(p_email)) > 254
      then false
    else exists (
      select 1 from public.access_invites i
      where i.code_hash = extensions.digest(convert_to(upper(trim(p_code)), 'UTF8'), 'sha256')
        and i.invite_email = lower(trim(p_email))
        and i.claimed_by is null and i.disabled_at is null and i.expires_at > now()
    )
  end;
$$;
revoke all on function public.validate_access_invite(text, text) from public;
grant execute on function public.validate_access_invite(text, text) to anon, authenticated;

create or replace function public.redeem_access_invite(p_code text)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user uuid := auth.uid();
  v_hash bytea;
  v_invite uuid;
  v_email text;
  v_status text;
begin
  if v_user is null then
    raise exception 'authentication_required' using errcode = '42501';
  end if;
  if p_code is null or length(trim(p_code)) <> 48 or trim(p_code) !~ '^[A-Fa-f0-9]{48}$' then
    return false;
  end if;
  select p.access_status into v_status
    from public.account_profiles p where p.user_id = v_user;
  select lower(u.email) into v_email from auth.users u
   where u.id = v_user and u.email_confirmed_at is not null;
  if v_email is null then return false; end if;
  if v_status = 'blocked' then return false; end if;
  if v_status = 'active' then return true; end if;

  v_hash := extensions.digest(convert_to(upper(trim(p_code)), 'UTF8'), 'sha256');
  update public.access_invites i
     set claimed_by = v_user, claimed_at = now()
   where i.code_hash = v_hash
     and i.claimed_by is null
     and i.disabled_at is null
     and i.expires_at > now()
     and i.invite_email = v_email
   returning i.id into v_invite;
  if v_invite is null then return false; end if;

  insert into public.account_profiles(user_id, access_status)
  values (v_user, 'active');
  return true;
end;
$$;
revoke all on function public.redeem_access_invite(text) from public, anon;
grant execute on function public.redeem_access_invite(text) to authenticated;

-- Admin can grant/revoke simulated credits only. The append-only ledger is not
-- a payment wallet and must never be used to represent real customer deposits.
create or replace function public.admin_adjust_demo_balance(
  p_user_id uuid,
  p_amount_idr bigint,
  p_memo text,
  p_idempotency_key text
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_entry_type text;
  v_balance bigint;
begin
  if auth.uid() is null or not exists (
    select 1 from public.account_admins a where a.user_id = auth.uid()
  ) then
    raise exception 'not_authorized' using errcode = '42501';
  end if;
  if p_amount_idr is null or p_amount_idr = 0
     or p_amount_idr not between -1000000000000 and 1000000000000 or p_memo is null
     or length(trim(p_memo)) not between 1 and 240
     or p_idempotency_key is null or length(p_idempotency_key) not between 8 and 120 then
    raise exception 'invalid_adjustment' using errcode = '22023';
  end if;
  perform 1 from public.account_profiles p
   where p.user_id = p_user_id and p.access_status = 'active'
   for update;
  if not found then
    raise exception 'account_not_active' using errcode = '22023';
  end if;
  select coalesce(sum(l.amount_idr), 0)::bigint into v_balance
    from public.demo_ledger l where l.user_id = p_user_id;
  if v_balance + p_amount_idr < 0 then
    raise exception 'demo_balance_cannot_be_negative' using errcode = '22023';
  end if;
  v_entry_type := case when p_amount_idr > 0 then 'admin_demo_credit' else 'admin_demo_debit' end;
  insert into public.demo_ledger(user_id, amount_idr, entry_type, memo, idempotency_key, created_by)
  values (p_user_id, p_amount_idr, v_entry_type, trim(p_memo), p_idempotency_key, auth.uid());
  select coalesce(sum(l.amount_idr), 0)::bigint into v_balance
    from public.demo_ledger l where l.user_id = p_user_id;
  return v_balance;
end;
$$;
revoke all on function public.admin_adjust_demo_balance(uuid, bigint, text, text) from public, anon;
grant execute on function public.admin_adjust_demo_balance(uuid, bigint, text, text) to authenticated;

create or replace function public.is_current_user_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select auth.uid() is not null and exists (
    select 1 from public.account_admins a where a.user_id = auth.uid()
  );
$$;
revoke all on function public.is_current_user_admin() from public, anon;
grant execute on function public.is_current_user_admin() to authenticated;
