# Hamada v2, M1 and M1.5: database review

Reviewer: senior-database. Date: 2026-11-11. Scope: commits b31087f, 385ece7 and d56ad47 (`git diff 794ae18 d56ad47 -- v2`), against `docs/v2/requirements.md`, `docs/v2/design.md` (4.0–4.5, 5.0–5.3, 5.7, 5.10–5.12, Appendices C and D) and the earlier reviews.

**How this was checked.** Everything ran on the local PostgreSQL 18.6, in databases made for this review and dropped afterwards:
- The full suite on `_rdb` test databases.
- Race experiments as extra `TransactionTestCase`s, kept outside the repo, using real threads and real connections.
- A scratch shard loaded in the pre-sections state (clinics 0001 and scheduling 0001), then migrated forward:
  - 2,000 clinics, 6.7M appointments, 1.1M patients, 407k invoices and payments;
  - clinic 1001 is the biggest: 30 doctors, 600 visits a day for two years, 100,000 files;
  - the plans were taken as the application role, which has no BYPASSRLS, under FORCE RLS.

The dev databases and port 8002 were not touched.

Labels: **[run]** measured here; **[inferred]** reasoning only, not run.

## Verdict: revise

The data layer is in good shape. What holds up:
- `clinic_id` everywhere, composite keys, and forced RLS on every new table.
- Turns and money through one upsert per summary table, in key order.
- Conditional status updates and a recompute that locks before it aggregates.
- Plans that stay on the clinic's slice of the right index.

Three things must change before M2, because they break invariants the design says the database keeps:
- B1: a booking can still be stranded by a schedule change.
- B2: the schedule-change check reads only the first 500 bookings.
- B3: nothing stops two slot visits of one doctor from overlapping.

Each fix is small now. The `section_id` backfill is correct, but it is not a pattern to repeat on a live shard (S1).

Suite: 225 of 226 pass on PostgreSQL [run]. The one error is S3, and it reproduces on its own.

## Blocking

### B1. A booking racing a schedule change is stranded outside every session [run]

The backend review listed this as S1, not run. It reproduces.

**Experiment.** Pause `book()` right after `rules.resolve_slot` returns, so the booking has validated against the old sessions. In that window, run the schedule change from a second connection, then release the booking.

