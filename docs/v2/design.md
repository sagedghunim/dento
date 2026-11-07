# Hamada v2: design

Status: draft 1, 2026-11-06, for review by the senior backend, frontend and database engineers, the judge and the chief judge.
Requirements: `docs/v2/requirements.md`. References like (R4.3) point to its sections.

## 1. Summary

- **One Django application** (a modular monolith) on **PostgreSQL**, plus a background worker. The web tier is stateless, so it scales by adding containers.
- **The tenant is the clinic.** Every tenant row carries `clinic_id`, every index on a tenant table starts with it, and every query filters on it. References between tenant rows are composite foreign keys on `(clinic_id, id)`, so the database itself refuses a row that points into another clinic.
- **The path to 250,000 doctors** has three stages (3.3):
  1. one PostgreSQL server with PgBouncer and a replica;
  2. a bigger server with Redis in front of the hot reads;
  3. Citus, distributing the tenant tables by `clinic_id`.
  The schema rules in 4.0 make stage 3 a migration, not a rewrite.
- **Hot paths are bounded.** Today, booking, status changes, payments and patient search each run a fixed number of indexed queries inside one clinic's rows, whatever the total data size.
- **The live queue uses conditional polling.** Each clinic-day has a version that every queue change bumps in the same transaction. Screens poll with that version and nearly always get `304 Not Modified`.
- **Counters and money summaries** (`clinic_day`, `doctor_day`) change in the same transaction as the work. Finance screens read summaries, never scans of event tables.
- **Concurrency is handled by conditional updates and constraints:**
  - status changes are conditional `UPDATE`s;
  - turn numbers come from an upsert on the doctor-day row;
  - a partial unique index blocks double booking of a slot;
  - stock and invoice balances are checked in the `UPDATE` and by `CHECK` constraints.
- **Safe repeats.** Every create carries a request key, stored on the created row under a unique index. A retry returns the same row. Status changes are idempotent by their target status.
- **The front end** is a single-page app in plain JavaScript modules with no build step, Arabic first (right to left) and English. Print pages are rendered on the server.
- **Health data:** permissions per person checked on the server, an audit log and an access log, two-factor sign-in for owners, a strict content security policy and short sessions.

## 2. Scale model

Assumptions from R2: 250,000 doctors, 15 visits per doctor per working day, 26 working days a month, so about 100M visits a month.

| Table | Rows per month at the target | 2 years at the target | Per visit |
|---|---|---|---|
| appointment (with cancellations and no-shows) | 110M | 2.6B | 1.1 |
| invoice | 100M | 2.4B | 1 |
| invoice_line | 130M | 3.1B | 1.3 |
| payment | 105M | 2.5B | 1.05 |
| patient (new files) | 40M | 1B | 0.4 |
| visit_note | 60M | 1.4B | 0.6 |
| prescription | 50M | 1.2B | 0.5 |
| prescription_item | 125M | 3B | 1.25 |
| inventory_movement | 50M | 1.2B | 0.5 |
| audit_event (mostly file opens) | 200M | 4.8B, then archived | 2 |
| doctor_day | 6.5M | 156M | |
| clinic_day | 4M | 96M | |
| expense | 3M | 72M | |

- **Storage** at the target after 2 years: roughly 6–8 TB with indexes. At 1,000 doctors it is about 1/250 of that: 25–35 GB.
- **Write rate at the peak:**
  - 60% of 3.75M daily visits in 6 evening hours is about 105 visits a second, or 140 at the 20-visit upper bound.
  - About 7 write transactions per visit (book, arrive, call, pay, finish, note, prescription, and stock some of the time) gives about 1,000 write transactions a second. Each has 3–8 statements, all by key.
- **Read rate at the peak:**
  - 100,000 screens polling every 5 s while visible is 20,000 requests a second. At least 95% of them answer 304 from a cached version.
  - About 150 patient searches and 300 file opens a second.
- **What really limits scale:**
  - the size of one database (vacuum, backups, restore time). That is why stage 3 shards by clinic;
  - polling load. That is why the 304 path costs at most one key lookup;
  - queries that grow with the data. They are forbidden: every list is keyset-paged and every lookup starts with `clinic_id`.
  - Hot rows are confined to one clinic, and one clinic writes only a few times a minute.

These numbers are estimates. M5 checks them with a synthetic data set and a load test (10).

## 3. Architecture

### 3.1 Components

| Component | Technology | Role |
|---|---|---|
| web | Django 6.1 on Gunicorn | JSON API, the app shell, print pages |
| worker | Procrastinate (jobs stored in PostgreSQL) | Nightly checks, no-show sweep, partition upkeep, exports and reminders later |
| db | PostgreSQL 18 | The only source of truth |
| pgbouncer | PgBouncer, transaction pooling | Keeps database connections few as web containers grow |
| cache | Redis (optional in development) | Sessions, rate limits, live queue versions, membership cache |
| proxy | Caddy | TLS, static files, compression |

Redis holds nothing that can't be rebuilt. If it's empty or down, requests fall back to PostgreSQL.

### 3.2 Django apps

