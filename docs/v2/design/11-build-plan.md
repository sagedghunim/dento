## 11. Build plan

| Milestone | Contents |
|---|---|
| M0, the base | Directory and two shards locally; the router and context; per-shard id ranges; `migrate_all`; RLS helpers and the policy test; `@api` and the page decorator with one transaction per request, `set_config`, request keys, `X-Membership`, `Ctx`; the business-day helper; accounts (sign-in, throttles in PostgreSQL, two-factor codes with encrypted secrets and recovery codes, forced password change); clinics (memberships, permissions and grant rules, services, schedules, counters, settings, specialty templates in code); audit tables and partitions; the page shell, `api.js`, the sign-in overlay, the clinic picker, the catalogue and its test; `seed_demo`; tests T1–T3, T10–T13 for what exists |
| M1, the front desk | Patients (search, by-phone, add, edit, file with visits and payments); booking with turns, slots, follow-ups, `arrive` and payment; status changes, cancel and reschedule; Today with the fragment, the ETag and the poller; invoices, lines, payments, refunds, discounts, voids; receipts; the clinic configuration read and `quote`; the platform admin; the end-of-day sweep and the recompute; tests T4–T9, T14–T16 and the plan check |
| M2, the doctor | Visit notes with versions and revisions, the medicine list, prescriptions and printing, the clinical tabs of the patient file |
| M3, the back office | Inventory, expenses and finances, staff pay, team and settings screens with invitations, specialty changes, consultations, the audit screens |
| M4 | Reminders (with a per-shard outbox), password reset, clinic sign-up, merging patient files, the day closing, exports, time-zone changes through a job |
| M5, before real patients | Synthetic data and the load test, PgBouncer and HA in compose, the backup and failover drills, moving a clinic between shards, the per-membership read limit, the idle lock, the runbooks |