| Change in the window | Change | Booking | Result |
|---|---|---|---|
| `DELETE /api/v1/schedules/{id}` (the owner's queue session that weekday) | 200 | 201 | 0 sessions that weekday, 1 booking that day |
| `PUT /api/v1/schedules/{id}` (slots 10:00–14:00 moved to 15:00–19:00) | 200 | 201 | a booking at 11:00 local, off the new grid |

**Why it happens.** `clinics/services.py::_lock_doctors` takes `FOR NO KEY UPDATE` on the doctor's `clinics_membership` row, and its comment says "a booking takes only KEY SHARE on it". That is exactly the problem.
- The booking's only lock on that row is the KEY SHARE that the composite foreign keys (`appointment_doctor_fk`, `doctor_day_doctor_fk`) take at insert.
- KEY SHARE does not conflict with NO KEY UPDATE, so neither side waits.
- `_check_not_stranded` reads `future_bookings` under READ COMMITTED. It cannot see a booking that commits after its read.
- The booking validated its slot before the change committed, so it never sees the change either.

The same gap lets a booking use a stale `max_bookings` (`DoctorDay.cap`), and it will let a booking land on a doctor deactivated in M3.

**Fix.**
- **Booking:** before reading the sessions, take `SELECT 1 FROM clinics_membership WHERE clinic_id = %s AND id = %s FOR SHARE`. FOR SHARE conflicts with the schedule change's NO KEY UPDATE.
  - Either make it the first statement of `rules.doctor_day_and_service`, or make the membership the driving row of that query with `FOR SHARE OF m`.
- **Reschedule:** take the same lock in `rules.doctor_day`, for the target doctor.
- **The schedule side doesn't change.** It already locks the membership before it reads the bookings.
- **Verified [run]:** with the lock patched in, the change waits (in the test it hit `lock_timeout` because the booking was held paused). On retry it answers 409 `schedule_conflict` and lists the booking.
- **Lock order:** add the membership row to design 5.0: request key → appointment (reschedule) → **doctor membership rows** (FOR SHARE for booking and reschedule, FOR NO KEY UPDATE for schedule changes, in id order) → counters → doctor_day → clinic_day.
- **Deadlocks [inferred]:** no new cycle.
  - The schedule change never locks an appointment, a counter or a doctor_day row. After the membership it takes only the schedule row and today's clinic_day.
  - clinic_day stays last in every flow.
- **Cost [inferred]:** the extra cost is small. Every appointment insert already takes a row lock on that membership row through its foreign-key check.
- **Test:** add the paused-thread test to `scheduling/test_concurrency.py`, for delete, for update, and for reschedule against update.

### B2. The schedule-change check reads only the first 500 future bookings [run]

`scheduling/selectors.py::future_bookings(..., limit=500)` feeds `rules.stranded`, so bookings after the 500th on that weekday are never checked.

**Experiment:**
- A doctor works a queue 17:00–23:00 and slots 09:00–12:00 on one weekday.
- 500 queue bookings next week, then one 10:00 slot booking the week after.
- Moving the slot session to 13:00–16:00 answers **200**. Booking 501 is left off the grid.

This is reachable. The booking horizon is a year (`BOOKING_HORIZON_DAYS = 366`), which is 52 weeks of one weekday. A doctor with 10–20 visits a day passes 500.

**Fix:**
- Check every booking. Either loop keyset pages of 500, ordered by `(day, queue_no)` (the query is 0.4 ms a page on the 30-doctor clinic [run]), or push the coverage test into SQL.
- Cap only the list returned to the screen (50 today).
- Test: 501 bookings with the stranded one last.

### B3. Two slot visits of one doctor can overlap; the database only checks equal start times [run]

`appointment_slot_uniq` is `UNIQUE (clinic_id, doctor_id, starts_at) WHERE holds_slot`. Equal starts are refused; overlapping intervals are not.

**Experiment:**
- Slots of 20 minutes from 10:00; book 10:00 (to 10:20).
- `PUT` the session to `slot_minutes = 15`: 200. `rules.stranded` checks only that `starts_at` is on the new grid, and 10:00 still is.
- Book 10:15: 201.
- Stored: 08:00–08:20 and 08:15–08:30 UTC for the same doctor.

**Fix:**
1. **The invariant, in the database:**
   ```sql
   CREATE EXTENSION IF NOT EXISTS btree_gist;
   ALTER TABLE scheduling_appointment ADD CONSTRAINT appointment_no_overlap
     EXCLUDE USING gist (clinic_id WITH =, doctor_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (holds_slot);
   ```
   - Verified on the scratch shard [run]: it rejects the 10:15 insert above with `conflicting key value violates exclusion constraint`.
   - It still accepts an `is_extra` visit at the same time, because `holds_slot` is false.
   - Its predicate reads only `holds_slot`, like the slot index, so status changes should stay HOT [inferred; the design measured this for the slot index].
   - Map the violation to 409 `slot_taken`, next to `appointment_slot_uniq`.
2. **Do it now.** An exclusion constraint can't be built concurrently, and adding it to a big live table holds ACCESS EXCLUSIVE while it builds. Today the table is empty.
3. **The schedule check:** `rules.stranded` should require that `(starts_at, ends_at)` both match a slot of the new grid, not only `starts_at`.

## Should fix (not blocking M1, required before the pattern repeats)

### S1. The `section_id` backfill is correct, but it is not safe on a live shard [run]

**Correctness [run].** On the scratch shard:
- every clinic got exactly one section;
- every service, session and appointment got that section's id;
- FORCE came back on all five tables;
- the new sequence is in the shard's range.

`clinics/test_migrations.py` checks the same on one clinic, and it passes.

**What it costs on 6.7M appointments (2.4 GB) [run]:**
- **Under the role's own timeouts it fails.** `scheduling.0002` was cancelled by the 5 s `statement_timeout` (`pg.ps1` sets it on the role) and rolled back cleanly.
  - The production roles' settings aren't written yet. If `hamada_owner` has the same 5 s timeout, the migration can't run at any real size.
- **With no timeout:**
  - It took **11 min 10 s** on this laptop.
  - It wrote **5.6 GB of WAL**, more than twice the table.
  - The table grew from **2.4 GB to 4.1 GB**: every row was rewritten, almost all of them as non-HOT updates, so every index got new entries.
- **Lock.** The first `ALTER TABLE … NO FORCE ROW LEVEL SECURITY` takes **ACCESS EXCLUSIVE** on `scheduling_appointment` and on `clinics_service`. It is held until commit: the UPDATE, the `SET NOT NULL` scan and the foreign-key validation are all in one transaction.
  - A second session reading clinic 1001's day under `lock_timeout = 2s` failed with `canceling statement due to lock timeout`.
  - So every Today poll and every booking on the shard fails for the whole run.
- **Side effect:** the rewrite scatters rows. After the migration, one clinic's 600 rows for a day sat on 609 heap pages, and the cold Today fetch took 942 ms against 12 ms warm (see N7).

**Why this isn't blocking.** No shard holds live data, so this exact migration only ever runs on dev and empty databases.

**Required for every later backfill** (M2 medicines and M3 stock items carry `section_id`, and M4 recomputes `day`):
1. **No `NO FORCE` in migrations.** Production migrates as `hamada_owner` with BYPASSRLS (design 4.0), so the toggle isn't needed there, and it is the statement that takes the table lock. For dev and tests, give the owner role BYPASSRLS, or backfill per clinic with `set_config`.
2. **Migration A (short locks only):**
   - add the nullable column;
   - add the foreign key `NOT VALID`;
   - add a not-null constraint `NOT VALID`: PostgreSQL 18 accepts `ALTER TABLE … ADD CONSTRAINT … NOT NULL section_id NOT VALID`, or use `CHECK (section_id IS NOT NULL) NOT VALID`;
   - from then on the code writes the column, as `book()` already does.
3. **A management command for the backfill:**
   - batches of about 5,000 ids, keyset by `(clinic_id, id)`;
   - each batch its own transaction, with `lock_timeout` set;
   - resumable, and it logs progress.
4. **Migration B (no write-blocking scan):**
   - `VALIDATE CONSTRAINT` on both constraints, which takes only SHARE UPDATE EXCLUSIVE;
   - then `SET NOT NULL`, which uses the validated constraint and skips the scan;
   - then drop the CHECK, if one was used.
   - Use `SeparateDatabaseAndState` so Django's state matches.
5. **Composite keys:** add the `NOT VALID` / `VALIDATE` pair to `core.constraints.CompositeForeignKey`, for example a `validate=False` option. Today `create_sql` always validates, holding SHARE ROW EXCLUSIVE on both tables during the scan.
6. **A check:** a test that fails any migration in a shard app that contains `NO FORCE`, or a `RunPython`/`RunSQL` UPDATE without a batch loop, unless it is explicitly allowed.

### S2. A sign-up that fails after the shard step leaves a place no reconciliation can find [run]

The backend review listed this as S4, not run. It reproduces.
- `keyed_public_call` keeps the directory transaction open around `create_clinic`. The directory row, the user and `user_clinic` are uncommitted savepoints, while `ensure_clinic_on_shard` commits on the shard.
- Making `pick_clinic` raise gives a 500, and the directory rolls back to 0 rows.
- `shard_1` keeps `clinics_clinic` (and its sections, services, owner membership, counters and sessions) under an id that no directory row will ever name.
- The ids are never reused, because sequences don't roll back, so nothing collides. The rows are just orphans.
- **This breaks design 5.12:** step 1 must commit before step 2. The nightly reconciliation walks the directory, so it can never find these rows.

**Fix:**
- Commit the claim, the account and `directory_clinic` first, and run the shard step after that commit.
- Record the result with a second statement against the same key, or make the replay finish a half-done flow.
- Or add a reconciliation that lists shard clinics with no directory row.

### S3. Search's `SET LOCAL statement_timeout = '300ms'` lasts for the rest of the transaction [run]

`patients.tests.PatientTests.test_search_runs_the_same_statements_for_10_or_10000_files` errors on this machine:
- The request's `SET LOCAL` survives `RELEASE SAVEPOINT` (checked directly: `SHOW statement_timeout` gives `300ms` after the release).
- So the test's next `bulk_patients(10000)` insert runs under 300 ms and is cancelled.

In production the request transaction is the outer one, so the setting ends with the request. Inside it, though, it applies to every later statement:
- `web/pages.py::patient_search` runs `clinic_config` after the search;
- `patients_page` renders the dialogs after it.

At the evening peak that turns a slow configuration read into a 503.

**Fix:** after the search, run `SET LOCAL statement_timeout = DEFAULT` (a `finally` in `selectors.search`). Or run the search last.

### S4. The design's lock-order table is missing the membership row

Design 5.0 lists the request key, appointment, invoice, payments, items, expenses, counters, doctor_day and clinic_day. The code also locks:
- `clinics_membership` (schedule changes now, booking after B1);
- `clinics_schedule`, `patients_patient` (patient edit) and `clinics_clinic` (`clinic_settings_view`).

I checked them against every flow [inferred]: none of them forms a cycle today. They still belong in the table, so the next flow keeps the order.

## Nice to have

- **N1. Section consistency in keys** [inferred]. Today nothing in the database ties these together:
  - an appointment's `section_id` to its service's section;
  - a consultation's `follow_up_service_id` to its own section;
  - an invoice's `patient_id` to its appointment's patient.
  - Add `UNIQUE (clinic_id, id, section_id)` on `clinics_service` and a composite FK `(clinic_id, service_id, section_id)` from the appointment, which can replace `appointment_section_fk`. The invoice works the same way.
  - Make `service.section_id` immutable (no endpoint changes it in M1; say so in the design before M3's settings screens).
- **N2. The RLS test should check the policy's text.** It should check `clinics_clinic`'s `id` policy too. Today it checks only that a policy named `clinic_isolation` exists.
- **N3. The recompute should also check `invoice.paid`** against the invoice's payments minus its refunds, for the invoices of the day's visits. It is the one money invariant no constraint holds.
- **N4. `days_to_recompute`** walks the clinic's whole `(clinic_id, day)` range below today, about 730 rows for a two-year clinic, to find the flagged days [inferred]. A partial index `(clinic_id, day) WHERE changed_since_recompute` makes it read only the flagged rows. Setting the flag is not HOT-safe with that index, so measure first; it may not be worth it.
- **N5. Database defaults for the summary counters.** The counters have only Python defaults. Any raw `INSERT` must list every counter; mine failed on `collected` being null. Use `db_default=0`, as the upserts assume.
- **N6. The test role's 5 s `statement_timeout`.** On the first suite run it cancelled Django's `DROP DATABASE` of the test databases. Give the test run `PGOPTIONS=-c statement_timeout=0`, or exempt the role's maintenance commands.
- **N7. Memory for Today and search** [run]. Clinic 1001, 600 rows a day:

  | Query | Warm | Cold |
  |---|---|---|
  | Today's full fetch | 12 ms | 942 ms (2,176 buffer reads) |
  | Word-prefix search with no match (100,000 files) | 30 ms | 1.7 s |

  The cold search exceeds its own 300 ms limit. The plans are correct; this is a working-set question. Size `shared_buffers` and RAM in M5 so that today's appointments and the name index stay cached. Watch the read hit ratio at the peak.
- **N8. `due_clinics`** reads the whole directory: 55 ms for 150,000 places per run [run]. It runs every 10 minutes per shard and job, so it's fine. Add `(shard, id)` when shards pass about 10.

## Checked and fine

- **RLS:** `clinics_section` gets ENABLE, FORCE and the policy in the migration that creates it. Every shard table with `clinic_id` is forced and has the policy (suite test [run]). The backfills restore FORCE, including on rollback [run].
- **Sequences:** every shard sequence, `clinics_section_id_seq` included, is in `[s × 2^48, (s + 1) × 2^48)` (suite test and migration test [run]). On the scratch shard all 10 sequences had MINVALUE 2^48 and MAXVALUE 2^49 − 1 [run].
- **Composite keys:** service, schedule and appointment reference `clinics_section (clinic_id, id)`, and a cross-clinic doctor is refused (suite [run]). Section names are unique among active sections. Services are unique per section, not per clinic.
- **Plans** [run], warm, clinic 1001, as the application role under FORCE RLS. Every one stays on the clinic's slice of an index that leads with `clinic_id`:

  | Query | Index | Time |
  |---|---|---|
  | Today's version (the 304 path) | `clinic_day_uniq` | 0.1 ms |
  | Doctors, sessions and sections | | 0.6 ms |
  | Today's 600 rows | `appointment_turn_uniq` in order, open invoice through the partial `invoice_one_open_per_visit` | 12 ms |
  | Doctor-days | | 0.07 ms |
  | Follow-up rule | | 1.8 ms |
  | Slots taken | `appointment_slot_uniq` | 0.06 ms |
  | Open invoice | | 0.08 ms |
  | Name prefix | index-only, 0 heap fetches | 0.06 ms |
  | Word prefix with hits | | 0.9 ms |
  | Search details | `appointment_patient_hist` | 0.4 ms |
  | `future_bookings` | | 0.4 ms |
  | The sweep's 7 days | | 4 ms |
  | Recompute: day counts | `appointment_turn_uniq` | 0.8 ms |
  | Recompute: day money | `payment_day_closing` | 2 ms |
  | Patient history page | `appointment_patient_hist`, index-only | 0.1 ms |

- **Concurrency already covered by the suite [run]:**
  - eight bookings of one slot (1 × 201, 7 × 409);
  - eight turns on one doctor-day (1..8, no gaps);
  - concurrent first payments (invoice numbers with no gap);
  - payment against cancel;
  - undo against call and finish.
- **Lock order [inferred]**, against 5.0: booking with a new patient and a payment, status changes, finish with a payment, cancel, reschedule, pay, lines, discount, void, refund, the sweep (appointments by id → invoice counter → doctor_days → clinic_days) and the recompute. Each summary table is written in one statement, in key order, and clinic_day is always last. I found no cycle.
- **Summaries [inferred]:**
  - The 5.3 table, the services and `jobs.expected_summaries` agree: `booked` = not cancelled, so the sweep's no-shows keep it; `done` is counted by status; money goes by `payment.day` and `payment.doctor_id`, refunds to the original's doctor.
  - The recompute locks the summary rows before aggregating. Any flow still in flight has not committed its source change and will add its delta after.
  - Doctor-day rows created after the lock are left flagged rather than locked late.
  - `last_queue_no` never goes down.
- **Jobs:**
  - the sweep and recompute target query is 55 ms at 150,000 places [run];
  - the sweep locks the open visits in id order and changes only open statuses, so two runs at once are safe [inferred];
  - one clinic-day per recompute transaction.