| App | Owns |
|---|---|
| `core` | API decorator, errors, request keys, the composite foreign key constraint, partitions, text and phone normalisation, money and time helpers, request logging |
| `accounts` | Users, sign-in, two-factor codes, throttling, password changes |
| `clinics` | Clinics, memberships, permissions, services and prices, schedules, specialty templates, per-clinic counters |
| `patients` | Patient files and search |
| `scheduling` | Appointments, turns, slots, status changes |
| `billing` | Invoices, lines, payments, refunds, receipts |
| `clinical` | Visit notes, medicine list, prescriptions |
| `inventory` | Items and the stock ledger |
| `finance` | Expenses, categories, staff pay, the month view |
| `consults` | Online consultation requests |
| `summaries` | `clinic_day` and `doctor_day`. Depends only on `clinics`, so every other app can write to it |
| `audit` | The audit and access log |
| `platform` | Platform admin: create, suspend and list clinics |
| `web` | The app shell template, static files, print templates |

Rules:
- Only `services.py` modules change data. A service is one short transaction. Views validate input, check the permission and call one service.
- Each app owns its tables. Other apps read them through selectors or services.
- Every service takes a `Ctx` (membership, clinic, user, request id, IP) and checks the permission again. The view's check is the first line, not the only one.

### 3.3 Stages of growth

| Stage | Doctors (estimate) | Database | Web | Change needed |
|---|---|---|---|---|
| 1 | up to about 10k | One PostgreSQL 18 (8 vCPU, 32 GB, NVMe), PgBouncer, one replica for backups and reports | 2–4 containers | None |
| 2 | about 10k–40k | 32 vCPU, 128–256 GB, two replicas; Redis required | 10–20 containers | Turn on the Redis paths (sessions, versions, membership cache) |
| 3 | 40k to 250k and beyond | Citus: tenant tables distributed by `clinic_id` on 8–16 workers; global tables as reference tables | 40+ containers | Swap each tenant table's primary key to `(clinic_id, id)`, a quick change because that unique index already exists; distribute the tables; rebalance online |

The rules that keep stage 3 possible are in 4.0. The M5 milestone rehearses the move on a copy.

### 3.4 Deployment

- One `docker compose` file with the components in 3.1. Configuration comes from environment variables.
- HTTPS everywhere. The proxy terminates TLS.
- **Open:** where to host it. The data protection review decides whether the data must stay in Egypt (R5).
- Local development: the portable PostgreSQL 18 in `C:\work\pgsql` on port 5433, shared with the warehouse project, with its own `hamada` database and role. The app runs on port 8002.

## 4. Data model

### 4.0 Rules

- Every table has a `bigint` identity primary key named `id`.
- **Tenant tables** (everything but the global tables in 4.1):
  - `clinic_id bigint NOT NULL` references `clinics_clinic`;
  - `UNIQUE (clinic_id, id)`, which is the target of composite foreign keys and later the Citus primary key;
  - every other index starts with `clinic_id`;
  - a reference to another tenant row is a composite foreign key, e.g. `FOREIGN KEY (clinic_id, patient_id) REFERENCES patients_patient (clinic_id, id)`.
- **Django specifics.** Tenant foreign keys are declared as `ForeignKey(..., db_constraint=False, db_index=False)`. The real constraint is a `CompositeForeignKey` in `Meta.constraints` (`core/constraints.py`), which the migration autodetector creates and drops like any other constraint. All indexes are declared explicitly.
- **No query joins two clinics.** Platform-wide numbers come from summary tables.
- **Money** is `bigint` piastres with a `CHECK` on its sign.
- **Times** are `timestamptz` in UTC. Where the business works by day (appointments, summaries, expenses, payments), a `day date` column holds the date in the clinic's time zone. The server computes it; the browser's clock is never trusted.
- **Statuses** are text with a `CHECK` that lists the allowed values. Only services change them.
- **Request keys.** A row created by a browser action has `request_key uuid NULL`, with `UNIQUE (clinic_id, request_key) WHERE request_key IS NOT NULL`.
- **Nothing clinical or financial is deleted.** Cancellations, voids and refunds keep the row. Clinics are suspended, not deleted.
- Business rows have `created_at` and `created_by_id` (a membership). Rows that change have `updated_at`.
- Text columns have length limits.

### 4.1 Global tables

These are small. In stage 3 they become Citus reference tables.

- `accounts_user`:
  - username (unique, lower case), phone (unique, nullable, E.164), full_name, password, language (`ar` | `en`);
  - is_active, is_platform_admin, must_change_password;
  - totp_secret, totp_confirmed_at.
- `clinics_clinic`: name, city, address, phone, timezone, specialty_code, status (`active` | `suspended`), settings (`jsonb`, validated by the settings registry in `clinics/settings.py`), created_at.
- `clinics_membership`:
  - clinic_id, user_id, role (`owner` | `doctor` | `assistant`), display_name, title, is_active;
  - granted `text[]`, revoked `text[]` (4.2, 5.8);
  - `UNIQUE (clinic_id, user_id)`, `UNIQUE (clinic_id, id)`, `UNIQUE (clinic_id) WHERE role = 'owner'` (one owner), an index on (user_id) for "my clinics".
- `clinics_specialty`: code, names in Arabic and English, templates for services and medicines (`jsonb`).
- `accounts_login_event`: partitioned by month (4.10). user_id, at, ip, outcome.

### 4.2 Clinic configuration (tenant)

