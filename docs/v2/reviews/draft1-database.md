# Hamada v2 design, draft 1: database review

Reviewer: senior-database. Date: 2026-11-07. Reviewed: `docs/v2/design.md` draft 1, against `docs/v2/requirements.md` and `CLAUDE.md`.
Experiments ran on the local PostgreSQL 18.6 in a scratch database, which has since been dropped. The data set had 2,000 clinics; one of them had 30 doctors and 100,000 files. In total: 2M patients, 3M appointments, 3M invoices, 1.1M doctor-days and 1M access-log rows.
Labels: **[run]** measured here; **[source]** read in the Django 6.1.3 source (`smart-warehouse/v2/.venv`); **[inferred]** reasoning or product knowledge, not run.
The backend and judge reviews independently reproduced the invoice-number gap, the reschedule deadlock and the failed key step. I agree with them on those.

**Verdict: revise.** The tenant model is the right shape: `clinic_id` everywhere, composite foreign keys, summaries in the same transaction, conditional updates, and logs partitioned by month. Six things should change in the design before M0. Each is a line or two now. Later, each becomes a code rewrite or a rebuild of billions of rows.

## Blocking problems

### B1. Keys: make the primary key `(clinic_id, id)` from the first migration (4.0, 3.3, 12.2)
- **The stage-3 swap fails on the schema as declared.**
  - A plain Django `UniqueConstraint` becomes `CONSTRAINT … UNIQUE` [source].
  - PostgreSQL won't promote a constraint's index to a primary key: "index … is already associated with a constraint" [run].
  - It also won't drop that constraint while composite FKs depend on it [run]. The only path is `DROP … CASCADE` of every composite FK, a new unique index on every tenant table (hours per billion rows), and then revalidating every FK.
  - The swap is quick (0.9 ms on 3M rows [run]) only if `(clinic_id, id)` was a plain unique index.
  - On the partitioned `audit_event`, `ADD CONSTRAINT … USING INDEX` is not supported at all [run].
- **A successful swap still breaks the queries.**
  - Django issues `WHERE id = %s` without clinic_id for `save()`, `delete()`, `refresh_from_db()` and lazy FK loads. `select_related` joins `ON patient.id = appointment.patient_id` only [inferred].
  - While PK(id) exists, nothing forces the code to carry clinic_id.
  - After the swap there is no index on `id` alone. On 3M appointments over 2,000 clinics, each such lookup became a PG 18 skip scan: 2,002 index descents, about 6,000 buffers, 22–26 ms. With clinic_id it is 1 descent and 3 buffers [run].
  - The cost grows with the number of clinics on the node. On Citus it becomes a fan-out to every shard, and a join that isn't on clinic_id is rejected unless repartition joins are on [inferred].
  - Fixing that at stage 3 means touching every relation and every save path, the rewrite 3.3 promises to avoid.
- **The cost until then:** two unique indexes per tenant table. The redundant one is 22.5 B/row bulk-built [run]: about 0.4–0.5 TB over ~19B tenant rows at the 2-year target, plus one extra index write per insert.
- **Fix:**
  - Every tenant table gets `PRIMARY KEY (clinic_id, id)`, with `id bigint` from a sequence and no separate index on `id`. Composite FKs reference that primary key.
  - Partitioned tenant tables lead their primary key with clinic_id (B6).
  - Django mapping: `pk = CompositePrimaryKey("clinic", "id")` with `id = BigIntegerField(db_default=<nextval>)`. Fields with `db_default` come back through `RETURNING` [source].
  - Relations: a `ForeignKey` to a composite-pk model is still refused in 6.1.3 (`fields.E347`). A `ForeignObject(from_fields=("clinic", "patient_id"), to_fields=("clinic", "id"))` passes the checks [source] and joins on both columns. The design's `CompositeForeignKey` still creates the database FK, because ForeignObject creates none.
  - Fallback, if the backend review rejects ForeignObject as internal API: keep `id` as Django's pk in model state only (`SeparateDatabaseAndState`), with the database primary key still `(clinic_id, id)`.
  - In both cases, add a test-time `connection.execute_wrapper` that fails any statement on a tenant table without a `clinic_id` predicate.
