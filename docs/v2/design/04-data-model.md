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
- **Partition upkeep** (`core/partitions.py`, `manage.py ensure_partitions`), as `hamada_owner`: each new partition is created standalone with a CHECK matching its bounds, **given RLS and the standard policy before it is attached**, then attached (ShareUpdateExclusiveLock, so file opens don't wait). Old partitions leave by `DETACH PARTITION … CONCURRENTLY`. A daily Procrastinate task (`core.ensure_partitions`) keeps the current month and **6 months ahead** on the directory and every shard, and `migrate_all` runs the same upkeep on every deploy; an alert fires below **3** future months. Empty partitions cost almost nothing, and six months give a small team a quarter to notice a dead worker. CI runs the upkeep once under the production roles (M5).

**Why the business tables aren't partitioned by time.** Appointments, invoices, payments and the stock ledger are read per clinic across all time, and other rows refer to them. Time partitions would force the partition column into every foreign key and unique key, and turn a patient's history into a probe of every partition. They grow out by shards instead.

### 4.11 Request keys

- `core_request_key` in every shard: `PRIMARY KEY (clinic_id, key)`, membership_id, endpoint, request_hash (SHA-256 of the method, the path and the canonical body), status (the HTTP status of the first answer), target_type, target_id, created_at. BRIN index on created_at. Forced RLS like every tenant table.
- The directory's copy is keyed by (user_id, key), for account and platform endpoints.
- No response body and no patient data are stored: a replay rebuilds the answer from the target.
- Rows live at least 72 hours. A nightly job, as `hamada_maint`, deletes older rows in batches of about 5,000 in the 04:00–09:00 window.
- Not partitioned by day: a day-partitioned key could be claimed again after midnight (verified by the chief judge).