- `clinics_service`: name, kind (`consultation` | `follow_up` | `procedure`), price, duration_min, is_active, sort. `UNIQUE (clinic_id, name)`.
- `clinics_schedule`: doctor_id, weekday, starts, ends, mode (`queue` | `slots`), slot_minutes, max_bookings. `CHECK (ends > starts)`, slot_minutes between 5 and 120 when mode is `slots`.
- `clinics_counter`: name (`patient_file`, `invoice`, `prescription`), value. `UNIQUE (clinic_id, name)`.
  - A number is taken with `INSERT … ON CONFLICT (clinic_id, name) DO UPDATE SET value = clinics_counter.value + 1 RETURNING value` inside the transaction that uses it. A rollback gives the number back, so numbers have no gaps.
- **Permissions** are codes in `clinics/permissions.py`. Role templates are fixed in code. The effective set is `(template[role] ∪ granted) − revoked`, and the owner always has everything. The codes:

| Code | Lets a person |
|---|---|
| `appointments.book` | Book, reschedule and cancel |
| `appointments.queue` | Mark arrived, call in, finish, no-show |
| `patients.view` | Search and open patient files |
| `patients.edit` | Create and edit patient files |
| `clinical.write` | Write visit notes and prescriptions |
| `billing.collect` | Take payments, add lines |
| `billing.discount` | Give discounts |
| `billing.refund` | Refund payments and void invoices (sensitive) |
| `inventory.use` | Record stock use |
| `inventory.manage` | Add items, record purchases, adjust after a count |
| `finances.view` | See the month's money and staff pay |
| `expenses.manage` | Add and void expenses |
| `consults.manage` | Handle online consultation requests |
| `settings.manage` | Clinic profile, services, prices, schedules, medicines |
| `team.manage` | Members and permissions (sensitive, only the owner grants it) |
| `audit.view` | Read the audit and access logs |

| Role template | Codes |
|---|---|
| owner | All |
| doctor | appointments.*, patients.*, clinical.write, billing.collect, inventory.use, consults.manage |
| assistant | appointments.*, patients.*, billing.collect, inventory.use |

### 4.3 Patients

- `patients_patient`:
  - file_no (from the counter), full_name, name_norm (normalised, 5.7), phone, phone_norm (digits only, national form);
  - sex (`m` | `f` | empty), birth_date (nullable), birth_date_estimated (when only an age was given), address, notes, allergies, chronic_conditions;
  - created_at, created_by_id, updated_at, request_key.
  - `UNIQUE (clinic_id, id)`, `UNIQUE (clinic_id, file_no)`.
  - Index (clinic_id, name_norm) INCLUDE (full_name, phone, file_no), for prefix search and the bounded "contains" scan (5.7).
  - Index (clinic_id, phone_norm), for phone search and duplicate hints.
- The database cluster uses the C collation, so a plain B-tree serves `LIKE 'prefix%'`.

### 4.4 Scheduling and summaries

- `scheduling_appointment`:
  - patient_id, doctor_id (a membership), service_id, day, queue_no, starts_at and ends_at (only for slot bookings), is_extra;
  - status (`booked` | `waiting` | `in_room` | `done` | `cancelled` | `no_show`), source (`clinic` | `phone` | `online` | `whatsapp`);
  - fee (the price when booked), is_follow_up, note;
  - arrived_at, called_at, finished_at, cancelled_at, cancel_reason;
  - request_key, created_at, created_by_id, updated_at, updated_by_id.
  - CHECKs:
    - status and source are in their lists; fee >= 0;
    - starts_at and ends_at are both null or both set, and ends_at > starts_at;
    - arrived_at is set for `waiting`, `in_room` and `done`; called_at for `in_room` and `done`; finished_at for `done`; cancelled_at for `cancelled`.
  - `UNIQUE (clinic_id, day, doctor_id, queue_no)`. This also serves Today.
  - `UNIQUE (clinic_id, doctor_id, starts_at) WHERE starts_at IS NOT NULL AND NOT is_extra AND status NOT IN ('cancelled', 'no_show')`. One booking per slot.
  - Index (clinic_id, patient_id, day DESC, id DESC), for the patient's history.
  - Composite foreign keys to patient, membership and service.
- `summaries_doctor_day`: doctor_id, day, last_queue_no, booked, arrived, done, cancelled, no_show, collected. `UNIQUE (clinic_id, doctor_id, day)`. Every counter >= 0.
- `summaries_clinic_day`: day, version, booked, arrived, done, cancelled, no_show, collected, refunded, expenses. `UNIQUE (clinic_id, day)`. Every counter >= 0.
  - `version` goes up with every change that Today shows. It is the queue's ETag (5.11).

### 4.5 Billing

- `billing_invoice`:
  - number (from the counter), patient_id, appointment_id (nullable), status (`open` | `void`);
  - subtotal, discount, total, paid;
  - created_at, created_by_id, voided_at, voided_by_id.
  - CHECKs: `0 <= discount <= subtotal`, `total = subtotal - discount`, `0 <= paid <= total`, and a void invoice has paid = 0.
  - `UNIQUE (clinic_id, number)`; `UNIQUE (clinic_id, appointment_id) WHERE appointment_id IS NOT NULL`, so a visit has at most one invoice.
  - Index (clinic_id, patient_id, created_at DESC). A partial index for unpaid invoices: (clinic_id, created_at) WHERE status = 'open' AND paid < total.