- **Q1 answer:** replace. The "quick primary key swap" is not real.

### B2. Patient search misses its target as specified (5.7, 4.3)
- **The index leaves out `id`.** The design's index is `(clinic_id, name_norm) INCLUDE (full_name, phone, file_no)`. Every result needs `id`, so an index-only scan is impossible.
  - Worst case: a word-prefix query with no match, in the 100,000-file clinic. The planner left the name index and ran a bitmap heap scan over all 42,138 heap pages that hold that clinic's rows: 120–485 ms [run].
  - This is not an edge case. The second query runs whenever the prefix finds fewer than 20, which is every rare name.
- **With `id` in INCLUDE, the claim holds only while the visibility map is current.**
  - Freshly vacuumed: an index-only scan, 0 heap fetches, 17 ms warm and 44 ms cold [run]. So "about 20 ms" is right only in this state.
  - After 3,000 new files in that clinic and 20,000 edits elsewhere, with no vacuum: 18,509 heap fetches and 230 ms [run].
  - At 1B rows, autovacuum's insert trigger waits for 200M inserts [run]. That is about 5 months at 40M new files a month, and the ratio per Citus shard is the same.
- **The prefix query is a range scan only when the pattern is a literal at plan time.**
  - As a prepared statement with a generic plan (server-side binding, or PgBouncer prepared statements) it scanned the whole clinic: 118 ms against 0.08 ms [run].
  - Under a non-C collation it gets no range at all, only a seq scan [run]. Design 4.3 relies on the cluster default, which a managed host may not provide.
