# Hamada v2: design

Status: draft 2.1, 2026-11-07.
- Draft 2 applies the 29 changes the chief judge required after reviewing draft 1 (`docs/v2/reviews/draft1-chief-judge.md`). Appendix A maps each change to the sections that apply it; Appendix B maps the requirements.
- Draft 2.1 fixes the 6 blocking problems from the chief judge's check of draft 2 and takes its improvements I1–I4 (`docs/v2/reviews/draft2-chief-check.md`; Appendix C).
- The four reviews of draft 1 are in `docs/v2/reviews/`.
Requirements: `docs/v2/requirements.md`. References like (R4.3) point to its sections.

## 1. Summary

- **One Django application** (a modular monolith) on **stock PostgreSQL**, plus a background worker. The web tier is stateless.
- **The tenant is the clinic.** Every tenant row carries `clinic_id`. Every index on a tenant table starts with it. References between tenant rows are composite foreign keys on `(clinic_id, id)`. **Row-level security**, forced on every tenant table from the first migration, refuses any row of another clinic even if a query forgets its filter.
- **Growth by application-level shards (3.3).** Each shard is an ordinary PostgreSQL database holding whole clinics. A small **directory** database holds accounts, sessions and the clinic-to-shard map. A Django router sends each request, and each job transaction, to its clinic's shard. Stage 1 is one cluster with one directory and one shard. Ids come from per-shard ranges, so a clinic can move between shards with its ids unchanged.
- **Hot paths are bounded.** Today, booking, status changes, payments and patient search each run a fixed number of indexed queries inside one clinic's rows, whatever the total size.
- **Server-rendered pages** (Django templates) with a few small JavaScript modules. No framework and no build step. Actions go through JSON endpoints under `/api/v1/`. Arabic first (right to left) and English, from one translation catalogue.
- **The live queue uses conditional polling.** Each clinic's business day has a version, bumped in the same transaction as any change Today shows. Today's queue is an HTML fragment polled with an ETag; nearly every poll answers `304`.
- **Concurrency through locks in one global order, conditional updates and constraints.** Every flow's locks are listed in 5.0. Counters and money summaries change in the same transaction as the work.
- **Safe repeats** through a request-key table in each database. A key is claimed first, in the work's own transaction, with a hash of the request. A repeat replays the first result; the same key with a different body is refused.
- **The business day ends at a clinic-set hour** (default 04:00), not at midnight, because evening sessions run late.
- **Health data:** permissions per person checked on the server, with grant rules that stop escalation; an access log for every read of clinical data; a change log; two-factor sign-in for owners; strict headers; no patient data in logs.

## 2. Scale model

Assumptions from R2: 250,000 doctors, 15 visits per doctor per working day, 26 working days a month: about 100M visits a month.

| Table | Rows per month at the target | 2 years | Notes |
|---|---|---|---|
| appointment | 110M | 2.6B | 1.1 per visit, with cancellations and no-shows |
| invoice | 100M | 2.4B | |
| invoice_line | 130M | 3.1B | |
| payment | 105M | 2.5B | refunds included |
| patient | 40M new files | 1B | |
| visit_note (+ revisions) | 60M | 1.4B | |
| prescription / items | 50M / 125M | 1.2B / 3B | |
| inventory_movement | 50M | 1.2B | |
| audit_access | 200–280M | 24 months online | partitioned, about 220 B a row |
| audit_change | about 3M | kept with the financial records | |
| doctor_day / clinic_day | 6.5M / 4M | 156M / 96M | |
| expense | 3M | 72M | |
| core_request_key | about 30M a day | about 90M live (72 hours) | about 18 GB live, spread over the shards |
| account_event (directory) | 15M+ | 12 months | sign-ins and account changes |
| procrastinate jobs (directory) | small until M4 | | reminders in M4 add about 110M a month; cleanup planned with them |