- `billing_invoice_line`: invoice_id, kind (`service` | `procedure` | `material` | `other`), service_id or item_id (nullable), description, qty, unit_price, amount. `CHECK (amount = qty * unit_price AND qty > 0)`. Index (clinic_id, invoice_id).
- `billing_payment`:
  - invoice_id, kind (`payment` | `refund`), method (`cash` | `card` | `wallet` | `instapay` | `other`), amount, refunded, refund_of_id;
  - received_at, day, received_by_id, note, request_key.
  - CHECKs: `amount > 0`, `0 <= refunded <= amount`, and only a payment row can have refunded > 0.
  - Index (clinic_id, invoice_id); index (clinic_id, day, received_by_id), for the day closing.

### 4.6 Clinical

- `clinical_visit_note`: appointment_id, patient_id, doctor_id, complaint, diagnosis, notes, created_at, updated_at, updated_by_id. `UNIQUE (clinic_id, appointment_id)`. Index (clinic_id, patient_id, created_at DESC).
- `clinical_medicine`: name, form, default_dose, is_active. `UNIQUE (clinic_id, name)`.
- `clinical_prescription`: number, patient_id, doctor_id, appointment_id (nullable), notes, created_at, request_key. `UNIQUE (clinic_id, number)`. Index (clinic_id, patient_id, created_at DESC, id DESC).
- `clinical_prescription_item`: prescription_id, position, medicine_name, form, dose, quantity, days, instructions. Names and doses are copies, so a printed prescription never changes. No service updates these rows.

### 4.7 Inventory

- `inventory_item`: name, unit, pack, quantity, threshold, unit_cost, is_active. `UNIQUE (clinic_id, name)`. `CHECK (quantity >= 0 AND threshold >= 0 AND unit_cost >= 0)`.
  - Partial index (clinic_id, name) WHERE is_active AND quantity <= threshold, for the low-stock banner.
- `inventory_movement`: item_id, kind (`purchase` | `use` | `adjust`), qty (signed), balance_after, unit_cost, patient_id, appointment_id, expense_id, reason, created_by_id, at, request_key.
  - Index (clinic_id, item_id, at DESC); index (clinic_id, at DESC) WHERE kind = 'use', for the usage report.
  - Not partitioned: it's a ledger that is kept, and stage 3 shards it by clinic.

### 4.8 Finance

- `finance_category`: name, is_system. `UNIQUE (clinic_id, name)`. Each new clinic gets rent, salaries, stock, utilities and other.
- `finance_expense`: category_id, name, amount, day, created_at, created_by_id, voided_at, voided_by_id, request_key. `CHECK (amount > 0)`. Index (clinic_id, day DESC, id DESC).
- `finance_staff_pay`: membership_id (nullable), name, title, kind (`fixed` | `percent`), amount, percent. `CHECK (percent BETWEEN 0 AND 100)`.

### 4.9 Online consultations

- `consults_request`: patient_id, doctor_id (nullable), reason, scheduled_at, status (`new` | `accepted` | `in_call` | `done` | `declined` | `cancelled`), the time of each change, request_key. Index (clinic_id, status, scheduled_at).

### 4.10 Logs (partitioned)

- `audit_event`, partitioned by month on `at`, primary key (id, at):
  - clinic_id, user_id, membership_id, kind (`change` | `access`), action, target_type, target_id, details (`jsonb`, before and after for changes), ip, request_id.
  - Index (clinic_id, at DESC); index (clinic_id, target_type, target_id, at DESC), for "who opened this file".
  - 24 months online. Older partitions are detached, dumped to archive storage and dropped (11.4).
- `accounts_login_event`, partitioned by month, kept 12 months.
- Partitions are built by hand with `RunSQL`, and a daily job keeps 3 months ahead (`core/partitions.py`, reused from the warehouse project).

**Why the business tables aren't partitioned by time.** Appointments, invoices, payments and the stock ledger are long-lived records that other rows refer to. Partitioning them by time would force the partition column into every foreign key and unique key. They are grown out by sharding on `clinic_id` instead (stage 3). Only the logs, which nothing refers to and which age out, are partitioned.

## 5. Core flows

### 5.0 General rules

- One service, one short transaction. Nothing slow or external runs inside it. Jobs are queued in the same transaction (Procrastinate stores them in the same database, so a rolled-back transaction never leaves a job behind).
- **Lock order.** A transaction locks, in this order:
  1. the entity it acts on (appointment, invoice, item, consultation);
  2. a counter row (`clinics_counter`);
  3. `summaries_doctor_day`;
  4. `summaries_clinic_day`.
  Summaries are always last and the doctor's row comes before the clinic's, so two flows can't wait on each other in a cycle.
- **Timeouts.** Web requests run with `lock_timeout = 2s` and `statement_timeout = 5s`. Jobs and migrations raise them per session.
- **Errors.** Services raise `ApiError(code, message, status)`. Constraint violations that users can cause (a taken slot, a duplicate name) map to 409 with a code. Anything else is a 500 and is logged with the request id.
- **Repeats.** A create first looks up its request key. If the row exists, it returns it. If two copies race, the unique index stops the second, which rolls back and returns the first.

### 5.1 Booking

