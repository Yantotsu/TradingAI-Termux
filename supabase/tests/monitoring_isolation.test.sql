begin;
select plan(11);

insert into auth.users (id, email)
values
  ('00000000-0000-4000-8000-000000000001', 'monitor-test@example.invalid'),
  ('00000000-0000-4000-8000-000000000002', 'member-test@example.invalid')
on conflict (id) do nothing;

insert into public.account_profiles(user_id, access_status)
values
  ('00000000-0000-4000-8000-000000000001', 'active'),
  ('00000000-0000-4000-8000-000000000002', 'active')
on conflict (user_id) do nothing;

insert into public.monitoring_admins(user_id)
values ('00000000-0000-4000-8000-000000000001')
on conflict (user_id) do nothing;

select throws_ok(
  $$insert into public.account_admins(user_id) values ('00000000-0000-4000-8000-000000000001')$$,
  '23514', 'monitoring_role_cannot_be_full_admin', 'monitor role cannot be upgraded to a full admin');

insert into public.service_monitoring(component, status, checked_at, latency_ms, build_id)
values ('site', 'healthy', now(), 25, 'test-build')
on conflict (component) do update
set status = excluded.status, checked_at = excluded.checked_at,
    latency_ms = excluded.latency_ms, build_id = excluded.build_id;

insert into public.demo_ledger(user_id, amount_idr, entry_type, memo, idempotency_key, created_by)
values
  ('00000000-0000-4000-8000-000000000001', 1000, 'admin_demo_credit', 'test fixture', 'monitor-test-key-1', '00000000-0000-4000-8000-000000000001'),
  ('00000000-0000-4000-8000-000000000002', 2000, 'admin_demo_credit', 'test fixture', 'monitor-test-key-2', '00000000-0000-4000-8000-000000000002')
on conflict (idempotency_key) do nothing;

select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-4000-8000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*) from public.service_monitoring), 1::bigint,
  'monitor sees coarse service status');
select is((select count(*) from public.account_profiles), 1::bigint,
  'monitor sees only its own profile');
select is((select count(*) from public.demo_ledger), 1::bigint,
  'monitor sees only its own simulated ledger');
select is((select count(*) from public.demo_balance), 1::bigint,
  'monitor sees only its own simulated balance');
select ok(not has_table_privilege('authenticated', 'public.monitoring_admins', 'SELECT'),
  'monitor role cannot enumerate role assignments');
select ok(not has_table_privilege('authenticated', 'public.service_monitoring', 'UPDATE'),
  'monitor role cannot alter service status');
select throws_ok(
  $$select public.admin_adjust_demo_balance('00000000-0000-4000-8000-000000000002', 10, 'monitor test', 'monitor-deny-key-1')$$,
  '42501', 'not_authorized', 'monitor cannot adjust another account balance');
select throws_ok(
  $$select public.issue_access_invite(1, 'someone@example.invalid')$$,
  '42501', 'not_authorized', 'monitor cannot issue member invitations');
reset role;

select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-4000-8000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*) from public.service_monitoring), 0::bigint,
  'AAL1 session cannot read monitoring data');
reset role;

select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-4000-8000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*) from public.service_monitoring), 0::bigint,
  'ordinary member cannot read monitoring data');
reset role;

select * from finish();
rollback;