- **Storage:** plan for about **10 TB after 2 years** (the database review's measured bytes per row, with organic index growth), and 4–5 TB more each year, since nothing clinical is deleted. At 1,000 doctors: about 40 GB after 2 years.
- **Peak write rate:** the evening (17:00–23:00 Cairo time) carries 60% of the day: about 105 visits a second. **Assumption:** the busiest hour is 1.5 times the evening average, so design for 160–210 visits a second and 1,100–1,500 write transactions a second, each 3–10 statements, all by key.
- **Read rate:** 100,000 screens polling every 5 s while visible is 20,000 requests a second, plus about 8% from hidden tabs. About 98% answer 304 (an average clinic changes its queue about 15 times an hour at the peak, against 720 polls an hour per screen). Searches: about 150 a second after debouncing; file opens: about 300 a second.
- **The biggest clinic:** 30 doctors, about 600 visits a day, 100,000+ files within 2 years. Its `clinic_day` version changes every 5–10 s at the peak, so its screens fetch a full queue more often (per-doctor versions are a deferred option, 13).
- **What forces a split:** not throughput but **the 2-hour restore** (R5). A shard is kept small enough that its restore drill passes. At about 10 TB that is roughly 10 shards after 2 years.

These are estimates. M5 checks them with synthetic data and a load test (10).

## 3. Architecture

### 3.1 Components

| Component | Technology | Role |
|---|---|---|
| web | Django 6.1 on Gunicorn | Pages, the JSON action API, print pages |
| worker | Procrastinate, stored in the directory database | The nightly sweep and recompute, request-key cleanup, partition upkeep, directory reconciliation; reminders and exports later |
| directory db | PostgreSQL 18 | Accounts, sessions, throttles, account events, the clinic directory, platform-admin data, jobs |
| shard dbs | PostgreSQL 18, one or more | Clinics and every tenant table |
| pgbouncer | PgBouncer, transaction pooling | Keeps database connections few as web containers grow |
| cache | Redis, optional | Only as a speed-up, from stage 2, after the M5 load test (5.11). Nothing in it is needed for correctness |
| proxy | Caddy | TLS, static files, compression |

### 3.2 Django apps

| App | Database | Owns |
|---|---|---|
| `core` | both | `@api` and the page decorator, `Ctx`, routing, request keys, the composite foreign key and RLS migration helpers, partitions, the business day, normalisation and the number parser, logging |
| `accounts` | directory | Users, sign-in, two-factor codes, recovery codes, throttles, sessions, account events |
| `directory` | directory | The clinic directory (`clinic_id → shard`), the `user_clinic` copy for the picker, the platform admin |
| `clinics` | shard | Clinics, memberships, permissions, services, schedules, counters, invitations; specialty templates live in code |
| `patients` | shard | Patient files and search |
| `scheduling` | shard | Appointments, turns, slots, status changes, reschedule |
| `billing` | shard | Invoices, lines, payments, refunds, receipts |
| `clinical` | shard | Visit notes and their revisions, the medicine list, prescriptions |
| `inventory` | shard | Items and the stock ledger |
| `finance` | shard | Expenses, categories, staff pay, the month view |
| `consults` | shard | Online consultation requests |
| `summaries` | shard | `doctor_day` and `clinic_day`. Depends only on `clinics` |
| `audit` | shard | `audit_change` and `audit_access` |
| `web` | — | Page templates, static files, print templates |

Rules:
- Only `services.py` modules change data. A service runs inside the request's single shard transaction. Views validate input and call one service.
- Each app owns its tables. Other apps read them through selectors.
- Every service takes a `Ctx` (5.0) and checks its permission codes itself, including codes that depend on the request body (5.8).

### 3.3 Databases, routing and shards

**Aliases.** `default` is the directory. `shard_<n>` are the shards (`shard_1`, `shard_2`, …). In development and tests: `hamada_dir`, `hamada_s001` and `hamada_s002` on the local PostgreSQL 18.

**The router** (`core/routing.py`):
- Directory apps (`accounts`, `directory`, `procrastinate`, Django's `contenttypes`, `auth` and `sessions`) read, write and migrate only on `default`.
- Tenant apps migrate on every shard and never on `default`. Their reads and writes go to the shard named in the current context. `@api`, the page decorator and the job runner set that context. A tenant query with no context raises; there is never a default shard.

**Ownership.** Each shard's `clinics_clinic` row has `owned`. The request transaction checks it together with the membership. A request that a stale directory cache sends to the wrong shard gets 503 with `Retry-After` and is routed again, so a clinic's writes can never be split across two shards during a move.

**No cross-database foreign keys, joins or transactions.** User ids on shard rows are plain integers. Flows that touch both databases are ordered, idempotent steps (5.12). The shard is the authority for memberships. The directory's `user_clinic` is a copy for the clinic picker: a nightly job reconciles it, and the picker re-checks on the shard.

**Ids.** Every identity sequence on shard *s* draws from [s × 2^48, (s + 1) × 2^48), set with MINVALUE and MAXVALUE so that exhaustion fails rather than collides. A post-migrate step sets the ranges, and a test fails if any sequence is out of range. Clinic ids come from the directory, and the shard's `clinics_clinic` row uses the same id. Shard numbers stay below 32, so every id stays under 2^53 and survives JavaScript's number type in JSON; a 33rd shard needs ids sent as strings first.

**Migrations.** `manage.py migrate_all` migrates the directory and every shard, and reports each one's state. Every tenant table gets its RLS policy in the migration that creates it (4.0).

**Jobs.** Procrastinate's tables live in the directory. Periodic jobs fan out by shard and by batches of about 500 clinics, with one transaction per clinic on its shard. M0–M3 queue no job from inside a request. When M4 needs that (reminders), it adds a per-shard outbox or a per-shard Procrastinate app; neither changes an M0 table.

### 3.4 Stages of growth

| Stage | Database | Web | Trigger to move on |
|---|---|---|---|
| 1 | One PostgreSQL cluster with two databases (`hamada_dir`, `hamada_s001`), a standby for failover, PgBouncer | 2–4 containers | Measured: the shard's restore drill approaches 2 hours, CPU above 60% at the peak, or the WAL rate near the standby's or archive's limit |
| 2 | A bigger server, or a second shard, whichever the trigger calls for. The directory can move to its own small cluster | 10–20 containers | The same measurements per shard |
| 3 | More shards (about 10 at 2 years at the target), each with a standby | 40+ containers | The same |

- New clinics go to the least-full shard.
- **Moving a clinic** (rehearsed in M5), in the 04:00–09:00 Cairo window:
  1. freeze the clinic: the directory marks it moving, **and the source shard clears `owned`**, so a request routed by a stale cache is refused there too (503 with `Retry-After`);
  2. wait until no transaction that started before that change is still running (`pg_stat_activity.xact_start`);
  3. copy its rows by `clinic_id`, table by table, to the target shard, and verify row counts and sums;
  4. set `owned` on the target, then switch the directory;
  5. delete the rows on the source in batches.
- A request checks the clinic (`owned`, status) **before** it claims its request key, and a missing clinic row counts as not owned (5.0).

### 3.5 Deployment

- One `docker compose` file with the components in 3.1. Configuration comes from environment variables.
- HTTPS everywhere; the proxy terminates TLS.
- **Open:** where to host it, which depends on the data protection review (R5).
- Local development: the portable PostgreSQL 18 in `C:\work\pgsql` on port 5433, shared with the warehouse project, with its own role and databases. The app runs on port 8002.

## 4. Data model

### 4.0 Rules

- **Keys.** Every table has a `bigint` identity primary key named `id`, from the shard's range (3.3).
- **Tenant tables** (everything in a shard):
  - `clinic_id bigint NOT NULL`;
  - `UNIQUE (clinic_id, id)` only on tables that a composite foreign key references;
  - a reference to another tenant row is a composite foreign key: `FOREIGN KEY (clinic_id, patient_id) REFERENCES patients_patient (clinic_id, id)`. In Django the field is `ForeignKey(..., db_constraint=False, db_index=False)`, and the constraint is `core.constraints.CompositeForeignKey` in `Meta.constraints`;
  - a database foreign key from `clinic_id` to `clinics_clinic` only on tables that have no composite foreign key (the others get the clinic through their parent);
  - actor columns (`created_by_id`, `updated_by_id`) are nullable plain bigints, for system jobs, with no foreign key: memberships are never deleted.
- **Indexes.** Every index on a tenant table starts with `clinic_id`. Every keyset index ends with `id`. Every child table has an index for the way it's read.
- **Row-level security** on every shard table that has `clinic_id`, in the migration that creates it:
  - `ENABLE` and `FORCE ROW LEVEL SECURITY`;
  - one policy for `USING` and `WITH CHECK`: `clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::bigint` (`id = …` on `clinics_clinic`). Without the setting there are no rows, on fresh and on reused connections.
  - A test fails for any shard table with `clinic_id` lacking `FORCE` and this policy.
  - Under a policy, operators that aren't leakproof (such as `LIKE`) lose their index ranges. Hot queries use range predicates instead (5.7), and M1 checks their plans as the web role.
- **Database roles** (production; development and tests use one owner role without BYPASSRLS, which `FORCE` subjects to the policies):
  - `hamada_owner` owns the tables and runs migrations. It has BYPASSRLS, for backfills.
  - `hamada_web` and `hamada_worker` have no bypass, DML grants only, and only INSERT and SELECT on the audit tables.
  - `hamada_maint` has BYPASSRLS for cross-clinic DML upkeep (request-key cleanup, platform counts from `clinic_day`), and no grants on patient tables. It is never a member of `hamada_owner`.
  - **Partition DDL** (create, attach, detach) needs to own the parent, so it runs as `hamada_owner` from a management command, like `migrate_all` (4.10).
  - Per-clinic jobs run as `hamada_worker`, with `set_config` in each transaction. A process that needs two roles uses two database aliases for the same shard (for example `shard_1` and `shard_1_maint`).
  - Timeouts are per-role settings (`ALTER ROLE … SET lock_timeout, statement_timeout, idle_in_transaction_session_timeout`). Any exception uses `SET LOCAL`.
- **Money** is `bigint` piastres with a `CHECK` on its sign.
- **Times** are `timestamptz` in UTC. A `day date` column holds the **business day** in the clinic's time zone: a time before the clinic's `day_ends_at` (default 04:00) belongs to the previous calendar day. One server helper, `business_day(clinic, instant)`, computes it everywhere; the browser never does.
- **Statuses** are text with a `CHECK` listing the allowed values. Only services change them.
- **Nothing clinical or financial is deleted.** Cancellations, voids and refunds keep the row. Clinics are suspended, not deleted.
- Search columns (`name_norm`, `phone_norm`) are declared `COLLATE "C"` on the column, not left to the cluster default.
- Text columns have length limits.

### 4.1 Directory tables

- `accounts_user`:
  - username (unique, lower case), phone (unique, nullable, national form), full_name, password, language (`ar` | `en`);
  - is_active, is_platform_admin, must_change_password, temp_password_expires_at;
  - totp_secret (encrypted with an application key), totp_confirmed_at, totp_last_step;
  - created_at.
- `accounts_recovery_code`: user_id, code_hash, used_at. Ten per user, single use.
- `accounts_throttle`: scope (`user_ip`, `user`, `ip`, `totp`), key, window_start, failures. Unique on (scope, key, window_start). Sign-in throttles live here, so they work without Redis (5.9).
- `accounts_account_event`: partitioned by month, kept 12 months. user_id, at, kind (`sign_in`, `sign_in_failed`, `totp_failed`, `password_changed`, `support_reset`, …), ip.
- `django_session`: Django's table, used through the `cached_db` backend.
- `directory_clinic`: id (the clinic id everywhere), shard, status (`active` | `suspended` | `moving`), name, created_at.
- `directory_user_clinic`: user_id, clinic_id, role, is_active. A copy for the picker; the shard is the authority.
- `directory_support_action`: platform support resets and ownership transfers, each with a reason, the requesting admin, the approving admin and the time (5.12).
- `core_request_key` (directory copy): for account and platform endpoints, keyed by (user_id, key) (4.11).
- Procrastinate's tables.

Platform admins hold no memberships. This is checked when a member is added and when the flag is set.

### 4.2 Clinic configuration (shard)

- `clinics_clinic`: id (from the directory), owned, name, city, address, phone, timezone, day_ends_at (default 04:00), specialty_code, status, settings (`jsonb`, validated by the settings registry in `clinics/settings.py`), created_at.
  - Settings include the follow-up days, the digits (`latn` | `arab`), the print language, letterhead offsets and whether every member needs a two-factor code.
  - The time zone and `day_ends_at` can't change once the clinic has bookings, except through a job that recomputes `day` (M4).
- `clinics_membership`: user_id, role (`owner` | `doctor` | `assistant`), display_name, title, is_active, granted `text[]`, revoked `text[]`, created_at.
  - `UNIQUE (clinic_id, user_id)`; `UNIQUE (clinic_id) WHERE role = 'owner'`; `CHECK (role <> 'owner' OR is_active)`; `UNIQUE (clinic_id, id)`.
- `clinics_service`: name, kind (`consultation` | `follow_up` | `procedure`), price, duration_min, follow_up_service_id (for consultations), is_active, sort. `UNIQUE (clinic_id, name)`, `UNIQUE (clinic_id, id)`.
- `clinics_schedule`: doctor_id, weekday, starts, ends, mode (`queue` | `slots`), slot_minutes, max_bookings. A change that would misalign future slot bookings is refused (5.1).
- `clinics_counter`: name (`patient_file`, `invoice`, `prescription`), value. `UNIQUE (clinic_id, name)`. A number is taken inside the transaction that uses it, so a rollback gives it back.
- `clinics_invitation` (M3): an existing account invited to join, with the role, created_by_id, expires_at and accepted_at.
- **Specialty templates** (services, medicines and a starter stock list per specialty) are data in `clinics/specialties.py`, copied into a clinic's tables when the clinic is created or its specialty changes (5.8).

**Permissions** are codes in `clinics/permissions.py`. The effective set is `(template[role] ∪ granted) − revoked`, intersected with the known codes; the owner always has everything.

| Code | Lets a person | Only the owner grants it |
|---|---|---|
| `appointments.book` | Book, reschedule, cancel | |
| `appointments.queue` | Arrive, call in, finish | |
| `patients.view` | Search and open patient details (not clinical content) | |
| `patients.edit` | Create and edit patient files | |
| `clinical.view` | Read visit notes and prescriptions | |
| `clinical.write` | Write visit notes and prescriptions | |
| `billing.collect` | Take payments, add and remove list-price lines | |
| `billing.discount` | Discounts and prices not from the list | |
| `billing.refund` | Refunds and voids | yes |
| `inventory.use` | Record stock use | |
| `inventory.manage` | Items, purchases, adjustments | |
| `finances.view` | The month's money and staff pay | yes |
| `finances.pay` | Edit staff pay | yes |
| `expenses.manage` | Add and void expenses | |
| `consults.manage` | Online consultation requests | |
| `settings.manage` | Clinic profile, services, prices, schedules, medicines | |
| `team.manage` | Members and permissions | yes |
| `audit.view` | The change and access logs | yes |

| Role template | Codes |
|---|---|
| owner | All |
| doctor | appointments.*, patients.*, clinical.view, clinical.write, billing.collect, inventory.use, consults.manage |
| assistant | appointments.*, patients.view, patients.edit, billing.collect, inventory.use |

### 4.3 Patients

- `patients_patient`:
  - file_no (from the counter), full_name, name_norm (`COLLATE "C"`), phone, phone_norm (`COLLATE "C"`);
  - sex, birth_date, birth_date_estimated, address, notes, allergies, chronic_conditions;
  - created_at, created_by_id, updated_at.
  - `UNIQUE (clinic_id, id)`, `UNIQUE (clinic_id, file_no)`.
  - Index `(clinic_id, name_norm) INCLUDE (id, full_name, phone, file_no)`, so search is an index-only scan.
  - Index `(clinic_id, phone_norm) INCLUDE (id, full_name, file_no)`.
  - Autovacuum: `autovacuum_vacuum_insert_scale_factor = 0`, `autovacuum_vacuum_insert_threshold = 50000`, `autovacuum_vacuum_scale_factor = 0.02`, so the visibility map stays current. The same on appointments and invoices.

### 4.4 Scheduling and summaries

- `scheduling_appointment`, `fillfactor = 85`:
  - patient_id, doctor_id, service_id, day, queue_no, starts_at and ends_at (slot bookings only), is_extra;
  - status (`booked` | `waiting` | `in_room` | `done` | `cancelled` | `no_show`), source (`clinic` | `phone` | `online` | `whatsapp`);
  - fee, is_follow_up, is_new (first visit of this patient), auto_closed (closed by the end-of-day sweep), note;
  - arrived_at, called_at, finished_at, cancelled_at, cancel_reason;
  - created_at, created_by_id, updated_at, updated_by_id;
  - `holds_slot boolean GENERATED ALWAYS AS (starts_at IS NOT NULL AND NOT is_extra AND status NOT IN ('cancelled', 'no_show')) STORED`.
  - CHECKs: the lists; fee >= 0; starts_at and ends_at both null or both set with ends_at > starts_at; the timestamps each status needs.
  - `UNIQUE (clinic_id, day, doctor_id, queue_no)`, which also serves Today.
  - `UNIQUE (clinic_id, doctor_id, starts_at) WHERE holds_slot`. Its predicate reads only the generated column, so status changes stay HOT updates (verified: 100% against 0%).
  - Index `(clinic_id, patient_id, day DESC, id DESC)` for the patient's history.
  - `UNIQUE (clinic_id, id)`. Composite foreign keys to patient, membership (doctor) and service.
  - `updated_at` is never indexed.
- `summaries_doctor_day`: doctor_id, day, last_queue_no, booked (active bookings, for `max_bookings`), done, collected, refunded. `UNIQUE (clinic_id, day, doctor_id)`. Every counter >= 0.
- `summaries_clinic_day`: day, version, done, collected, refunded, expenses, changed_since_recompute (set by every change to that day's sources, cleared by the recompute, 5.10). `UNIQUE (clinic_id, day)`. Every counter >= 0.
- Status counts on Today come from the day's rows that Today already reads, so the summaries hold only what finance, limits and the version need. Fewer counters means less to drift.

### 4.5 Billing

- `billing_invoice`: number, patient_id, appointment_id (nullable), status (`open` | `void`), subtotal, discount, total, paid, created_at, created_by_id, voided_at, voided_by_id.
  - CHECKs: `0 <= discount <= subtotal`, `total = subtotal - discount`, `0 <= paid <= total`, a void invoice has paid = 0.
  - `UNIQUE (clinic_id, number)`; `UNIQUE (clinic_id, appointment_id) WHERE status = 'open'` (every `ON CONFLICT` on it repeats the predicate); `UNIQUE (clinic_id, id)`.
  - Index `(clinic_id, patient_id, created_at DESC, id DESC)`. A partial index for unpaid invoices only when a screen uses it (M3).
- `billing_invoice_line`: invoice_id, kind (`service` | `procedure` | `material` | `other`), service_id, item_id, description, qty, unit_price, amount, from_list. `CHECK (amount = qty * unit_price AND qty > 0)`. Index `(clinic_id, invoice_id, id)`.
- `billing_payment`: invoice_id, patient_id, doctor_id (copied at insert), kind (`payment` | `refund`), method (`cash` | `card` | `wallet` | `instapay` | `other`), amount, refunded, refund_of_id, received_at, day, received_by_id, note.
  - CHECKs: `amount > 0`, `0 <= refunded <= amount`, only payment rows have refunded > 0, a refund row has refund_of_id.
  - `UNIQUE (clinic_id, id)` (refunds reference payments). Index `(clinic_id, invoice_id, id)`; index `(clinic_id, patient_id, received_at DESC, id DESC)`; index `(clinic_id, day, received_by_id)` for the day closing.

### 4.6 Clinical

- `clinical_visit_note`: appointment_id, patient_id, doctor_id, complaint, diagnosis, notes, version, created_at, updated_at, updated_by_id. `UNIQUE (clinic_id, appointment_id)`, `UNIQUE (clinic_id, id)`. Index `(clinic_id, patient_id, created_at DESC, id DESC)`.
- `clinical_visit_note_revision`: note_id, version, the old complaint, diagnosis and notes, replaced_at, replaced_by_id. Written when a note is edited after its visit day. Index `(clinic_id, note_id, version)`.
- `clinical_medicine`: name, form, default_dose, is_active. `UNIQUE (clinic_id, name)`.
- `clinical_prescription`: number, patient_id, doctor_id, appointment_id, notes, created_at. `UNIQUE (clinic_id, number)`, `UNIQUE (clinic_id, id)`. Index `(clinic_id, patient_id, created_at DESC, id DESC)`.
- `clinical_prescription_item`: prescription_id, position, medicine_name, form, dose, quantity, days, instructions. `UNIQUE (clinic_id, prescription_id, position)`. Names and doses are copies; no service updates these rows.

### 4.7 Inventory

- `inventory_item`: name, unit, pack, quantity, threshold, unit_cost, is_active. `UNIQUE (clinic_id, name)`, `UNIQUE (clinic_id, id)`. `CHECK (quantity >= 0 AND threshold >= 0 AND unit_cost >= 0)`. No partial low-stock index: its predicate would make every stock use a non-HOT update, and a clinic's few hundred items scan in under a millisecond.
- `inventory_movement`: item_id, kind (`purchase` | `use` | `adjust`), qty (signed), balance_after, unit_cost, patient_id, appointment_id, expense_id, reason, created_by_id, at. Index `(clinic_id, item_id, at DESC, id DESC)`; index `(clinic_id, at DESC, id DESC) WHERE kind = 'use'`.

### 4.8 Finance

- `finance_category`: name, is_system. `UNIQUE (clinic_id, name)`, `UNIQUE (clinic_id, id)`.
- `finance_expense`: category_id, name, amount, day, purchase (bool), created_at, created_by_id, voided_at, voided_by_id. `CHECK (amount > 0)`. `UNIQUE (clinic_id, id)`. Index `(clinic_id, day DESC, id DESC)`. A purchase's expense can only be reversed together with the purchase (M3).
- `finance_staff_pay`: membership_id, name, title, kind (`fixed` | `percent`), amount, percent. `CHECK (percent BETWEEN 0 AND 100)`.

### 4.9 Online consultations (M3)

- `consults_request`: patient_id, doctor_id, reason, scheduled_at, status, the time of each change. Index `(clinic_id, status, scheduled_at, id)`.

| Action | From | To |
|---|---|---|
| accept | new | accepted |
| decline | new | declined |
| start | accepted | in_call |
| end | in_call | done |
| cancel | new, accepted | cancelled |

### 4.10 Logs

- `audit_change` (shard): actor_kind, membership_id, action, target_type, target_id, details (`jsonb`, before and after), ip, request_id, at. Index `(clinic_id, at DESC, id DESC)` and `(clinic_id, target_type, target_id, at DESC, id DESC)`. Low volume. Kept as long as the financial records; **Open:** the legal period (likely at least 5 years).
- `audit_access` (shard), partitioned by month on `at`:
  - membership_id, patient_id, action (`smallint`: open, print, export, note, prescription, invoice, history), at, ip, request_id;
  - `PRIMARY KEY (clinic_id, at, id)`, which also serves a clinic's newest-first list; index `(clinic_id, patient_id, at, id)` for "who opened this file";
  - kept 24 months online, then detached, archived and dropped.
- `accounts_account_event` (directory, 4.1).
- **Partition upkeep** (`core/partitions.py`, `manage.py ensure_partitions`), as `hamada_owner`: each new partition is created standalone with a CHECK matching its bounds, **given RLS and the standard policy before it is attached**, then attached (ShareUpdateExclusiveLock, so file opens don't wait). Old partitions leave by `DETACH PARTITION … CONCURRENTLY`. A daily run keeps 3 months ahead, and an alert fires below 2. CI runs the upkeep once under the production roles (M5).

**Why the business tables aren't partitioned by time.** Appointments, invoices, payments and the stock ledger are read per clinic across all time, and other rows refer to them. Time partitions would force the partition column into every foreign key and unique key, and turn a patient's history into a probe of every partition. They grow out by shards instead.

### 4.11 Request keys

- `core_request_key` in every shard: `PRIMARY KEY (clinic_id, key)`, membership_id, endpoint, request_hash (SHA-256 of the method, the path and the canonical body), status (the HTTP status of the first answer), target_type, target_id, created_at. BRIN index on created_at. Forced RLS like every tenant table.
- The directory's copy is keyed by (user_id, key), for account and platform endpoints.
- No response body and no patient data are stored: a replay rebuilds the answer from the target.
- Rows live at least 72 hours. A nightly job, as `hamada_maint`, deletes older rows in batches of about 5,000 in the 04:00–09:00 window.
- Not partitioned by day: a day-partitioned key could be claimed again after midnight (verified by the chief judge).

## 5. Core flows

### 5.0 General rules

**One transaction per request, on the clinic's shard.** `@api` (and the page decorator) does, in order:
1. checks the method, the session, `must_change_password`, the two-factor state (5.9) and `X-Membership` against the session's active membership (409 `clinic_changed`);
2. opens a transaction on the shard and runs `SELECT set_config('app.clinic_id', <id>, true)`. Never a session `SET`;
3. loads the membership and the clinic (`owned`, status) in one query. A missing clinic row, or one not `owned`, is 503 with `Retry-After` (3.4). It computes the effective codes;
4. for a mutating request, claims the request key (5.0 "Repeats");
5. checks the view's declared permission, builds the `Ctx` and calls the view. Any answer other than 2xx rolls the transaction back, the key claim included.

**`Ctx`** carries the clinic, the membership, the user, the actor kind (`member`, `system` or `platform`), the effective codes, `mfa_verified`, the request id and the IP. Only the decorators and the job runner build it. Services that read patient data refuse `system` and `platform` contexts.

**Repeats.** Every mutating request carries an `Idempotency-Key` header (a UUID), required by `@api`. The exceptions are sign-in, the code and sign-out, which are naturally single-use: a replayed code request never signs anyone in again, because the code's step is already used.
- `INSERT INTO core_request_key … ON CONFLICT DO NOTHING RETURNING 1`, right after `set_config`.
- Claimed: do the work and record the target in the same transaction.
- Conflict, with the same endpoint, hash and membership: replay the first result, rebuilt from the target. Anything else is 422 `request_key_reused`.
- A concurrent copy waits on the claim, then replays. Past `lock_timeout` it gets 409 `busy`, and the client resends the same key.
- A refusal (any 4xx) rolls back the claim with the work. Nothing was written, so the same key may run again.
- `PUT` and `PATCH` send absolute values with the expected old value or version.

**Lock order.** Every transaction takes locks in this global order:
1. the request key;
2. the appointment (`SELECT … FOR NO KEY UPDATE`, or a conditional `UPDATE`);
3. the invoice;
4. payment rows;
5. inventory items;
6. expenses;
7. counter rows, in the order `patient_file`, `invoice`, `prescription`;
8. `summaries_doctor_day` rows;
9. `summaries_clinic_day` rows.

A flow writes **all** its changes to one summary table in **one statement**, in key order (doctor-days by (day, doctor_id), clinic-days by day), for example a payment on a past day's visit that changes the payment day's `collected` and the visit day's version.

Several rows of one table are locked in one statement in key order (`ORDER BY id FOR NO KEY UPDATE`; doctor-days by (day, doctor_id); clinic-days by day). Inserting a new row is not a lock anyone waits on, except through a unique index.

| Flow | Locks, in order |
|---|---|
| Booking (5.1), with optional arrive and payment | key → [new patient: counter patient_file] → [payment: counter invoice, taken here and kept for `ensure_invoice`] → doctor_day (one upsert: turn, booked, collected) → clinic_day (one upsert) |
| Status change (5.2) | key → appointment (conditional UPDATE) → doctor_day → clinic_day |
| Finish with payment | key → appointment → invoice (or counter invoice for a new one) → doctor_day → clinic_day |
| Reschedule | key → appointment → both doctor_day rows in one statement → both clinic_day rows in one statement |
| Cancel | key → appointment → invoice (read; void if empty) → doctor_day → clinic_day |
| Payment, line, discount, void (5.3) | key → appointment → invoice (or counter invoice) → doctor_day → clinic_day |
| Refund | key → appointment → invoice → the payment → doctor_day (refund day) → clinic_day (refund day and the appointment's day, one statement) |
| Stock use, several items (5.4) | key → items in one statement by id → clinic_day (only if an item crosses its threshold) |
| Purchase, adjustment | key → item → clinic_day (purchase: expenses) |
| Expense and its void | key → expense → clinic_day |
| Prescription (5.5) | key → counter prescription |
| Visit note (5.6) | key → note (conditional on version) |
| End-of-day sweep (5.10) | the day's open appointments in one statement by id → for in-room visits without an invoice, counter invoice → that clinic-day's doctor_day rows in one statement → clinic_day |
| Recompute (5.10) | that clinic-day's doctor_day rows `FOR UPDATE` in one statement → clinic_day `FOR UPDATE` → aggregate the source rows (no locks) → write |

- **Deadlocks** (`40P01`): the server retries the whole transaction once. **Lock timeouts** (`55P03`): the request has already waited 2 s, so it answers 409 `busy` with `Retry-After`, and the client resends the same key.
- **Errors:** services raise `ApiError(code, message, status)`. Constraint violations that users can cause map to 409 with a code. Anything else is a 500, logged with the request id, and never retried by the client.

### 5.1 Booking

`POST /api/v1/appointments`: patient (an id, or a new patient's name, phone, sex and age), doctor, service, optionally day (default: the current business day) and a slot time, source, `arrive` (bool), and an optional payment `{amount, method}`.

1. Validate:
   - the doctor is an active owner or doctor who works that weekday;
   - the service is active and in this clinic;
   - the day is the current business day or up to a year ahead;
   - for slot doctors, the time is on that weekday's grid;
   - a new patient needs `patients.edit`, and a payment needs `billing.collect`.
2. A new patient gets a file number from the counter. With a payment, the invoice number is taken right after it and kept for `ensure_invoice`, so the counters come before the summary rows, as in every other flow. A payment inside a booking is accepted only for the current business day; a prepayment for a later day uses the payment endpoint after booking.
3. **Fee and the follow-up rule** (an assumption for Saged, R4.1):
   - a consultation for a patient whose last consultation with the same doctor arrived or was paid within the follow-up window, counted back from the new appointment's day, uses the price of the consultation's `follow_up_service_id`, and `is_follow_up` is set;
   - a follow-up doesn't start a new window;
   - the rule is checked again at arrival.
   This costs one indexed query on the patient's history.
4. **Turn:**
   ```sql
   INSERT INTO summaries_doctor_day (clinic_id, day, doctor_id, last_queue_no, booked, …)
   VALUES (:c, :d, :doc, 1, 1, …)
   ON CONFLICT (clinic_id, day, doctor_id) DO UPDATE
     SET last_queue_no = summaries_doctor_day.last_queue_no + 1,
         booked = summaries_doctor_day.booked + 1
     WHERE :max IS NULL OR summaries_doctor_day.booked < :max
   RETURNING last_queue_no
   ```
   No row returned means the day is full: 409 `day_full`.
5. Insert the appointment. A taken slot violates the slot index; the whole transaction rolls back, the turn number included, and the answer is 409 `slot_taken`.
6. With `arrive`: status `waiting` and arrived_at now (only on the current business day). With a payment: the payment flow in 5.3 inside the same transaction, using the invoice number from step 2. The turn, `booked` and `collected` go into the step 4 upsert, so `doctor_day` is written once.
7. `clinic_day`: version + 1 (and `collected` with a payment), in one upsert.

The answer is the new row (turn, invoice id, amount due) and the new version.

`GET /api/v1/appointments/quote?patient=&doctor=&service=&day=` returns the fee and `is_follow_up` for the Book dialog, using the same rule.

A schedule change that would leave a future slot booking off the grid, or outside the doctor's hours, is refused with the list of affected bookings.

### 5.2 Status changes

| Action | From | To | Sets | doctor_day | clinic_day |
|---|---|---|---|---|---|
| arrive | booked | waiting | arrived_at | | version |
| undo_arrive (pending Saged) | waiting | booked | clears arrived_at | | version |
| call | waiting | in_room | called_at | | version |
| finish (optional payment) | in_room | done | finished_at | done + 1 | done + 1, version |
| cancel | booked | cancelled | cancelled_at, reason | booked − 1 | version |
| no_show (sweep only) | booked | no_show | | | version |
| reschedule | booked | booked | new day, doctor, slot, turn | old row booked − 1, new row booked + 1 | version on both days |

- The client sends the status it saw. The service runs `UPDATE … SET status = :to … WHERE clinic_id = :c AND id = :id AND status = :from RETURNING …`.
  - One row: update the counters and bump the version.
  - No row: read the appointment. Missing is 404. Already in the target status is 200 (a harmless repeat). Anything else is 409 `status_changed` with the current status.
- **arrive** is allowed only on the appointment's business day.
- **cancel:** while the visit's invoice has `paid > 0`, the answer is 409 `refund_first`. An open invoice with nothing paid is voided in the same transaction.
- **reschedule:**
  - a target equal to the current doctor, day and slot is a 200 that changes nothing;
  - a move within the same doctor-day keeps its turn number;
  - otherwise it takes a new turn on the new doctor-day, so the old number is a gap (turn numbers may have gaps; invoice, file and prescription numbers may not);
  - `last_queue_no` never goes down.
- **finish** with a payment runs the 5.3 payment flow in the same transaction (R4.3).
- Every action answers with the updated row and the new version.

### 5.3 Invoices and payments

- **ensure_invoice** (under the appointment lock): read the visit's open invoice; if there is none, take a number from the counter and insert it, with one line for the service at the appointment's fee. Every billing action calls it: a payment, a line, a discount, finish. Nothing at arrival, so no-shows take no numbers.
- **Payment** `POST /api/v1/appointments/{id}/payments` `{amount, method}`:
  1. lock the appointment (`FOR NO KEY UPDATE`); refuse a cancelled visit;
  2. ensure_invoice;
  3. `UPDATE billing_invoice SET paid = paid + :a WHERE clinic_id = :c AND id = :i AND status = 'open' AND paid + :a <= total`. No row is 409 `over_balance`;
  4. insert the payment with the patient, the appointment's doctor and the business day;
  5. `doctor_day.collected` (payment day, doctor) and `clinic_day.collected` (payment day) go up; the version of the appointment's day is bumped.
- **Lines:** `POST /api/v1/invoices/{id}/lines` adds a line. A list price needs `billing.collect`; any other price needs `billing.discount`. `DELETE /api/v1/invoices/{id}/lines/{line}` removes one, only while `paid` stays <= the new total.
- **Discount:** `PUT /api/v1/invoices/{id}/discount` with `{discount, expected_discount}`, absolute, under `billing.discount`. It is refused if the expected value doesn't match or if `paid` would exceed the new total. Audited.
- **Refund** (`billing.refund`): lock the appointment, then the invoice; `UPDATE billing_payment SET refunded = refunded + :a WHERE … AND refunded + :a <= amount`; insert a refund row (its own business day, the original's doctor); take the amount off `invoice.paid`; `doctor_day.refunded` and `clinic_day.refunded` go up on the refund's day. Audited.
- **Void** (`billing.refund`): only while paid = 0. Audited. A void invoice no longer blocks a new one for the visit, since the one-invoice index covers only open invoices.
- Revenue is counted when cash comes in (`payment.day`). Net revenue = collected − refunded, per doctor and per clinic.

**Counters each action changes.** The recompute (5.10) and the invariant tests (T7) both read this table. Every row also sets `changed_since_recompute` on each `clinic_day` it touches.

| Action | `doctor_day` | `clinic_day` | Recompute formula |
|---|---|---|---|
| book | appointment's day and doctor: `last_queue_no` + 1, booked + 1 | appointment's day: version | booked = appointments of that doctor-day not cancelled; `last_queue_no` is never lowered |
| arrive, undo_arrive, call | | version | |
| finish | done + 1 | done + 1, version | done = appointments with status done |
| cancel | booked − 1 | version | (as book) |
| reschedule | old row booked − 1, new row booked + 1 and a turn (one statement) | version on both days (one statement) | (as book) |
| sweep: booked → no_show; waiting → no_show; in_room → done | done + 1 for in-room visits | done, version | (as above) |
| pay | payment day, appointment's doctor: collected + a | payment day: collected + a; appointment's day: version (one statement) | collected = Σ payment rows of that day (and doctor) |
| refund | refund day, original doctor: refunded + a | refund day: refunded + a; appointment's day: version | refunded = Σ refund rows of that day (and doctor) |
| line, discount, void | | appointment's day: version | |
| expense; its void | | expense day: expenses ± amount | expenses = Σ expenses of that day not voided |
| purchase | | today: expenses + amount | (as expense) |
| stock use crossing a threshold | | today: version | |
| patient edit, schedule change | | today: version | |

### 5.4 Stock

- **Use**, `POST /api/v1/inventory/uses` with one or more `{item, qty}`, optionally for a patient or appointment:
  - lock the items in one statement by id;
  - for each, `UPDATE inventory_item SET quantity = quantity - :q WHERE clinic_id = :c AND id = :i AND quantity >= :q RETURNING quantity, unit_cost, threshold`. No row is 409 with the quantity left;
  - a `use` movement per item, with the cost at that moment;
  - bump the version only if an item fell to or below its threshold (Today's banner).
  - The `CHECK` stops a negative quantity even if a service forgets the condition.
- **Purchase:** quantity up, the unit cost set, an expense (`purchase = true`), a `purchase` movement, and `clinic_day.expenses`.
- **Adjustment** (`inventory.manage`): `UPDATE … SET quantity = :counted WHERE … AND quantity = :expected`, an `adjust` movement with the difference and the reason, and an audit event.

### 5.5 Prescriptions

- 1 to 30 items. Each is a medicine from the list (copied) or typed text, with the dose, the amount (1–200), the days (1–365) and instructions. It needs `clinical.write`.
- The number comes from the counter. One insert for the prescription and one bulk insert for the items.
- The print page `/print/prescriptions/<id>?lang=ar|en` (default: the clinic's print language) writes an access event.

### 5.6 Visit notes

- A note has a `version`. `PUT /api/v1/appointments/{id}/note` sends `If-Match: <version>`. The update is conditional on the version. A mismatch is 409 with the current text, which the screen shows next to the draft to merge.
- An edit after the visit's business day first copies the old text into `clinical_visit_note_revision`.
- Reading a note needs `clinical.view` and writes an access event.

### 5.7 Patient search

- **Normalising**, in Python (`core/text.py`), for both stored names and queries: Arabic-Indic digits become 0–9; the forms of alef become ا, ة becomes ه, ى becomes ي, ؤ becomes و, ئ becomes ي; diacritics and tatweel are removed; text is case-folded and spaces are collapsed.
- **A query of digits:** phone_norm by prefix (normalised from `+20`, `0020` and `20`), or file_no exactly.
- **A name query:**
  1. prefix, written as a range so it keeps its index range under RLS: `name_norm >= :q AND name_norm < :q_next`, limit 20;
  2. if that found fewer than 20, a word prefix: `name_norm LIKE '% ' || :q || '%'` with `%`, `_` and `\` escaped, filling up to 20.
  - The second query scans only this clinic's part of the covering index, as an index-only scan: about 17 ms for 100,000 files when vacuumed (verified by two reviewers).
  - Search runs with `SET LOCAL statement_timeout = '300ms'`.
- **The client:** a 250 ms debounce, at least 2 characters, the previous request aborted.
- Results show the name, phone, file number, age, sex, any booking today and the last visit. Picking a result doesn't open the file, so it writes no access event.
- **Duplicate phone:** `GET /api/v1/patients/by-phone?phone=` lists this clinic's files with that phone. The Book dialog shows them before a new file is created (R4.2).
- M1 checks the plans of both queries for a 100,000-file clinic as the web role.
- Clinics past about 200,000 files can get a word table `(clinic_id, word, patient_id)` that a job fills (deferred).

### 5.8 Team, permissions and specialty

- **Granting:**
  - only `team.manage` changes members or permissions;
  - a non-owner can give, remove or reach through a role change only codes they hold themselves (`new − current ⊆ actor's codes`);
  - only the owner gives or removes the owner-only codes (4.2);
  - nobody edits their own membership; only the owner edits a member who holds `team.manage`;
  - the owner can't be removed or demoted; ownership moves only through the platform admin, to an active doctor of the clinic (5.12).
- **Adding a member:**
  - a new account: created in the directory with a temporary password that expires in 72 hours (5.12);
  - an existing account (M3): an invitation its holder accepts. The answer is the same for known and unknown usernames;
  - clinic endpoints change memberships, and may create a new account, but never change an existing account.
- Every change writes an `audit_change` event with the before and after. Permissions are read from the membership row on every request, so a change applies at once.
- **Changing the specialty** (M3) copies the new template's services, medicines and stock items that the clinic doesn't have yet. It never deletes or deactivates anything. Audited.

### 5.9 Signing in

1. `POST /api/v1/auth/login` with a username or phone and a password.
   - Throttles in PostgreSQL (`accounts_throttle`), counting failures only:
     - 5 per (username, IP) in 15 minutes;
     - 20 per username in 15 minutes;
     - 200 per IP in 15 minutes, loose because clinic routers and carrier NAT share addresses.
   - When a limit is hit, the answer says how long to wait. The throttle fails closed. Redis can only speed it up.
   - An unknown username still runs a dummy password check. One generic error for a wrong username or password.
2. **Two-factor codes** are required for owners and platform admins, and for every member when the clinic's setting says so.
   - After the password, the session holds a pending sign-in (user id, expires in 5 minutes). It doesn't count as signed in for `@api`.
   - `POST /api/v1/auth/totp` accepts a code (±1 step) by `UPDATE accounts_user SET totp_last_step = :s WHERE id = :u AND totp_last_step < :s`, so one code can't be used twice, even on two containers. A recovery code also works, once.
   - Five wrong codes end the pending sign-in and count toward the username limit.
   - After the code, the session key changes and `mfa_verified` is set.
3. `@api` refuses everything except changing the password while `must_change_password` is set, and everything except code set-up while a required code isn't set up. The password must be changed before the code is set up. Code set-up shows the QR code (segno, as SVG) and 10 recovery codes, which are stored hashed.
4. **The active clinic:** with one active membership it becomes the active one; with several, the picker. `POST /api/v1/me/clinic` re-checks the membership on the shard. When the chosen clinic requires a code and `mfa_verified` isn't set, the code is asked for first.
5. **Sessions:** the `db` backend until a shared Redis exists, and `cached_db` only over Redis: a per-process cache would keep a signed-out session valid on other workers. A fixed 12 hours, never saved by a poll. `GET /api/v1/me` returns `session_expires_at`. A password change signs out the other sessions.
- TOTP secrets are encrypted with an application key (`HAMADA_FIELD_KEY`, AES-GCM through Python's standard library primitives or a vetted package; rotation is a runbook).

### 5.10 Jobs

All periodic jobs fan out by shard and by batches of about 500 clinics, with one transaction per clinic.

- **End-of-day sweep**, per clinic after `day_ends_at` plus 30 minutes, for the business day just ended:
  - `booked` → `no_show`;
  - `waiting` → `no_show` with `arrived_at` kept and `auto_closed` set ("left without being seen", pending Saged);
  - `in_room` → `done` with `auto_closed` set, its invoice ensured and flagged for the owner.
- **Recompute**, one clinic-day per transaction, for every past day whose `changed_since_recompute` is set. It runs after the business day ends. It locks the summary rows `FOR UPDATE` first, then aggregates the source rows, then writes. A difference is fixed, logged, and counted in a metric.
- **Request-key cleanup**, partition upkeep, expired sessions and throttles, and the `user_clinic` reconciliation, as `hamada_maint`.
- Jobs run with a system `Ctx`. Their audit actor is "system", and their `created_by_id` is null.
- The Procrastinate worker connects straight to PostgreSQL for LISTEN, not through PgBouncer's transaction mode; or it polls.

### 5.11 The live queue

- **The page:** `/today` renders the shell. Its queue is the fragment `/today/queue`, polled with `If-None-Match`.
- **The ETag:** `"<clinic>.<day>.<version>.<hash of the person's codes and language>"`. Without `?day=`, the server uses the current business day. The answer carries the day, the server time and the next rollover, so at the rollover the ETag changes and a normal 200 arrives.
- **The server reads the version before the data**, so a commit landing in between never pairs a new ETag with old data.
- **The real cost of a 304** in stage 1: the session and the user (directory, 2 queries), `set_config`, the membership with the clinic, and the version (shard, 3 statements). There's no membership cache. From stage 2, `cached_db` takes the session off the database. A Redis version path waits for the M5 load test; if built, it only ever sets a higher value, with a 30–60 s TTL.
- **Every answer, 304s included, carries `Poll-After`** (seconds), so operations can slow every screen at once. `Cache-Control: no-store` on every answer.
- **What bumps the version:** every status change, booking, reschedule, cancel; every payment, refund, line, discount and void (on the appointment's day); a patient edit (today's version); stock use that crosses a threshold; a schedule change (today's version).
- **The full fragment:**
  - the doctors working that day;
  - the day's appointments, with patient name, phone, file number, service, status, `is_new`, `is_follow_up`, the invoice id and the amount due (one query with joins inside the clinic, at most 1,000 rows);
  - the doctor-day rows;
  - the low-stock items (a scan of the clinic's items);
  - the last 10 changes, worked out from the fetched rows.
- Money totals appear only for people with `finances.view`.

### 5.12 Flows across the directory and a shard

Each is a sequence of idempotent steps with its own request key. A failed step is safe to run again.

- **Create a clinic** (platform admin, M1):
  1. directory: insert `directory_clinic` (the id, the least-full shard, status `active`);
  2. directory: create the owner's account if new, with a temporary password expiring in 72 hours and `must_change_password`;
  3. shard: insert `clinics_clinic` (`owned`), the owner's membership, the specialty template's services, medicines and stock items, the counters and the categories;
  4. directory: insert the owner's `user_clinic` row.
  A step that finds its row already there succeeds without changing it. A nightly reconciliation repairs a half-finished run.
- **Add a member with a new account** (team, M3): directory account first, then the shard membership, then `user_clinic`.
  - The account step reuses an account **only if this same request created it**: the account records the creating clinic and request key until its first sign-in. Any other existing username goes through the invitation path, with the same answer, so the step never attaches someone else's account or reveals that a username exists.
  - A replay shows no temporary password, and offers to issue a new one.
- The shard step of "create a clinic" is idempotent by clinic id and claims no shard request key (the clinic row doesn't exist yet); the platform request key in the directory covers the whole flow.
- **Suspend or reactivate a clinic:** directory status first, then the shard's `clinics_clinic.status`. Requests check the shard's status.
- **Transfer ownership, support resets** (platform admin): a `directory_support_action` with a reason, a second admin's approval, an `audit_change` event in the clinic's log, and a notice to the account at its next sign-in. Ownership moves only to an active doctor of the clinic. Django's admin site is off in production.
- Platform admins see clinic names, statuses and usage counts (from `clinic_day`, read as `hamada_maint`), never patient data.

## 6. API

### 6.1 Conventions

- JSON action endpoints under `/api/v1/`. Pages and fragments are HTML (section 7). Browsers use the session cookie and send the CSRF token in a header, read from the cookie at each send. CSRF failures answer JSON (`CSRF_FAILURE_VIEW`), so the client can fetch a fresh token once and resend.
- Every view declares its method, its permission codes and whether it returns clinical data: `@api(method, perm=…, access=…)`. A test over the URL configuration checks that every view returning patient fields declares `access`.
- **Required headers:** `Idempotency-Key` on every mutating request; `X-Membership` on every request.
- **Errors:** `{"error": {"code": "slot_taken", "message": "<in the user's language>", "fields": {…}}}`. Statuses 400, 401, 403, 404, 409 (`status_changed`, `slot_taken`, `day_full`, `over_balance`, `refund_first`, `busy`, `clinic_changed`), 422 (`request_key_reused`), 429 (with `Retry-After`) and 503 (with `Retry-After`). A record of another clinic is 404, never 403.
- **Every answer** carries `Cache-Control: no-store` and `X-App-Version`. Every mutating answer echoes the request key.
- **Lists** take `?limit=` (at most 100) and `?after=<cursor>`, and return `{"items": […], "next": "<cursor>" | null}`. The cursor encodes the sort key and the id.
- **Times** in JSON are ISO 8601 with the clinic's offset (`2026-07-15T20:00:00+03:00`), from `zoneinfo` and the pinned `tzdata`. Days are business days (`YYYY-MM-DD`). Money is integer piastres. Display strings are formatted on the server.
- **Values:** one number parser, on the server and the client, accepts Arabic-Indic and extended Arabic-Indic digits and `٫`, `.` or `,` as the decimal point. Money becomes piastres by string arithmetic.

### 6.2 Endpoints

| Area | Endpoints | Permission | M |
|---|---|---|---|
| Sign-in | `POST auth/login`, `auth/totp`, `auth/logout`; `GET/POST me/totp` (set-up and recovery codes) | public, then signed in | M0 |
| Me | `GET me` (person, memberships, codes, `session_expires_at`, app version), `POST me/clinic`, `me/password`, `me/language` | signed in | M0 |
| Clinic configuration | `GET clinic` (active services and prices, doctors with mode and today's hours, medicines, payment methods, display settings), with an ETag | any member | M1 |
| Today | page `/today`, fragment `/today/queue` (ETag) | `appointments.book` or `.queue` | M1 |
| Appointments | `POST appointments` (with `arrive` and payment); `GET appointments/quote`; `GET appointments/{id}`; `POST appointments/{id}/{arrive, undo-arrive, call, finish, cancel, reschedule}`; `GET doctors/{id}/slots?day=` | `appointments.*`, plus body codes | M1 |
| Patients | `GET patients?q=`, `GET patients/by-phone?phone=`, `POST patients`, `GET patients/{id}` (access), `PATCH patients/{id}`, `GET patients/{id}/visits`, `/payments` (access) | `patients.view`, `.edit` | M1 |
| Billing | `POST appointments/{id}/payments`; `GET invoices/{id}` (access); `POST invoices/{id}/lines`, `DELETE invoices/{id}/lines/{line}`, `PUT invoices/{id}/discount`, `POST invoices/{id}/void`, `POST payments/{id}/refund`; `GET invoices?unpaid` (M3) | `billing.*` | M1 |
| Clinical | `GET/PUT appointments/{id}/note` (access), `POST prescriptions`, `GET prescriptions/{id}` (access), `GET patients/{id}/prescriptions` (access) | `clinical.view`, `.write` | M2 |
| Inventory | `GET inventory/items`, `POST inventory/items`, `PATCH inventory/items/{id}`, `POST inventory/purchases`, `inventory/uses`, `inventory/adjustments`, `GET inventory/movements` | `inventory.*` | M3 |
| Finance | `GET finances?month=`, `GET/POST expenses`, `POST expenses/{id}/void`, `GET/POST expense-categories`, `GET/PUT staff-pay` | `finances.view`, `expenses.manage`, `finances.pay` | M3 |
| Consultations | `GET consults`, `POST consults`, `POST consults/{id}/{accept, decline, start, end, cancel}` | `consults.manage` | M3 |
| Team and settings | `GET team`, `POST team/members`, `PATCH team/members/{id}`, invitations; `PUT settings/clinic`, services, schedules, medicines, specialty | `team.manage`, `settings.manage` | M3 |
| Audit | `GET audit/changes`, `GET audit/access?patient=` | `audit.view` | M3 |
| Platform | `GET/POST platform/clinics`, `POST platform/clinics/{id}/suspend`, `/reactivate`, support actions | platform admin, no patient data | M1 |
| Print | `GET /print/receipts/{invoice}?lang=&format=a5|80mm`, `GET /print/prescriptions/{id}?lang=` | `patients.view` (receipt), `clinical.view` (prescription); access | M1, M2 |
| Health | `GET /healthz` (process), `GET /readyz` (directory and shards) | public | M0 |

## 7. Screens

### 7.1 Approach

- **Server-rendered Django pages** with a few small JavaScript modules. No framework, no HTMX, no build step, no CDN, no web fonts.
- **Modules** (in `web/static/js/`, plain ES modules):
  - `api.js`: sends actions, owns request keys, retries, the sign-in overlay and CSRF;
  - `today.js`: the poller and row patching;
  - `dialog.js`: Book, Pay, Prescription and confirmations as native `<dialog>`s;
  - `numbers.js`: the shared number parser.
- Client code never builds HTML from strings. It inserts only same-origin server fragments, sets text with `textContent`, and sets styles through `el.style`, which the strict CSP allows (style attributes don't work).
- **Static files:** `ManifestStaticFilesStorage` with `support_js_module_import_aggregation = True`, so imports get hashed names; `<link rel="modulepreload">` for the shared modules. `X-App-Version` on every answer: when it changes, Today reloads at a quiet moment (no open dialog, nothing pending). The previous release's files are kept for a day.
- **Browser floor:** Chrome and Edge 109, Safari 15.4. Not used without a fallback: CSS nesting, `AbortSignal.timeout`, `AbortSignal.any`, `Object.groupBy`, import maps.
- The page hides what the person can't do, and the server enforces it anyway.

### 7.2 Language, numbers and right to left

- `<html lang dir>` comes from the user's language. Arabic is the default and right to left. The CSS uses logical properties, so one stylesheet serves both directions.
- **One catalogue:** `locale/ar/LC_MESSAGES/django.po`, compiled with polib (`manage.py compile_translations`), because GNU gettext isn't available on Windows. Strings that are ambiguous in Arabic use `pgettext` (Book the verb and the noun differ). The few strings JavaScript needs come from the page (`data-*` attributes or `json_script`). A test extracts the translatable strings by pattern and fails when Arabic lacks one or keeps an unused one; it replaces `makemessages`.
- Prefer "label: number" (`في الانتظار: 3`) over plural sentences.
- **Formatting on the server:** times in the clinic's time zone; digits from the clinic's setting (Western by default, Arabic-Indic optional); money with our own formatter (Intl's EGP string carries invisible marks).
- **Bidi:** every data value is wrapped in `<bdi>`. Phones, file, invoice and prescription numbers, amounts and times get `dir="ltr"` with `unicode-bidi: isolate`, inputs included. Names, notes and addresses get `dir="auto"`. A page test checks the on-screen order of a phone's digit groups.
- **Number fields** are `type="text"` with the right `inputmode`, parsed by `numbers.js` and again on the server.
- **Type:** 16 px body text, at least 14 px secondary text, line height about 1.6, no letter-spacing on Arabic. Fonts: `system-ui, "Segoe UI", Tahoma, "Noto Sans Arabic", "Geeza Pro", Arial, sans-serif`.

### 7.3 Pages

| Page | What it does | M |
|---|---|---|
| Sign in | Username or phone and password; then the code or a recovery code; a forced password change; code set-up with recovery codes | M0 |
| Clinic picker | For people in several clinics | M0 |
| Today | Per-doctor queues (tabs on small screens): turn, patient, service, status, paid or owed, the next action. "Arrive and collect" for booked rows. Counts, the low-stock banner, recent changes, search, Book | M1 |
| Book | One dialog: find or add the patient (with the duplicate-phone step), doctor, service, a slot for slot doctors, source. The fee and follow-up from `quote`. One button: "Book, collect and print", defaulting to the business day, the open doctor tab, a consultation, "at the clinic" and collect now in cash | M1 |
| Pay | Amount (the balance by default), method, receipt | M1 |
| Patients | Search, then the patient file: details, visits and payments; notes and prescriptions with `clinical.view`; each list paged with "load more" and a count of what is shown | M1, M2 |
| Visit | For the doctor: the note (with version checks), a prescription, materials used, finish | M2 |
| Prescription | Pick medicines, set dose, amount and days with steppers, print in Arabic or English | M2 |
| Inventory, Finances, Consultations, Team, Settings, Audit | As in R4.6–R4.9 | M3 |
| Account | Password, language, code set-up, recovery codes | M0 |
| Platform | Clinics: create, suspend, reactivate, usage counts | M1 |

### 7.4 Today and resilience

**Polling:**
- One poller per browser and membership: it holds a Web Lock named after the membership and shares each answer, and each of the person's own actions, over `BroadcastChannel`. Where locks don't exist (plain http on a LAN), each tab polls on its own.
- The interval comes from `Poll-After` (5 s while visible, 60 s while hidden, by default), with ±20% jitter. Backoff after errors uses full jitter, and `Retry-After` is honoured.
- "Updated HH:MM" in server time, and the queue dims when the last good answer is older than two intervals. It refreshes at once on `visibilitychange`, `online`, `pageshow` and `focus`, and when a timer fires much later than set (a laptop that slept). The first failure after waking doesn't count. A "try now" button.
- A 401 stops polling and shows the sign-in overlay, not "connection lost". A 409 `clinic_changed` reloads the tab.

**Rows:**
- Rows are ordered by turn or slot time only, never by status. They are keyed by id and patched in place, and the row under the pointer or with focus is never replaced.
- Pending actions are kept in a store keyed by appointment and action, so a redraw keeps their buttons disabled.
- Confirmations name the patient ("Called in: Mohamed Ahmed"). Cancel asks for confirmation with the patient's name.

**Saving (the browser half of safe repeats):**
- A form's request key is made when the form opens, and reused for every send until a definite answer: a 2xx, or a 4xx that wrote nothing. **409 `busy` and 503 are never definite**: the same key is resent after `Retry-After`.
- After a timeout or a network error the outcome is unknown. The form locks its fields, says "not confirmed yet, checking", and resends the same key and body with backoff. It offers only "try again" and "discard".
- `{url, key, body, user, membership, created_at}` is kept in `sessionStorage`. After a reload or a new sign-in, it is settled first, only for the same user and membership. A pending request older than 24 hours is never resent: the screen says "outcome unknown, check the record", because its key row may already be gone (4.11). It is cleared at sign-out and when another person signs in. No patient data ever goes into `localStorage`.
- "Saved" shows only for an answer that echoes the key. "Still saving" shows after 3 s; retries stop after about 20 s.
- Retries happen only after network errors, timeouts, 502, 503, 504, 409 `busy`, and 429 after `Retry-After`. Never after a 500.
- Where `crypto.randomUUID` is missing (plain http), keys are built from `crypto.getRandomValues`.

**The sign-in overlay:**
- It sits over the intact page and handles the password, the code and a forced password change. It returns to the same clinic without the picker.
- After sign-in, a stored request is replayed only for the same user and membership. Otherwise it is dropped with a message, and the page reloads for the new person.
- Sign-in and sign-out are broadcast to the other tabs.
- A warning appears 10 minutes before `session_expires_at`, with sign-in in place.

**Drafts:** visit notes and prescription drafts are kept in `sessionStorage` per appointment until saved, with one save in flight per note (M2).

### 7.5 Printing

- Receipts and prescriptions are Django templates, rendered in the language from `?lang=ar|en` (default: the clinic's print language).
- **A5:** `@page { size: A5; margin: 0 }`, with padding inside, so the browser prints no URL or title.
- **80 mm receipts:** content 72 mm wide, black text only, at least 10 pt. The page height is set from the measured content with `insertRule("@page { size: 80mm <h>mm; margin: 0 }")`, which works under the CSP.
- A letterhead setting: no header, with top and bottom offsets in mm, for pre-printed paper.
- The print tab is opened in the tap handler (Safari blocks one opened later), then pointed at the URL when the save returns, with a link as a fallback. It prints after `document.fonts.ready`.
- No patient name in `<title>`: it would be printed and kept in a shared PC's history.
- M1 tests two common thermal printers.

### 7.6 Accessibility

- Every field has a label. Focus is visible and the keyboard works. `/` focuses search, checked with `event.code === "Slash"` and ignored inside fields (on the Arabic layout that key types ظ).
- Tap targets of at least 44 px with 8 px gaps.
- Native `<dialog>` with `showModal()`, returning focus to the button that opened it. Navigation focuses the `h1`.
- Results go to a polite live region (`role="status"`); errors use `role="alert"` and stay until dismissed.
- Every status is shown as text plus an icon, never by colour alone. Contrast meets WCAG AA.

## 8. Security and privacy

- **Tenant isolation, in five layers:**
  1. the clinic comes from the session's membership, re-checked on the shard in every request, with `X-Membership` guarding against another tab's clinic;
  2. every query filters on `clinic_id`, and a test-time check fails any statement whose root tenant table isn't filtered on it;
  3. composite foreign keys stop cross-clinic references;
  4. forced row-level security refuses other clinics' rows;
  5. tests try every endpoint with another clinic's ids, in paths and in bodies.
- **Grants:** the rules in 5.8 stop escalation. Services check codes that depend on the request body.
- **Accounts:** clinic endpoints never change accounts. Platform admins hold no memberships. Support actions need two admins and leave a trail in the clinic's own log (5.12).
- **Passwords:** Django's PBKDF2 hasher, at least 10 characters, the common-password check. Temporary passwords expire in 72 hours.
- **Two-factor codes:** 5.9. Secrets are encrypted; recovery codes are hashed.
- **Sessions:** 12 hours, a new key at sign-in and after the code, `Secure`, `HttpOnly`, `SameSite=Lax`, CSRF on every mutating request.
- **Headers:** a content security policy that allows only this site, HSTS, `nosniff`, frames denied, referrer `same-origin`, and `Cache-Control: no-store` on every API, page and print answer.
- **Logs:** no query strings or bodies in the proxy, Gunicorn or application logs. Sentry, when used, has `send_default_pii=False` and scrubbing.
- **Audit:** `audit_change` records permissions, members, prices, discounts, refunds, voids, stock adjustments, patient identity edits, settings, specialty changes, exports and support actions, with before and after. `audit_access` records every read of clinical data, declared per view (6.1).
- **Before real patients (M5):** a per-membership limit on file opens with an alert; an idle lock on shared desk PCs; sessions that end at the clinic's day end.
- **Data minimisation:** no national ID numbers in v2.
- **Open:** the legal review under Law 151 of 2020 (R5).

## 9. Operations

### 9.1 Deploying and migrating

- `migrate_all` runs as a one-off step, as `hamada_owner`, before the web containers update. Schema changes use expand and contract, so old and new code both work during a deploy.
- **Migrations on large tables:** `lock_timeout` with retries, `CREATE INDEX CONCURRENTLY`, constraints added `NOT VALID` and validated later, backfills in batches in jobs. A stored generated column can't be added cheaply later, so such columns go in a table's first migration.

### 9.2 Availability

- **Every database** (the directory and each shard) runs with high availability: a managed PostgreSQL with HA, or Patroni with a tested promotion.
- **PgBouncer** points at the cluster's write endpoint and reconnects after a promotion. In-flight requests fail and the client resends them with the same keys (7.4), so nothing is done twice.
- **Redis**, when used, is only a speed-up: if it fails, requests fall back to PostgreSQL.
- **Backups:** pgBackRest or WAL-G with continuous WAL archiving to object storage, `archive_timeout` of at most 300 s for the 5-minute RPO, a daily backup, encryption, and a monthly restore drill per database.
- **RTO:** failover within minutes. A restore from backup within 2 hours per shard, which sets the shard size (3.4).

### 9.3 Monitoring

- `/healthz` and `/readyz`.
- Structured logs with the request id, clinic id, shard, user id, path, status, time and query count; no query strings.
- `pg_stat_statements` and a slow-query log (over 200 ms) per database.
- Alerts on error rate, p95, replication lag, disk, partition runway, job queue age, request-key table size, and summary differences found by the recompute.

### 9.4 Connections and roles

- Roles and per-role timeouts as in 4.0. With PgBouncer in transaction mode: `DISABLE_SERVER_SIDE_CURSORS`, no session-level state, and the clinic set per transaction.
- The Procrastinate worker connects directly for LISTEN, or polls.

### 9.5 Runbooks

Restore a database; fail over; migrate across shards; move a clinic; partition upkeep; incidents (slow every screen with `Poll-After`); rotate the field-encryption key and the secret key; support resets and ownership transfer; a PC with a wrong clock (HTTPS fails, so "check the date and time" comes first).

### 9.6 Local runs

`RUN_V2.bat` starts PostgreSQL, runs `migrate_all`, compiles translations, starts the worker, and serves http://127.0.0.1:8002.

## 10. Performance targets and tests

| Request | p95 server time at the target | Statements |
|---|---|---|
| Today, unchanged (304) | 20 ms | 2 directory (session, user) + 3 shard (`set_config`, membership, version) |
| Today, full fragment | 150 ms | 3 shard + version, appointments, doctor-days, doctors, items |
| Book (with arrive and payment) | 200 ms | at most 12 |
| Status change | 100 ms | 5–7 |
| Payment | 150 ms | at most 10 |
| Patient search | 150 ms (300 ms timeout) | 1–2 |
| Patient file | 200 ms | fixed |

**Tests:**
- **T1, isolation:** every endpoint with another clinic's ids, in paths and in bodies (patient, doctor, service, medicine, item) answers 404 and writes nothing. Lists never show other clinics' rows. Clinics live on both test shards.
- **T2, permissions:** each view without its codes is 403; a single grant gives 200; a removal from the template gives 403; the owner always gets 200; the 5.8 grant rules; a platform admin gets 403 or 404 on every tenant endpoint. Generated from the URL configuration.
- **T3, repeats:** for every mutating endpoint, from the URL configuration: the same key twice gives one effect and the same answer; two threads with one key give one effect and two 200s; the same key with another body gives 422; a retry after a 409 runs again.
- **T4, the transition matrix:** every action against every status; repeats leave the counters unchanged.
- **T5, concurrency on PostgreSQL** with real threads:
  - 8 bookings of one slot: one succeeds;
  - 8 bookings for one doctor-day: turns 1 to 8, never repeated;
  - concurrent first payments: no invoice-number gap;
  - payment against cancel: never a paid cancelled visit;
  - crossing reschedules: no deadlock;
  - "book, collect and print" against a finish with payment for the same doctor and day: no deadlock;
  - a payment, a refund and a discount on one invoice;
  - the last unit of stock;
  - the recompute against live writes;
  - one two-factor code on two connections.
- **T6, throttles** across two processes, not threads.
- **T7, money invariants** after random sequences, including cancel after payment, reschedule after prepayment, refunds on another day, and void then re-invoice: `invoice.paid = Σ payments − Σ refunds`; doctor-day sums equal clinic-day sums; summaries equal the recompute.
- **T8, time:** `day_ends_at`, a session crossing midnight, Egypt's clock changes in April and October, a clinic in another time zone.
- **T9, the access log:** every view declared `access` writes exactly one event, and no other view returns patient fields.
- **T10, security:** a session signed out through one process is refused by another; CSP, `no-store`, CSRF on every mutating method, a new session key at sign-in and after the code, a pending or must-change-password session gets 403 elsewhere, the code-guess limit.
- **T11, row-level security:** no setting gives no rows; a reused connection gives no rows; another clinic's id in WHERE gives nothing; an INSERT for another clinic is refused; every shard table with `clinic_id` is forced and has the standard policy.
- **T12, routing and shards:** clinics on two shards; a tenant query without a context raises; a wrong-shard request is refused; every sequence is in its shard's range.
- **T13, the SQL shape:** a test-time `execute_wrapper` fails any statement whose root tenant table isn't filtered on `clinic_id`.
- **T14, query counts:** each hot request runs the same number of statements with 10 rows or 10,000.
- **T15, pages and fragments:** Django test-client tests for every page and fragment, including the bidi order of a phone and the translation catalogue test.
- **T16, browser scripts** (headless Edge): a double tap, a lost answer, a 401 mid-save, the RTL order of a phone, and the A5 and 80 mm page sizes.
- **Plans** (M1): the hot queries for a 100,000-file clinic, as the web role, under RLS.
- **Synthetic data and load** (M5): `generate_load_data` builds rows the way the app inserts them (indexes grown, not bulk-built), and the load test states its pass criteria at stage-1 size: p95 and error rate for 304s and for the write mix.
- A failing test comes first for every stock, money, permission and status bug (CLAUDE.md).

## 11. Build plan

| Milestone | Contents |
|---|---|
| M0, the base | Directory and two shards locally; the router and context; per-shard id ranges; `migrate_all`; RLS helpers and the policy test; `@api` and the page decorator with one transaction per request, `set_config`, request keys, `X-Membership`, `Ctx`; the business-day helper; accounts (sign-in, throttles in PostgreSQL, two-factor codes with encrypted secrets and recovery codes, forced password change); clinics (memberships, permissions and grant rules, services, schedules, counters, settings, specialty templates in code); audit tables and partitions; the page shell, `api.js`, the sign-in overlay, the clinic picker, the catalogue and its test; `seed_demo`; tests T1–T3, T10–T13 for what exists |
| M1, the front desk | Patients (search, by-phone, add, edit, file with visits and payments); booking with turns, slots, follow-ups, `arrive` and payment; status changes, cancel and reschedule; Today with the fragment, the ETag and the poller; invoices, lines, payments, refunds, discounts, voids; receipts; the clinic configuration read and `quote`; the platform admin; the end-of-day sweep and the recompute; tests T4–T9, T14–T16 and the plan check |
| M2, the doctor | Visit notes with versions and revisions, the medicine list, prescriptions and printing, the clinical tabs of the patient file |
| M3, the back office | Inventory, expenses and finances, staff pay, team and settings screens with invitations, specialty changes, consultations, the audit screens |
| M4 | Reminders (with a per-shard outbox), password reset, clinic sign-up, merging patient files, the day closing, exports, time-zone changes through a job |
| M5, before real patients | Synthetic data and the load test, PgBouncer and HA in compose, the backup and failover drills, moving a clinic between shards, the per-membership read limit, the idle lock, the runbooks |

## 12. Decisions and trade-offs

1. **A modular monolith,** not microservices. A small team can run it, and the clinic boundary is already in the data.
2. **Application-level shards of whole clinics,** not Citus (chief judge D1). Stock PostgreSQL, plain Django relations, and the whole topology testable on the Windows machine. The cost is a router, migrations over every shard, a clinic-move tool and a few cross-database flows.
3. **An `id` primary key from per-shard ranges, with composite foreign keys,** not composite primary keys. Django handles single-column keys far better, and no key ever needs swapping.
4. **Row-level security from M0** (D4). Isolation by the database before go-live, while it is cheapest, with its two traps handled: range predicates instead of `LIKE` for prefixes, and `NULLIF` for pooled connections.
5. **A request-key table with a request hash** (D2), not keys on business rows and not a day-partitioned table. It is bounded (72 hours), covers every mutating request, and refuses a reused key with a different body.
6. **Server-rendered pages** (D3), not a single-page app. One catalogue, server-side formatting, Django tests, fresh assets on every navigation.
7. **Partition the logs, not the business tables** (4.10).
8. **Polling with an ETag,** not WebSockets or server-sent events. It works through any proxy and is cheap per 304. Revisit if updates must arrive in under a second.
9. **Procrastinate in the directory,** not Celery and a broker.
10. **Status counts from the day's rows,** not counters; summaries hold only money, limits and the version.
11. **Authenticator codes for owners,** not SMS. SMS arrives later with password reset.

## 13. Risks

- **A very large clinic** (30+ doctors) changes its version every few seconds at the peak, so its screens fetch full fragments often. Mitigation if needed: per-doctor versions (an added column).
- **Cross-database flows** can stop halfway. Each step is idempotent, and a nightly reconciliation repairs them.
- **Moving a clinic** needs a short freeze. It runs in the early-morning window, and M5 rehearses it.
- **RLS mistakes:** a missing policy or a query that loses its index range. Covered by the policy test and the M1 plan check.
- **Clinic PCs with wrong clocks or old browsers:** the server decides the business day and formats times; the browser floor is stated.
- **The data protection review** could change hosting or consent flows.

## 14. Open questions

The requirements' open questions (R7, including the new ones in R7.8), plus:
1. Hosting: in Egypt, or a nearby region, after the legal review.
2. The retention period for `audit_change` (likely at least 5 years) and for medical records.

## Appendix A: changes required by the review of draft 1

| # | Change | Applied in |
|---|---|---|
| 1 | Application-level shards, directory, router, ownership check, id ranges, `migrate_all`, Procrastinate in the directory | 1, 3.1–3.5, 4.0, 4.1, 5.10, 5.12, 9, 11, 12.2, 13 |
| 2 | Key and index rules: id ranges, `UNIQUE (clinic_id, id)` only where referenced, child and keyset indexes, `doctor_day` key order, the SQL-shape check | 4.0–4.9, 10 (T13) |
| 3 | Row-level security, roles, per-role timeouts, tests | 4.0, 5.0, 8, 9.4, 10 (T11) |
| 4 | Request-key table with a request hash, claimed first, 72 hours, absolute values | 4.11, 5.0, 5.3, 6.1, 12.5 |
| 5 | The browser half of safe repeats | 7.4 |
| 6 | Server-rendered pages, one catalogue with `pgettext` and a test, static file rules, browser floor | 1, 6.1, 7.1, 7.2, 12.6 |
| 7 | Bidi, the shared number parser, the phone-order test | 6.1, 7.2, 10 (T15) |
| 8 | Grant rules, body-dependent codes, `clinical.view`, `finances.pay` | 4.2, 5.1, 5.3, 5.8 |
| 9 | Accounts across clinics, invitations, platform admins without memberships, support actions, the platform admin in M1 | 4.1, 5.8, 5.12, 6.2, 8, 11 |
| 10 | Two-factor codes, throttles in PostgreSQL, sessions, the overlay | 4.1, 5.9, 7.4, 8 |
| 11 | `X-Membership`, `Ctx` with the actor kind, system contexts for jobs | 5.0, 5.10, 6.1 |
| 12 | Access declared per view, no patient data in logs, `no-store`, the read limit before go-live | 6.1, 8, 10 (T9) |
| 13 | `audit_change` and `audit_access`, account events, attach-based partition upkeep | 4.1, 4.10 |
| 14 | The business day | 4.0, 4.2, 5.1, 5.2, 5.10, 5.11, 7.4, 10 (T8); R4.1 |
| 15 | Jobs and connections | 3.3, 5.10, 9.4 |
| 16 | Lock order and the per-flow lock table, reschedule rules, the safe recompute, the fixed upsert | 5.0, 5.1, 5.2, 5.10 |
| 17 | Invoices under the appointment lock, `ensure_invoice`, the open-invoice index, `refund_first`, payment columns, `doctor_day.refunded`, the counters table, absolute discounts, removing a line | 4.4, 4.5, 5.2, 5.3 |
| 18 | `holds_slot`, fillfactor, no index on `updated_at`, `max_bookings`, the schedule-change rule | 4.4, 5.1 |
| 19 | Patient search: covering index, collation, range predicates, escaping, timeout, autovacuum, debounce, duplicate phone, plan check | 4.3, 5.7, 10 |
| 20 | The follow-up rule | 5.1; R4.1, R7.8 |
| 21 | Front-desk actions in one request | 5.1, 5.2, 6.2, 7.3 |
| 22 | Reads the screens need | 5.1, 6.2 |
| 23 | Today and the live queue | 2, 5.11, 7.4, 10 |
| 24 | Printing | 7.5 |
| 25 | Tests | 10 |
| 26 | Visit notes and drafts | 4.6, 5.6, 7.4 |
| 27 | Availability and runbooks | 9.2, 9.5 |
| 28 | The scale model | 2, 3.4 |
| 29 | The requirements map, milestones and questions for Saged | 11, Appendix B; R7.8 |

## Appendix B: requirements map

| R-section | Design | Milestone |
|---|---|---|
| R1 purpose, specialty catalogue | 4.2 (templates with services, medicines and stock) | M0 |
| R2 scale, retention | 2, 3.4, 4.10 | M5 for the load test |
| R3 roles, per-person permissions | 4.2, 5.8 | M0 (engine), M3 (screens) |
| R4.1 setup, follow-up, schedules, business day | 4.2, 5.1 | M0, M1, M3 |
| R4.2 patients, search, duplicate phone | 4.3, 5.7 | M1 |
| R4.3 booking, queue, Today, finish with payment | 4.4, 5.1, 5.2, 5.11, 7.3, 7.4 | M1 |
| R4.4 billing, refunds, discounts, voids, receipts | 4.5, 5.3, 7.5 | M1 |
| R4.5 patient file, notes, prescriptions, access log | 4.6, 5.5, 5.6, 4.10 | M1, M2 |
| R4.6 inventory | 4.7, 5.4 | M3 |
| R4.7 expenses, finances, staff pay | 4.8, 5.3 (net per doctor) | M3 |
| R4.8 consultations | 4.9 | M3 |
| R4.9 team, settings, specialty | 5.8 | M3 |
| R4.10 accounts | 4.1, 5.9 | M0 |
| R4.11 platform admin | 5.12, 6.2 | M1 |
| R4.12 reminders | 3.3 (jobs) | M4 |
| R5 performance | 2, 10 | M1 (plans), M5 (load) |
| R5 availability, backups | 9.2 | M5 |
| R5 isolation by the database | 4.0, 8 | M0 |
| R5 audit and access logs | 4.10, 6.1, 8 | M0, M1, M2 |
| R5 language, devices, time, money | 4.0, 6.1, 7.2 | M0 |
| R5 runbooks | 9.5 | M5 |
| R6 out of scope, R7 open questions | 14; R7 | |

## Appendix C: changes from the check of draft 2

| # | Change | Applied in |
|---|---|---|
| B1 | Booking with payment takes the invoice number before the summary rows; each summary table written in one statement; payment inside a booking only for the current business day; T5 cases | 5.0, 5.1, 10 |
| B2 | 409 `busy` and 503 are never final for a form's key; no resend after 24 hours | 7.4 |
| B3 | The `db` session backend until a shared Redis exists; a two-process sign-out test | 5.9, 10 |
| B4 | Partition DDL as `hamada_owner`; RLS on a partition before it is attached; `hamada_maint` DML-only and never a member of the owner | 4.0, 4.10 |
| B5 | The move clears `owned` on the source first and waits out older transactions; the clinic is checked before the key is claimed; any non-2xx rolls back | 3.4, 5.0 |
| B6 | Adding a member reuses only an account the same request created; otherwise the invitation path; "never change an existing account"; no temporary password on replay | 5.8, 5.12 |
| I1 | `UNIQUE (clinic_id, id)` on payment, item, visit note, category and expense; audit list indexes end with `id` | 4.5–4.8, 4.10 |
| I2 | The counters table; `changed_since_recompute`; one clinic-day per recompute transaction | 4.4, 5.3, 5.10 |
| I3 | `status` on request keys; the endpoints that take no key | 4.11, 5.0 |
| I4 | Refund lock order in the text; the sweep's lock row; expenses in the global order; one statement for two clinic-days | 5.0, 5.3 |