1. Validate the doctor (an active owner or doctor of this clinic), the service (active, this clinic), the day (today up to a year ahead), and for slot doctors a time on that weekday's grid.
2. If the request key already has an appointment, return it.
3. Patient: an existing id in this clinic, or a new file with a number from the counter.
4. Fee: the service's price. A consultation for a patient whose last finished consultation with the same doctor was within the follow-up window (a clinic setting, default 14 days) uses the follow-up price, and is_follow_up is set. That is one indexed query on the patient's history.
5. Turn number: `INSERT INTO summaries_doctor_day … ON CONFLICT (clinic_id, doctor_id, day) DO UPDATE SET last_queue_no = last_queue_no + 1, booked = booked + 1 RETURNING last_queue_no`.
6. Insert the appointment. A taken slot violates the slot index; the whole transaction rolls back, the turn number included, and the answer is 409 `slot_taken`.
7. `clinic_day`: version + 1, booked + 1.

Seven statements at most.

### 5.2 Status changes

| Action | From | To | Sets |
|---|---|---|---|
| arrive | booked | waiting | arrived_at |
| undo_arrive | waiting | booked | clears arrived_at |
| call | waiting | in_room | called_at |
| finish | in_room | done | finished_at |
| cancel | booked | cancelled | cancelled_at, cancel_reason |
| no_show | booked | no_show | (the day has passed, or the doctor's session ended) |

- `UPDATE … SET status = :to, … WHERE clinic_id = :c AND id = :id AND status = :from RETURNING …`.
  - One row: update the doctor-day and clinic-day counters, and bump the version.
  - No row: read the appointment. Missing is 404. Already in the target status is 200, so a repeat is harmless. Anything else is 409 `status_changed`, with the current status.
- `arrive` is allowed only on the appointment's day.
- A nightly job marks the previous day's `booked` appointments as `no_show`, clinic by clinic in batches.
- Reschedule (`booked` only) is a conditional update on the status and the old values. It takes a new turn number for the new doctor-day and moves the counters between the two summary rows.

### 5.3 Payments

- In Egypt the fee is usually collected when the patient arrives. So a payment is allowed in any status except `cancelled`, and the invoice is created at the first payment, or at finish, whichever comes first.
- `POST /appointments/{id}/payments` with an amount, a method and a request key:
  1. If the request key already has a payment, return it.
  2. Get or create the invoice: `INSERT … ON CONFLICT (clinic_id, appointment_id) DO NOTHING`, then read it. A new invoice gets a number from the counter and one line, the service at the appointment's fee.
  3. `UPDATE billing_invoice SET paid = paid + :a WHERE clinic_id = :c AND id = :i AND status = 'open' AND paid + :a <= total`. No row is 409 `over_balance`.
  4. Insert the payment.
  5. `doctor_day.collected` and `clinic_day.collected` go up; the version is bumped.
- **Refund** (`billing.refund`): `UPDATE billing_payment SET refunded = refunded + :a WHERE … AND refunded + :a <= amount`, insert a refund row, take the amount off `invoice.paid`, update the summaries, and write an audit event.
- **Discount** (`billing.discount`): a conditional update that keeps `paid <= total`, with an audit event.
- **Void** (`billing.refund`): only while paid = 0, with an audit event.
- Revenue is counted when cash comes in, by `payment.day`.

### 5.4 Stock

- **Purchase:** `UPDATE inventory_item SET quantity = quantity + :q, unit_cost = :c`, then an expense (category stock, with the request key), a `purchase` movement with balance_after, and `clinic_day.expenses`.
- **Use:** `UPDATE inventory_item SET quantity = quantity - :q WHERE clinic_id = :c AND id = :i AND quantity >= :q RETURNING quantity, unit_cost, threshold`. No row is 409 with the quantity left. Then a `use` movement with the request key and the cost at that moment. The `CHECK` stops a negative quantity even if a service forgets the condition.
- **Adjust after a count** (`inventory.manage`): `UPDATE … SET quantity = :counted WHERE … AND quantity = :expected`, so a change made meanwhile isn't overwritten. Then an `adjust` movement with the difference and the reason, and an audit event.

### 5.5 Prescriptions

- 1 to 30 items. Each is a medicine from the list (copied) or typed text, with the dose, amount (1–200), days (1–365) and instructions.
- The number comes from the counter. One insert for the prescription and one bulk insert for the items.
- The print page (`/print/prescriptions/<id>`) is rendered on the server on A5, and writes an `access` audit event.
- "Repeat the last one" copies the patient's most recent prescription into a new draft in the browser.

### 5.6 Visit notes

- One note per appointment, written with `INSERT … ON CONFLICT (clinic_id, appointment_id) DO UPDATE`. The last writer wins, and `updated_by` and `updated_at` record who.

### 5.7 Patient search

- Normalising, in Python, for both stored names and queries: Arabic-Indic digits become 0–9; alef forms become ا, ة becomes ه, ى becomes ي; diacritics and tatweel are removed; text is lower-cased and spaces are collapsed.
- A query of digits looks up the phone (prefix of phone_norm) or the file number (exact).
- A name query:
  1. prefix: `name_norm LIKE :q || '%'`, limit 20;
  2. if that found fewer than 20, a word prefix: `name_norm LIKE '% ' || :q || '%'`, filling up to 20.
  - The second query scans only this clinic's part of the (clinic_id, name_norm) index, as an index-only scan. That is bounded by the clinic's size: about 20 ms for 100,000 files. It never touches other clinics' rows.
- Results show the name, phone, file number and the last visit day.

### 5.8 Team and permissions

- Only `team.manage` changes members or permissions. Only the owner can grant `team.manage`. Nobody can change their own permissions. The owner can't be removed or demoted; transferring ownership is a platform admin action.
- Adding a member: a new account (temporary password, must change it at first sign-in) or an existing username. The membership is `UNIQUE (clinic_id, user_id)`.
- Each change writes an audit event with the before and after.
- Permissions are read from the membership row on every request, so a change applies at once.

### 5.9 Signing in

1. `POST /api/v1/auth/login`. Throttled per IP (30 in 5 minutes) and per username (8 in 5 minutes). One generic error for a wrong username or password.
2. If the user has two-factor sign-in (required for owners and platform admins), the session holds a pending user until `POST /api/v1/auth/totp` gets a valid code (±1 step, each code used once). An owner who hasn't set it up yet must set it up before anything else.
3. With one active membership, that clinic becomes the active one. With several, the app shows the clinic picker (`POST /api/v1/me/clinic`).
4. The session lasts 12 hours. Its key changes at sign-in. A password change signs out the other sessions.
- Every request reloads the membership (one query). An inactive membership or a suspended clinic answers 403 with a code, and the app returns to the picker.

### 5.10 Summaries and nightly checks

- `doctor_day` and `clinic_day` change in the same transactions as the work (5.1–5.4).
- A nightly job recomputes yesterday's counters for every clinic, in batches, from appointments, payments and expenses. A difference is fixed, logged and counted in a metric.
- Other jobs: the no-show sweep, partition upkeep, expired sessions.

### 5.11 The live queue

- `GET /api/v1/today?day=YYYY-MM-DD` returns `ETag: "<clinic_id>.<day>.<version>"`.
- With `If-None-Match`, the server reads only the version and answers 304 when it hasn't changed. In stage 2 the version is read from Redis, which is set after each commit, and PostgreSQL is the fallback.
- A full answer: the doctors working that day, the day's appointments with patient, service and payment status (one query with joins inside the clinic, at most 1,000 rows), the doctor-day counters, the low-stock items, and the last 10 changes. The changes are worked out from the appointments' `updated_at`, so there is no activity table.

## 6. API

### 6.1 Conventions

- JSON under `/api/v1/`. Browsers use the session cookie and send the CSRF token in a header. Token auth for mobile apps comes later.
- Every view declares its method and permission in `@api(method, perm=…)`. The decorator checks the method, the session, the active membership, the clinic's status and the permission, parses JSON (64 KB limit), runs the view, and maps errors.
- Errors: `{"error": {"code": "slot_taken", "message": "<in the user's language>", "fields": {…}}}` with 400, 401, 403, 404, 409, 422 or 429. A record from another clinic is 404, never 403.
- A create takes `request_key` (a UUID) in the body. A retry with the same key returns the same result.
- Lists take `?limit=` (at most 100) and `?after=<cursor>`, and return `{"items": […], "next": "<cursor>" | null}`. The cursor encodes the sort key and the id.
- Times are ISO 8601 in UTC; `/api/v1/me` gives the clinic's time zone, and the browser formats with it. Days are `YYYY-MM-DD` in the clinic's zone. Money is integer piastres.

### 6.2 Endpoints

| Area | Endpoints | Permission |
|---|---|---|
| Sign-in | `POST auth/login`, `POST auth/totp`, `POST auth/logout`, `GET/POST me/totp` (set-up) | Public, then signed in |
| Me | `GET me`, `POST me/clinic`, `POST me/password`, `POST me/language` | Signed in |
| Today | `GET today?day=` (ETag) | `appointments.queue` or `appointments.book` |
| Appointments | `POST appointments`; `POST appointments/{id}/{arrive, undo-arrive, call, finish, cancel, no-show, reschedule}`; `GET doctors/{id}/slots?day=` | `appointments.book` or `.queue` per action |
| Patients | `GET patients?q=`, `POST patients`, `GET patients/{id}` (writes an access event), `PATCH patients/{id}`, `GET patients/{id}/visits`, `/prescriptions`, `/payments` | `patients.view`, `patients.edit` |
| Billing | `POST appointments/{id}/payments`, `GET invoices/{id}`, `POST invoices/{id}/lines`, `POST invoices/{id}/discount`, `POST invoices/{id}/void`, `POST payments/{id}/refund` | `billing.*` |
| Clinical | `GET/PUT appointments/{id}/note`, `POST prescriptions`, `GET prescriptions/{id}` | `clinical.write`, `patients.view` |
| Inventory | `GET inventory/items`, `POST inventory/items`, `POST inventory/purchases`, `POST inventory/uses`, `POST inventory/adjustments`, `GET inventory/movements` | `inventory.*` |
| Finance | `GET finances?month=`, `GET/POST expenses`, `POST expenses/{id}/void`, `GET/POST staff-pay` | `finances.view`, `expenses.manage` |
| Consultations | `GET consults`, `POST consults`, `POST consults/{id}/{accept, decline, start, end}` | `consults.manage` |
| Team and settings | `GET team`, `POST team/members`, `PATCH team/members/{id}`, `GET/PUT settings/clinic`, services, schedules, medicines | `team.manage`, `settings.manage` |
| Audit | `GET audit?target=` | `audit.view` |
| Platform | `GET/POST platform/clinics`, `POST platform/clinics/{id}/suspend` | Platform admin only, no patient data |
| Print | `GET /print/prescriptions/{id}`, `GET /print/receipts/{invoice_id}` (HTML) | `patients.view` |
| Health | `GET /healthz` (process), `GET /readyz` (database) | Public |

## 7. Screens

### 7.1 Approach

- One page shell (`web/templates/web/app.html`) and ES modules in `web/static/js/`. No framework, no build step, nothing loaded from a CDN, no web fonts (a system font stack that covers Arabic).
- A hash router. Each page module exports `mount(root, params)` and returns a cleanup function that stops its polling.
- `api.js`: every request has a 10 s timeout. A create gets a request key from `crypto.randomUUID()` and is retried up to 3 times with the same key after a network error, a timeout or a 5xx, never after a 4xx. 401 shows the sign-in overlay.
- All rendering goes through a small `h()` helper that escapes text. No data is ever put into `innerHTML`.
- The UI hides what the person can't do (from `GET me`), and the server enforces it anyway.

### 7.2 Language and layout

- `<html lang dir>` comes from the user's language. Arabic is the default and right to left. The CSS uses logical properties (`margin-inline-start` and so on), so one stylesheet serves both directions.
- UI strings are in `web/static/js/i18n/ar.js` and `en.js`, keyed by the English text. Server messages (errors, print pages) use Django's translations in `locale/ar/LC_MESSAGES/django.po`, compiled with polib as in the warehouse project.
- Numbers, money, dates and times are formatted with `Intl` in the clinic's time zone. **Assumption:** Western digits by default, Arabic-Indic digits as a clinic setting.
- Layout: a side menu on wide screens and a bottom bar on phones. The top bar shows the clinic, the date, the person and the language switch.

### 7.3 Pages

| Page | What it does |
|---|---|
| Sign in | Username or phone, password, then the code if needed. First sign-in forces a new password |
| Clinic picker | For people in several clinics |
| Today | Per-doctor queues (tabs on small screens): turn, patient, service, status, paid or owed, and the next action. Counts, the low-stock banner, recent changes, a search box, and Book |
| Book | A dialog: find or add the patient, doctor, service, day, a slot for slot doctors, source; shows the fee and the follow-up price when it applies |
| Visit | For the doctor: the note, a prescription, materials used, and finish |
| Patients | Search, then the patient file: details, visits, prescriptions, payments, each paged |
| Prescription | Pick medicines, set dose, amount and days with steppers, print |
| Pay | Amount (the balance by default), method, receipt |
| Inventory | Items with levels, purchase, use, adjust, usage report |
| Finances | The month: collected, refunds, expenses by category, what is left, revenue per doctor, staff pay; expenses list and add |
| Consultations | Requests by status with their actions |
| Team | Members, roles, single permissions per person |
| Settings | Clinic profile, services and prices, schedules, medicines |
| Account | Password, language, two-factor set-up |

### 7.4 Live updates and resilience

- Today polls every 5 s while the tab is visible and every 60 s while hidden, with `If-None-Match`. After an error it backs off up to 60 s. After two failed polls it shows "connection lost", and it refreshes at once when the connection returns. After the person's own action, it refreshes immediately.
- While an action is saving, its button is disabled and shows that it is saving. If all retries fail, the form keeps what was typed and says it wasn't saved.

### 7.5 Printing

- Receipts and prescriptions are Django templates with `@page { size: A5 }`, plus an 80 mm receipt layout (`?format=80mm`). They open in a new tab and print. The clinic's header, the doctor, the patient's name and file number, and the date all come from the server, in the clinic's language.

### 7.6 Accessibility

- Every field has a label, focus is visible, and the keyboard works (`/` focuses search). Contrast meets WCAG AA. A status is shown as text, not only as a colour.

## 8. Security and privacy

- **Tenant isolation, in four layers:**
  1. the clinic comes from the session's membership, reloaded on every request;
  2. every query filters on `clinic_id`;
  3. composite foreign keys stop cross-clinic references in the database;
  4. tests try every endpoint with another clinic's ids (10).
  Row-level security in PostgreSQL is a planned fifth layer for stage 2.
- **Passwords:** Django's PBKDF2 hasher, at least 10 characters, the common-password check. Sign-in is throttled (5.9).
- **Two-factor codes (TOTP)** are required for owners and platform admins, optional for others. A clinic setting can require them for everyone.
- **Sessions:** 12 hours, rotated at sign-in, `Secure`, `HttpOnly`, `SameSite=Lax`. CSRF on every POST.
- **Headers:** a content security policy that allows only this site, HSTS, `nosniff`, frames denied, referrer `same-origin`.
- **Audit:** changes to permissions, members, prices, discounts, refunds, voids, stock adjustments, patient identity fields, settings and exports, with before and after. Access events for opening, printing or exporting a patient file.
- **Platform admins** have no endpoint that returns patient data.
- **Data minimisation:** no national ID numbers in v2.
- **Open:** the legal review under Law 151 of 2020 (R5).

## 9. Operations

- **Deploy.** Migrations run as a one-off step before the web containers update. Schema changes use expand and contract, so the old and new code both work during a deploy.
- **Backups.** pgBackRest or WAL-G with continuous WAL archiving to object storage, a daily backup, encryption, and a monthly restore drill. RPO 5 minutes.
- **Monitoring:**
  - `/healthz` and `/readyz`;
  - structured logs with the request id, clinic id, user id, path, status, time and query count;
  - Sentry when `SENTRY_DSN` is set;
  - `pg_stat_statements` and a slow-query log (over 200 ms);
  - alerts on error rate, p95, replica lag, disk, partition runway and job queue age.
- **Migrations:** `lock_timeout` with retries, `CREATE INDEX CONCURRENTLY` on big tables, constraints added `NOT VALID` and validated later, backfills in batches in jobs.
- **Connections:** stage 1 can use psycopg's pool inside each process. With PgBouncer in transaction mode, set `DISABLE_SERVER_SIDE_CURSORS` and keep no session-level state.
- **Jobs:** Procrastinate queues `default` and `maintenance`. Every job is idempotent and works one clinic at a time.
- **Local runs:** `RUN_V2.bat` starts PostgreSQL, migrates, compiles translations, starts the worker and serves http://127.0.0.1:8002.

## 10. Performance targets and tests

| Endpoint | p95 server time at the target | Queries |
|---|---|---|
| Today, unchanged (304) | 20 ms | 1 in stage 1 (the version), 0 database queries with Redis, plus the session and membership |
| Today, full | 150 ms | Fixed: the version, appointments, doctor-days, doctors, low stock |
| Book | 200 ms | At most 7 statements |
| Status change | 100 ms | 3–4 |
| Payment | 150 ms | At most 8 |
| Patient search | 150 ms | 1–2 |
| Patient file | 200 ms | Fixed |

Tests:
- A unit test for every rule.
- **Isolation:** every endpoint is called with another clinic's ids and must answer 404, and no list ever shows another clinic's rows. The list of endpoints comes from the URL configuration, so a new endpoint can't be forgotten.
- **Permissions:** every endpoint is called without its permission and must answer 403.
- **Concurrency**, on PostgreSQL with real threads:
  - 8 people booking the same slot: one succeeds;
  - 8 bookings for one doctor-day: turns 1 to 8, with no gaps and no repeats;
  - a double finish: one change;
  - payments racing over the balance: the total never exceeds it;
  - the last unit of stock: one person gets it.
- **Invariants:** after random sequences of actions, the summaries equal a recomputation from the source rows.
- **Query counts:** each hot endpoint runs the same number of queries with 10 rows or 10,000.
- **Synthetic data** (M5): `manage.py generate_load_data` fills thousands of clinics and a year of visits with SQL `generate_series`. The plans and timings of the hot queries for the biggest clinic go into `docs/v2/perf.md`.
- **Load test** (M5): simulated front desks polling and booking.

## 11. Build plan

| Milestone | Contents |
|---|---|
| M0, the base | Project and settings; PostgreSQL; `core` (API decorator, errors, request keys, composite foreign keys, partitions, normalisation, logging); accounts (sign-in, codes, throttling, passwords); clinics (memberships, permissions, services, schedules, specialty templates, counters); audit; summaries; the app shell (router, i18n, API client, sign-in, clinic picker); `seed_demo`; tests |
| M1, the front desk | Patients (search, add, edit, file); booking with turns and slots; status changes; Today with the ETag; payments, invoices and receipts; the concurrency and isolation tests |
| M2, the doctor | Visit notes, the medicine list, prescriptions and printing, the patient file's tabs |
| M3, the back office | Inventory, expenses and finances, staff pay, team and settings screens, consultations |
| M4 | Reminders, password reset, clinic sign-up, merging patient files, the day closing, exports |
| M5, scale | Synthetic data and query plans, the load test, PgBouncer and Redis in compose, a backup drill, row-level security, a Citus rehearsal |

## 12. Decisions and trade-offs

1. **A modular monolith on one database,** not microservices. A small team can run it, and the clinic boundary is already in the data.
2. **An `id` primary key, `UNIQUE (clinic_id, id)` and composite foreign keys,** not composite primary keys. Django handles single-column keys far better. When Citus comes, swapping the primary key to the existing unique index is quick.
3. **Partition the logs, not the business tables** (4.10).
4. **Polling with an ETag,** not WebSockets or server-sent events. It works through any proxy and costs one key lookup per 304. Revisit if updates must be under a second.
5. **Request keys on the rows they create,** not a separate idempotency table. Nothing to clean up, and no table that grows by tens of millions of rows a day.
6. **Plain JavaScript modules,** not React or Vue. No build and small pages. The cost is discipline in how modules are organised.
7. **Procrastinate,** not Celery and a broker. One fewer stateful service, and jobs are queued in the same transaction as the work.
8. **The change feed comes from appointments,** not an activity table. At the target that saves about 25M rows a day.
9. **Summaries in the same transaction,** not rolled up later. They are right at once, and the hot rows belong to one clinic, which writes a few times a minute.
10. **Authenticator codes for owners,** not SMS codes. No provider and no cost per message. SMS arrives later with password reset.

## 13. Risks

- A very large clinic (30 or more doctors) makes `clinic_day` a hot row. If contention shows, the version moves to Redis only and the counters go per doctor.
- The Citus move: primary key swaps, reference tables, sequences. Rehearsed on a copy in M5.
- Clinic PCs often have the wrong clock or time zone. The server decides what "today" is, and the browser never computes it.
- Before Redis, polling costs one query per 304. That is fine at stage 1.
- TOTP secrets are stored as they are. Encrypting them with an application key is an M5 item.
- The data protection review could change hosting or consent flows.

## 14. Open questions

The requirements' open questions, plus:
1. Hosting: in Egypt, or in a nearby cloud region, depending on the legal review.
2. Whether a managed Citus service is acceptable for stage 3, or a self-run cluster.
