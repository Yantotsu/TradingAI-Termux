-- Require a verified second factor for all privileged write operations.
-- Missing AAL claims are treated as aal1 and fail closed.
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
  if auth.uid() is null
     or coalesce(auth.jwt() ->> 'aal', 'aal1') <> 'aal2'
     or not exists (select 1 from public.account_admins a where a.user_id = auth.uid()) then
    raise exception 'not_authorized' using errcode = '42501';
  end if;
  if v_email is null or length(v_email) > 254
     or v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then
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
  if auth.uid() is null
     or coalesce(auth.jwt() ->> 'aal', 'aal1') <> 'aal2'
     or not exists (select 1 from public.account_admins a where a.user_id = auth.uid()) then
    raise exception 'not_authorized' using errcode = '42501';
  end if;
  if p_amount_idr is null or p_amount_idr = 0
     or p_amount_idr not between -1000000000000 and 1000000000000
     or p_memo is null or length(trim(p_memo)) not between 1 and 240
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
