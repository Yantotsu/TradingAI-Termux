# Production readiness and payment safety

Status: **not production-ready**. The public Pages site is deployed, but its Supabase configuration is blank. The account schema and invite functions are source-only and have not been applied to a live Supabase project. This repository currently has no payment-provider integration and no real-money wallet.

Do not collect deposits, promise a stored rupiah balance, or enable real trading from this code. The existing ledger is simulation-only (`SIM_IDR`).

## Decide who holds customer money

Before implementing top-ups, document the legal entity, product, money flow, custody/settlement owner, refund policy, and regulator/partner responsibilities. A payment gateway does not by itself authorize the platform to hold customer funds or trade assets for customers. Bank Indonesia says PJP activities require the relevant authorization; OJK currently regulates crypto-asset trading providers, and other instruments have their own regimes. Obtain qualified legal and compliance review for the actual model.

Preferred lower-custody model: users keep cash and assets in their own regulated broker/exchange accounts; TradingAI connects only with minimum-scope trading credentials, with withdrawals disabled. This still needs a legal/compliance review for automated trading and any advice or execution service. If the product instead receives money into a platform balance and trades it for users, stop before launch until the entity has an approved operating and custody structure and a regulated execution partner.

## Payment safety requirements (before any real top-up)

1. Use an approved merchant account and hosted payment flow. Never ask for card, bank, exchange, or withdrawal credentials in the browser or chat.
2. Create a server-side payment order first. Store its owner, exact integer IDR amount, unique merchant reference, provider request ID, expiry, and immutable state transitions. The browser cannot set the credited amount, user ID, or paid status.
3. Receive provider callbacks only on a server-side endpoint. Verify the provider callback token/signature using constant-time comparison; reject unknown events and wrong environment/business IDs. Never credit from a success redirect or client request.
4. Verify the provider's server-side payment status and match the internal reference, exact amount, currency, and successful captured/settled state. Mismatches go to a locked reconciliation queue; no credit is issued.
5. Process every callback and credit in one database transaction. Put a unique constraint on provider event/payment ID and on the credit source order. Repeated, concurrent, or out-of-order callbacks must not create duplicate credits. Keep raw provider secrets out of logs; retain a redacted event record for audit.
6. Use an append-only, double-entry ledger with balanced entries, immutable source IDs, integer minor units, and a per-account balance lock. Never edit a balance directly. A correction is a new compensating entry with an administrator identity and reason.
7. Reconcile daily: provider payment report and settlement statement against internal orders, ledger credits, fees, refunds, chargebacks, and bank settlement. Freeze new credits for an affected merchant/account when totals do not match; notify the operator and preserve evidence.
8. Define refunds, chargebacks, partial payments, overpayments, late payments, expiry races, duplicate callbacks, provider outages, manual review, support SLA, and account closure before launch. A refund after funds were traded requires a pre-agreed recovery/negative-balance policy; do not silently erase ledger history.
9. Configure separate sandbox and production credentials, least-privilege API keys, rotation, webhook secret rotation, rate limits, monitoring, backups, restore drills, and restricted operator access. Production must remain disabled by default until the release gates below pass.

## Required end-to-end release gates

- Supabase project is deployed from migrations; Auth email confirmation, SMTP, redirect allowlist, RLS, and owner bootstrap are verified.
- Invite token tests: correct email succeeds; wrong email, expired token, blocked account, and reused token fail; simultaneous redemption grants one account only.
- Isolation tests use at least two users and prove neither can read the other's profile, ledger, payment orders, or balance through REST, RPC, Realtime, or guessed IDs.
- Payment-provider sandbox tests prove: wrong callback secret is rejected; browser success redirect never credits; wrong amount/currency/reference/business is quarantined; duplicate and concurrent callbacks credit exactly once; expiry and late success follow the documented policy; refund/chargeback is recorded as a compensating event.
- Verify payment settlement report, internal ledger, and the merchant bank settlement reconcile exactly. Verify webhook retries and provider outage recovery without manual balance edits.
- Complete a real small-value payment only after sandbox tests and written legal/merchant approval. Reconcile the provider report and actual bank settlement before enabling any broader access. A test payment is not evidence that the trading/custody model is authorized.
- Keep automated live trading disabled until every supported asset class has an approved execution provider, tested order/position reconciliation, risk limits, kill switch, and explicit user consent.

## External setup still required

No test or production merchant credentials are in the repository, and none should be committed. To perform a provider sandbox test, the owner must create/verify the merchant account, obtain sandbox API credentials and callback secret, and place them in the production backend's secret manager (sandbox project first). To perform an actual low-value settlement test, the owner must additionally complete merchant onboarding and provide a controlled test account and approval for that transaction. Never send these secrets through chat.

Useful primary references:

- Bank Indonesia, [PBI 23/6/PBI/2021 on Payment Service Providers](https://www.bi.go.id/id/publikasi/peraturan/pages/pbi_230621.aspx).
- OJK, [licensing and regulation for digital financial assets and crypto assets](https://ojk.go.id/id/fungsi-utama/itsk/perizinan-itsk-aset-keuangan-digital-aset-kripto/default.aspx) and [POJK 27/2024](https://www.ojk.go.id/id/regulasi/Documents/Pages/POJK-27-2024-AKD-AK/POJK%2027%20Tahun%202024%20Penyelenggaraan%20Perdagangan%20Aset%20Keuangan%20Digital%20Termasuk%20Aset%20Kripto%20AK.pdf).
- Xendit, [payment integration checklist](https://docs.xendit.co/docs/payments-integration-setup), [webhook authentication and idempotency guidance](https://docs.xendit.co/docs/handling-webhooks/), and [payment request status API](https://docs.xendit.co/apidocs/get-payment-request).