- **Fix:**
  - Index: `INCLUDE (id, full_name, phone, file_no)`. Declare `name_norm` and `phone_norm` `COLLATE "C"` on the column itself.
  - Autovacuum on `patients_patient` (and the appointment and invoice tables): `autovacuum_vacuum_insert_scale_factor = 0`, `autovacuum_vacuum_insert_threshold = 50000`, `autovacuum_vacuum_scale_factor = 0.02`.
  - Send the pattern as a literal (Django's default client-side binding does this), escaping `%`, `_` and `\`. Put a 300 ms `statement_timeout` on search.
  - If clinics with more than ~200k files appear, add a token table `(clinic_id, token, patient_id)` so a word prefix becomes a range scan too.

### B3. Appointment status changes can never be HOT updates (4.4)
- **The slot index's predicate references `status`.** The predicate `… AND status NOT IN ('cancelled', 'no_show')` makes `status` an indexed column.
  - So every arrive, undo, call and finish is a non-HOT update, writing a new entry into every appointment index (5–6 of them).
  - Measured on 20,000 slot appointments × 3 changes: 0 of 60,000 updates were HOT, at fillfactor 100 and at fillfactor 80 [run].
  - Each change wrote 790 B of WAL [run]. At the target that is about 350M changes a month on a 2.6B-row table.
- **Fix:**
  - Add `holds_slot boolean GENERATED ALWAYS AS (starts_at IS NOT NULL AND NOT is_extra AND status NOT IN ('cancelled', 'no_show')) STORED`. Make the index `WHERE holds_slot`, and set `fillfactor = 85` on the table.
  - Measured: 99.2% HOT at fillfactor 80 (91.6% at 100), 274 B of WAL per change (about 180 GB a month less at the target), and no index growth [run].
  - Never index `updated_at`. The change feed in 5.11 must come from the day's rows Today already fetched.
  - Decide this now: adding a stored generated column later rewrites the whole table.

### B4. Concurrency: lock order, invoice numbers and summary drift (5.0–5.3, 5.10)
1. **The lock order has levels but no order within a level.**
   - Reschedule touches two `doctor_day` rows, and two `clinic_day` rows when the day changes. Two opposite reschedules deadlock: reproduced as `deadlock detected` [run].
   - The same applies to finish with two stock items, and to the no-show sweep's several doctor-days.
   - Fix: within a level, lock in key order (items by id, `doctor_day` by (doctor_id, day), `clinic_day` by day). Lock both summary rows in one ordered statement before changing them.
   - Write the full order into 5.0: appointment → invoice → payment → items → counter → doctor_day → clinic_day.
2. **Invoice get-or-create burns numbers (5.3 step 2).**
   - `number` is NOT NULL, so the counter is taken before `INSERT … ON CONFLICT DO NOTHING`.
   - With two first payments for one visit, the second waits on the counter, takes value 2, inserts nothing, and commits. Reproduced: counter = 2, invoices = {1} [run]. That contradicts R4.4 and 4.2.
   - Also, `ON CONFLICT (clinic_id, appointment_id)` without the index predicate fails with "there is no unique or exclusion constraint matching the ON CONFLICT specification" [run]. Every ON CONFLICT on a partial index, request keys included, needs `WHERE <predicate>`.
   - Fix: lock the appointment first with `SELECT … FOR NO KEY UPDATE`, which doesn't block FK checks. Then read the invoice, and take a number and insert only if there is none.
3. **Payment vs cancel.** The payment reads the status without a lock, so a cancel and a payment can both commit, leaving a paid cancelled visit. The appointment lock in (2) fixes this.
4. **Refunds on a later day.** `doctor_day` has no `refunded` column.
   - Taking a refund off today's `collected` violates `collected >= 0` when the doctor collected less today, or has no row today, so the refund fails.
   - Taking it off the payment's day rewrites a closed day.
   - Fix: add `doctor_day.refunded`, counted on the refund's day.
5. **The nightly recompute loses concurrent increments.** Fix: in one transaction, lock the day's summary rows `FOR UPDATE` in key order, then aggregate, then write.

### B5. `clinical_prescription_item` has no usable index (4.6)
- **No index is declared.** With `db_index=False` and "all indexes declared explicitly", items are found only through the `(clinic_id, id)` prefix.
- So `GET prescriptions/{id}`, printing and "repeat the last one" scan every item of the clinic: about 12k rows for a one-doctor clinic after 2 years, and about 5M for a 30-doctor clinic [inferred from the section 2 rates].
- **Fix:** `UNIQUE (clinic_id, prescription_id, position)`, which also gives the print order. Re-check the other child tables against their read paths when writing the M0 migrations.

### B6. `audit_event`: key, volume, upkeep lock and retention (4.10, 8)
1. **It can't be distributed or re-keyed later.**
   - Its primary key `(id, at)` lacks clinic_id, and `clinic_id` is nullable for account and platform events.
   - Citus needs every unique key to contain the distribution column, and rejects NULL in it [inferred].
   - A partitioned table's primary key can't be swapped with USING INDEX [run]. At 4.8B rows the only fix is to rebuild every partition.
2. **Volume.** The design's layout measured 287 B/row [run].
   - Section 2's own figure of 300 file opens a second implies about 280M rows a month, not 200M.
   - That is 57–80 GB a month, and 1.4–1.9 TB online over 24 months.
3. **Partition upkeep stalls file opens.**
   - `CREATE TABLE … PARTITION OF` takes an AccessExclusiveLock on the parent [run], and every file open and print inserts into that parent.
   - 5.0 raises lock_timeout for jobs. So one long reader of the log queues every file open behind the job.
4. **Retention.** Dropping everything at 24 months deletes the trail of refunds, voids, discounts, stock adjustments and permission changes, while the invoices and payments they explain are kept. Tax records likely need at least 5 years; the legal review should confirm.

**Fix:**
- Split the log into two tables:
  - `audit_change`: low volume, keeps `details`, retained as long as financial records.
  - `audit_access`, monthly partitions:
    - columns: `clinic_id NOT NULL`, `membership_id`, `patient_id`, `action smallint`, `at`, `ip`, `request_id`;
    - `PRIMARY KEY (clinic_id, at, id)`, which also serves a clinic's newest-first list, so no separate (clinic_id, at) index;
    - `(clinic_id, patient_id, at)` for "who opened this file";
    - measured at 220 B/row, 23% less [run].
- Events with no clinic go to a global `account_event`.
- Upkeep: create each partition standalone, with a CHECK matching its bound, then `ATTACH PARTITION`. That takes ShareUpdateExclusiveLock [run]. Detach old partitions with `DETACH PARTITION … CONCURRENTLY`.
- The primary key also gives the Citus online rebalancer the replica identity it needs [inferred].

## Improvements (nice to have)
1. **Request keys (4.0, 12.5).**
   - Seven permanent partial unique indexes, about 37 B/row each bulk-built [run], filled by scattered UUID inserts. That is about 0.3–0.4 TB at the target, for keys needed for minutes.
   - Booking also catches a duplicate only at step 6, after taking the counter and the doctor-day lock.
   - Option: a `request_key (clinic_id, key, kind, row_id, created_at)` table partitioned by day, inserted first with `ON CONFLICT DO NOTHING RETURNING`, with partitions dropped after 7 days.
2. **The FK to `clinics_clinic` from every tenant table.** Each insert takes FOR KEY SHARE on the clinic row [run]: a WAL record per insert, and MultiXacts when one clinic has concurrent writers. Only root tables need it, since children get the clinic through the composite FK. Actor columns (`created_by_id`, `updated_by_id`) don't need an FK either, because memberships are never deleted.
3. **Room for HOT updates elsewhere.**
   - Use fillfactor 70–80 on `doctor_day`, `clinic_day` and `clinics_counter`, which change 5–7 times per visit.
   - Drop the low-stock partial index: its `quantity` predicate makes every stock use non-HOT, and a clinic's 100–500 items scan in under 1 ms.
   - The unpaid-invoice partial index has the same effect on payments. Keep it only if a screen uses it.
4. **`summaries_doctor_day` key order.** Make it `UNIQUE (clinic_id, day, doctor_id)`. Today: 1 index descent and 0.1 ms, against 31 descents and 5.4 ms with the current order. ON CONFLICT on `(clinic_id, doctor_id, day)` still finds the reordered index [run].
5. **Keyset tiebreak for `inventory_movement`.** Use `(clinic_id, item_id, at DESC, id DESC)` and `(clinic_id, at DESC, id DESC) WHERE kind = 'use'`. Movements written in one transaction share `at`.
6. **Slot overlaps.** Uniqueness on `starts_at` doesn't stop overlaps after a grid or slot-length change.
   - Use `EXCLUDE USING gist (clinic_id WITH =, doctor_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (holds_slot)`. `btree_gist` is available locally.
   - Citus accepts exclusion constraints that include the distribution column with `=` [inferred].
7. **The patient's payments page.** `billing_payment` has no `patient_id`, so "payments, a page at a time" (R4.5) joins through invoices and sorts in memory. Add the column and `(clinic_id, patient_id, received_at DESC, id DESC)`.
8. **Citus details [inferred].**
   - `accounts_login_event` is partitioned, and Citus has no partitioned reference tables, so it stays local on the coordinator.
   - Keep reference tables write-rare: disconnect Django's `update_last_login`, which would be a cross-node write on every sign-in.
   - Membership as a reference table referenced by composite FKs is supported.
   - Procrastinate tables stay on the coordinator. Its LISTEN must bypass PgBouncer's transaction mode.
   - Choose the shard count early (64–128).
   - Confirm Citus supports PostgreSQL 18 before M5, and rehearse `create_distributed_table_concurrently` with the FKs in place.
9. **Row-level security in stage 2 behind PgBouncer.** Set the clinic with `set_config('app.clinic_id', …, true)` inside one transaction per request. Autocommit reads would otherwise run without it.
10. **ETag read order (5.11).** Read the version before the data, or both in one snapshot. Read the other way round, a commit landing in between gives screens the new ETag with old data, and they get 304 until the next change. Give the Redis versions a short TTL.
11. **Server settings.**
    - Web sessions: `idle_in_transaction_session_timeout` of about 15 s.
    - WAL: `wal_compression = zstd` (off locally) and a `checkpoint_timeout` of 15–30 min. Scattered `(clinic_id, …)` inserts make full-page images most of the WAL.
12. **Arabic normalisation.** Also join "عبد ال…" forms, so "عبد الرحمن" matches "عبدالرحمن". Store a normaliser version so a rule change can be backfilled in batches.
13. **M5 data.** Build indexes by inserting rows, not with CREATE INDEX after a bulk load. Otherwise sizes come out 30–40% low: the same key measured 44.9 B/row grown against 31.5 B/row bulk-built [run].
14. **Reminder jobs (M4).** They add about 110M jobs a month to Procrastinate's table. Plan the daily cleanup before they ship.

## Answers to the eight questions
1. **Keys:** replace (B1).
2. **No time partitioning for appointments, invoices, payments and the stock ledger:** confirm.
   - These tables are read per clinic across all time.
   - Time partitions would add the partition column to every FK and unique key, and turn each patient history into a probe of every partition.
   - They would bring no retention benefit for rows that are kept forever. Shards bound the size per node.
   - Conditions: the autovacuum settings in B2. If a retention period is ever set for non-medical ledgers, delete in batches.
3. **Citus blockers:**
   - PK(id), and the id-only queries and joins (B1);
   - the audit key and its nullable clinic_id (B6);
   - `login_event`, `last_login`, Procrastinate and PostgreSQL 18 support (I8).
   - Fine as designed: every other unique key includes clinic_id, sequences are bigint, and membership works as a reference table.
   - Cross-clinic queries: only "my clinics", which reads the reference table, and platform counts, a multi-shard aggregate over `clinic_day`.
4. **Search:** only with `id` in INCLUDE, visibility-map upkeep, literal patterns and the C collation on the column (B2).
5. **Turns, slots, counters, lock order:**
   - Turns are correct: the doctor-day upsert serialises them and a rollback returns the number.
   - Slot uniqueness is correct, but it blocks HOT updates (B3) and misses overlaps (I6).
   - Counters are gap-free except in get-or-create (B4.2).
   - The lock order is incomplete (B4.1). Drift comes from refunds and the recompute (B4.4–5).
6. **Indexes:**
   - Missing: B5, I4, I5 and I7.
   - Harmful or useless: PK(id) (B1), the slot predicate (B3), the low-stock partial index (I3), and the separate (clinic_id, at) on the access log (B6).
   - Verified fine: Today runs a bitmap scan on the queue key plus nested loops, and the invoice's partial unique index is used through the join clause [run].
7. **Audit:** B6.
8. **Scale:** next section.

## Scale model and stages (section 2, 3.3)
- **The arithmetic holds.** 250k × 15 × 26 = 97.5M visits a month, and 104 visits/s over the evening (140 at 20 visits).
  - **Assumption:** if 19:00–21:00 carry 1.5 times the evening average, design for 160–210 visits/s and 1,100–1,500 write transactions/s.
  - Full Today refetches: about 5 version bumps per visit × about 2 screens ≈ 1,000–1,400/s. This is consistent with the design's 5%.
- **Storage is low.** Measured bytes per row, bulk-built:
  - appointment 396 B (heap 194 B + 6 indexes 203 B); invoice 292 B; patient 493 B (thin test rows); access log 287 B.
  - Applied to section 2's row counts, with estimates for the unmeasured tables, that gives about 7 TB bulk-built.
  - Organic index fill (+30–40%) and update bloat bring it to about 8.5–10 TB. Plan for about 10 TB at 2 years, and about 40 GB at 1,000 doctors, not 6–8 TB.
  - B1, B3 and B6 save roughly 1 TB.
- **Stages.**
  - Stage 1 (≤10k doctors): about 4 visits/s and 30 write transactions/s; about 800 polls/s × 3 queries before Redis.
  - Stage 2 (≤40k doctors): about 17 visits/s and about 1.5 TB at 2 years. Both are comfortable on the stated servers.
  - Throughput doesn't force stage 3; the 2-hour restore (R5) does. Beyond roughly 3–4 TB, a 2-hour restore is unlikely.
  - Make the stage triggers measured limits (size against the restore drill, CPU, WAL rate), not doctor counts.
  - Stage 3 at 8–16 workers means 0.6–1.2 TB per worker, which is reasonable.

## What is right and should stay
- `clinic_id` on every tenant row, every index led by it, and composite FKs on `(clinic_id, x_id)`. Keep them; B1 only changes what they reference.
- Sharding by clinic rather than time-partitioning the business tables (Q2). Monthly logs with no DEFAULT partition, kept 3 months ahead, with an alert.
- Conditional `UPDATE`s plus CHECKs for status, stock and balances: no negative stock, `paid <= total`, discount bounds, a void has paid = 0.
- Turn numbers from the doctor-day upsert, and gap-free numbers from an upsert inside the transaction that uses them (once B4.2 is fixed).
- Summaries in the same transaction with a nightly recompute; finances read summaries only; ETag polling with a one-key 304.
- Money as bigint piastres, `timestamptz` plus a server-computed local `day`, status CHECKs, length limits, nothing deleted, bigint everywhere.
- Web `lock_timeout` and `statement_timeout`; expand-and-contract migrations; `NOT VALID` then validate; indexes built concurrently.
- Membership and the clinic as reference tables in stage 3.

## Verified by running (PostgreSQL 18.6, scratch database, C collation)
- **Keys:**
  - the swap fails with a UNIQUE constraint ("already associated with a constraint"), and dropping the constraint fails because of the FKs;
  - with a plain unique index the swap takes 0.9 ms;
  - afterwards, id-only lookups make 2,002 descents and take 22–26 ms;
  - USING INDEX is unsupported on partitioned tables.
- **Search:**
  - the design's index loses to a bitmap heap scan of 42,138 pages (120–485 ms);
  - with `id` in INCLUDE: an index-only scan, 17/44 ms;
  - after churn: 18,509 heap fetches and 230 ms;
  - with a generic plan: 118 ms against 0.08 ms;
  - non-C collation: a seq scan;
  - autovacuum insert threshold at 1B rows: 200M.
- **HOT:** 0/60,000 HOT with the design's predicate at fillfactor 100 and 80; 99.2% and 91.6% with the generated column; WAL 790 against 274 B per change.
- **Races:** invoice counter 2 with invoices {1}; crossing doctor-day updates give `deadlock detected`; ON CONFLICT without the predicate fails, and with it works.
- **Locks:** `CREATE TABLE … PARTITION OF` takes AccessExclusiveLock on the parent, CREATE plus ATTACH takes ShareUpdateExclusiveLock; an FK insert takes "For Key Share" on the clinic row (pgrowlocks).
- **Index order:** `doctor_day` takes 31 descents against 1, and ON CONFLICT ignores column order. The Today plan uses the invoice's partial unique index through the join.
- **Sizes:** the bytes per row above, plus the access log at 287 B/row (design) against 220 B/row (proposed).
- **Caveats:**
  - a Windows laptop with `shared_buffers` of 128 MB, so "cold" means the OS cache;
  - indexes built after a bulk load, so organic sizes are larger;
  - a generator slip made all synthetic appointments `done`, which affects none of the measured results.

## Inferred, not run
- All Citus behaviour (Citus isn't installed locally): unique keys must contain the distribution column; NULL distribution values are rejected; no partitioned reference tables; FKs to reference tables are allowed; the rebalancer needs a replica identity; PostgreSQL 18 support.
- Django ORM behaviour: id-only `save()` and lazy loads, and joins on `id` alone. The `E347`, `ForeignObject`, `db_returning` and `UniqueConstraint` findings were read in the 6.1.3 source, not executed.
- Totals for unmeasured tables, WAL per visit, the peak-hour factor, the 5-year tax retention, and the item counts in B5.
