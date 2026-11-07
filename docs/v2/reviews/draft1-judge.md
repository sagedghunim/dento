# Hamada v2 design, draft 1: judge's scorecard

Reviewed: `docs/v2/design.md` (draft 1, 2026-11-06) against `docs/v2/requirements.md` and `CLAUDE.md`.
Standard applied: the same bar as `smart-warehouse/docs/v2/design.md` (its Appendix A and B), which required a per-flow lock table, a stored request fingerprint for safe repeats and a full requirements map.
Experiments: run on the local PostgreSQL 18.6 in a scratch database (`hamada_judge_scratch`, C collation, dropped afterwards). Citus isn't available locally, so Citus behaviour below comes from analysis and is marked unverified.

## 1. Scores

| # | Criterion | Score | Reason |
|---|---|---|---|
| 1 | Clinic business rules | 6 | Turns, slots, statuses, follow-ups and refunds are mostly right. But evening sessions that run past midnight break Today, arrive and no-show, a prepaid booking can be cancelled with no money rule, and the follow-up rule differs from R4.1 |
| 2 | Concurrency | 6 | Conditional updates, CHECKs and upserts are the right tools. But invoice creation burns numbers (verified), reschedules deadlock (verified), a payment can land on a visit being cancelled, and several creates double on retry |
| 3 | Performance at the target | 6 | The hot paths are bounded, money screens read summaries, and the polling model is realistic. But the search scan as specified reads every heap page of the clinic (verified, 8 to 15 times the claim), the 304 cost is understated, and Django lookups by id alone would fan out under Citus |
| 4 | Isolation, security, privacy | 6 | Good: four layers, composite FKs, 404 for other clinics' records, TOTP, CSP, audit and access logs. But the database doesn't isolate reads until M5, the membership cache conflicts with instant revocation, and TOTP secrets stay in plain text until M5 |
| 5 | Operability | 5 | Expand/contract migrations, PITR backups and monitoring are solid. No HA or failover for 99.9%, no runbooks, and the stage-3 path isn't credible as written |
| 6 | Usability, Arabic and English | 7 | RTL-first with logical properties, `Intl` formatting, server-side A5 and 80 mm printing, and behaviour on weak networks are all handled well. Gaps: no flow for the duplicate-phone hint, a fixed print language, and the queue at midnight |
| 7 | Simplicity and maintainability | 7 | Right-sized: monolith, no build step, no broker, polling, summaries updated in the same transaction. Minus for leaning on Citus as the only way to grow, and its hidden cost to the ORM |
| 8 | Test coverage | 6 | Strong isolation and permission tests built from the URL conf, real-thread races, invariant checks and query-count checks. Missing: retry and idempotency tests, the reschedule and pay-versus-cancel races, midnight and DST tests, an access-log test, front-end tests, and CI on Citus |

## 2. Claims tested

**a. "The 304 path costs one key lookup" (2, 12.4) and the read rates.** Refuted.
- Stage 1 has no Redis, so sessions live in the database. One 304 therefore costs three indexed lookups: the session, the membership (with the clinic's status), and the `clinic_day` version. Section 10 concedes this ("plus the session and membership").
- Stage 2's "0 database queries with Redis" needs a membership cache (3.1). That contradicts 5.8 and 5.9 ("read from the membership row on every request, so a change applies at once"). No rule says when the cache is invalidated.
- The Redis version is "set after each commit". Two commits can set it out of order, or a crash after commit can skip the set. Then screens get 304 for a stale queue until the next change, which can be minutes away.
- Read rates: 100,000 screens / 5 s = 20,000 req/s is correct. "≥95% are 304" is plausible. An average clinic at the peak has about 2.5 visits an hour × 5–6 version bumps, against 720 polls an hour per screen, so about 98% are 304. Unverified until the M5 load test. Hidden-tab polls (+8%) and search-as-you-type traffic aren't counted. The web-tier CPU for 20,000 Django requests a second ("40+ containers") is unverified.

**b. "The word-prefix scan … about 20 ms for 100,000 files" (5.7).** Refuted as specified; true after a one-line fix.
- Test set: 2M patients, one clinic with 100,000 files scattered through the heap, the design's index `(clinic_id, name_norm) INCLUDE (full_name, phone, file_no)`.
- Without `id` in the select list: index-only scan, 17 ms warm. That matches the claim.
- The results page needs `id` to open a file, and `id` isn't in the INCLUDE list. With `id`, the planner switches to a bitmap heap scan over all of the clinic's rows: **48,763 heap pages read, 163 ms warm, 295 ms on first run**.
- At 1B patient rows those pages aren't cached, so a search costs about 100k random reads. That breaks the 200 ms p95 for large clinics and flushes the cache.
- With `INCLUDE (id, full_name, phone, file_no)` it is index-only again: 17 ms (verified).
- The cost still grows with the clinic's size, and clinics never shrink. It also depends on a current visibility map: insert-only autovacuum on a 1B-row table runs rarely unless it is tuned per table.

**c. "Summaries last, doctor before clinic, so no deadlock cycles" (5.0).** Refuted.
- Reschedule (5.2) updates two `doctor_day` rows, and across days two `clinic_day` rows, in an order set by the request. Two receptionists moving patients A→B and B→C between doctors at the same time: **`ERROR: deadlock detected` (verified)**.
- Not ordered either: a stock request with several items ("materials used"), and a payment that touches the doctor-day rows of both the appointment day and the payment day.
- The payment flow takes the counter before its entity (the invoice). That's harmless, but it contradicts the stated order.
- The nightly recompute (5.10) writes summaries from a read it made without locking them, so it can overwrite a live increment (a visit finished after midnight) and "fix" a correct row into a wrong one.
- The other flows (booking, status changes, refund, discount, void, purchase, use, adjust, prescription, note, team) do follow the order.

**d. "Stage 3 is a migration, not a rewrite" (3.3, 4.0, 12.2).** Refuted by analysis; Citus itself not tested here.
- The literal step, `ADD PRIMARY KEY USING INDEX` on the existing `UNIQUE (clinic_id, id)`, fails: *index "…" is already associated with a constraint*. That index can't be dropped either, because the composite FKs depend on it (both verified).
- `DROP` the `id` key plus `REPLICA IDENTITY USING INDEX` works and is quick (verified). So the key change itself is fine once the doc says it correctly.
- The real problem is 4.0's `ForeignKey(..., db_constraint=False)` on `id` alone:
  - `select_related` joins `ON patient.id = appointment.patient_id`, without `clinic_id`. Citus rejects that join between tables distributed on the same key, unless repartition joins are turned on.
  - Related-object access, `.get(pk=)` and `.save()` filter on `id` only, so each one runs on every shard.
  - The result: every data-access path gets rewritten in M5.
- Also not addressed:
  - "jobs queued in the same transaction" turns into a cross-node transaction;
  - `audit_event`'s key `(id, at)` has no `clinic_id`;
  - RLS on distributed tables and Citus support for PostgreSQL 18 are both unverified.

**e. "Request keys on the created rows make every create safe to repeat" (4.0, 5.0, 6.1).** Refuted.
- These have no request key: `billing_invoice_line`, `finance_staff_pay`, members and accounts, services, schedules, medicines, items, `platform/clinics`.
- `api.js` (7.1) retries creates by itself after a timeout or a 5xx. So a slow `POST invoices/{id}/lines` adds the line twice and the patient is overcharged. A retried member create returns 409 although the account exists.
- No payload fingerprint is stored. A reused key with a changed body silently returns the old row.
- 7.4 keeps the form after "not saved" but doesn't say whether pressing Save again reuses the key. With a new key, an attempt that did commit is recorded twice (double payment or double booking).
- The discount endpoint doesn't say whether it sends an absolute value with the expected old value.

**f. "Turn numbers are never given twice and have no gaps" (5.1, 5.2).** Partly refuted.
- "Never twice" holds: there is a unique index, and the upsert's row lock serialises bookings. A rolled-back booking gives its number back (verified).
- "No gaps" fails:
  - a reschedule abandons the old number by design;
  - invoice numbers do have gaps when two first payments (or a payment and a finish) race. **Counter = 2, invoices = {1} (verified)**, against 4.2 and R4.4.
- The SQL printed in 5.1 fails as written: *column reference "last_queue_no" is ambiguous* (verified). It needs `summaries_doctor_day.last_queue_no`.
- "Moves the counters" in 5.2 must never lower `last_queue_no`, or a number will be given twice.

**g. Scale model (2).** The arithmetic holds; there are omissions.
- Each row of the table checks: 250,000 × 15 × 26 = 97.5M visits a month, 104–139 visits a second at the peak, about 1,000 write transactions a second, 20,000 polls a second, 40M new files a month (within R2's 1,500–4,000 a year per clinic).
- My own size estimate is about 7.4 TB with indexes after 2 years, so 6–8 TB is plausible.
- Missing:
  - login events (500k accounts, about 15M+ a month), job rows and per-clinic catalogue copies;
  - growth after year 2: nothing clinical is ever deleted, so data grows by about 3.7 TB a year at the target;
  - the biggest clinic: 30 doctors means 100k+ files in about 2 years and a hot `clinic_day` row;
  - the RTO (2 h) at each stage.
- The stage limits (10k, 40k doctors) are unverified estimates.

## 3. Requirements map

| R-section | Design | Milestone | Gap |
|---|---|---|---|
| R1 purpose, specialty catalogue | 4.1, 4.2 | M0 | Specialty templates cover services and medicines, not the stock list |
| R2 scale, retention | 2, 3.3, 4.10 | M5 | Growth after 2 years isn't modelled |
| R3 roles, per-person permissions | 4.1, 4.2, 5.8 | M0, M3 | OK |
| R4.1 setup, follow-up, schedules | 4.2, 5.1 | M0, M3 | Follow-up uses "finished, same doctor"; R4.1 says "paid". Which follow-up price applies isn't defined |
| R4.2 patients, search | 4.3, 5.7 | M1 | No flow or endpoint for the duplicate-phone suggestion |
| R4.3 booking, queue, Today | 4.4, 5.1, 5.2, 5.11 | M1 | Midnight; finish-and-collect in one step isn't defined; `undo_arrive` and early no-show aren't in R4.3 |
| R4.4 billing | 4.5, 5.3, 7.5 | M1 | Refund, discount and void aren't named in M1 |
| R4.5 file, notes, prescriptions | 4.6, 5.5, 5.6 | M2 | Print language is fixed per clinic; R4.5 says Arabic or English per print |
| R4.6 inventory | 4.7, 5.4 | M3 | OK |
| R4.7 expenses, finances | 4.8 | M3 | No refunds per doctor for the percentage pay; the permission for `GET expenses` is unclear |
| R4.8 consultations | 4.9 | M3 | No state machine |
| R4.9 team, settings | 5.8 | M3 | What changing the specialty does isn't defined |
| R4.10 accounts | 5.9, 8 | M0 | OK |
| **R4.11 platform admin** | 6.2 | **none** | Not in any milestone, so clinics would again be created in the shell |
| R4.12 reminders | none | M4 | OK (later) |
| R5 performance | 10 | M5 | See 2a, 2b |
| **R5 availability 99.9%, RTO 2 h** | **none** | **none** | No HA or failover; a replica exists but its promotion isn't designed |
| **R5 isolation "by the database"** | 8 | **M5** | Reads aren't isolated until RLS in M5, after go-live |
| R5 audit and access log | 4.10, 8 | M0 | Sub-endpoints (visits, prescriptions, payments, notes, receipts) can be read without an access event |
| **R5 runbooks** | **none** | **none** | Missing |
| R5 language, devices, time, money | 4.0, 7 | M0 | Egypt has had DST again since 2023; there are no tests for it |
| R6, R7 | 14 | | OK |

## 4. Blocking problems

1. **Safe repeats are incomplete** (4.0, 5.0, 6.1, 7.1, 12.5).
   - Failure: creates without a key are retried by `api.js`. Invoice lines double (an overcharge), staff pay rows double, member creation returns a false 409. A changed payload under a reused key returns the old row. A manual resubmit after "not saved" can record a payment or booking twice.
   - Fix:
     - a key on every mutating POST;
     - a stored request hash (on the row, or in a small idempotency table partitioned by day and dropped after 2 days), with 422 on a mismatch;
     - the form keeps its key until its content changes;
     - discounts and other value changes sent as absolute values with the expected old value;
     - tests: same key twice, two copies of one key at once, one key with different bodies.
2. **Invoice creation and the payment rules** (5.3, 5.2 cancel, 4.2).
   - Failure:
     - a racing first payment or finish burns an invoice number (verified gap);
     - the "not cancelled" check isn't locked, so a payment can land on a visit being cancelled;
     - cancelling a prepaid booking leaves money on a cancelled visit;
     - R4.3's finish-and-collect isn't defined.
   - Fix:
     - payment, finish, cancel and reschedule lock the appointment row first;
     - find the invoice under that lock, and take a number only when one is actually inserted;
     - cancel refuses while `paid > 0` (refund first) and voids an empty invoice;
     - `finish` takes an optional payment in the same transaction;
     - race tests for both cases.
3. **Lock order is incomplete** (5.0, 5.2, 5.4, 5.10).
   - Failure: opposite reschedules deadlock (verified); requests with several items are unordered; the nightly recompute overwrites live increments.
   - Fix:
     - add "rows of one table in key order" to the global order;
     - a per-flow lock table, as in the warehouse design's 5.1;
     - retry once on `40P01` and `55P03`;
     - the recompute takes `FOR UPDATE` on the summary rows before it reads the source rows, and runs after the business day ends;
     - a reschedule within the same doctor-day keeps its number, and `last_queue_no` is never lowered;
     - fix the ambiguous SQL in 5.1.
4. **The business day ends at calendar midnight** (4.0, 5.2, 5.10, 5.11).
   - Failure: in an evening-peak market, at 00:00 Today switches to an empty day. Patients still waiting can't be marked arrived. Walk-ins restart at turn 1 mid-session. A sweep that runs after midnight marks people still in the session as no-shows.
   - Fix:
     - a clinic setting "day ends at" (default 04:00 local), used for every `day` value, for Today and arrive, for the no-show sweep (after the day ends plus a grace period) and for the summaries;
     - tests across that boundary and across the Africa/Cairo DST changes.
5. **The patient search scan isn't covered by its index** (4.3, 5.7).
   - Failure: the query must return `id`, which the INCLUDE list lacks. It reads every heap page of the clinic (49k pages, 163–295 ms on 2M rows, verified), and seconds at the target.
   - Fix:
     - `INCLUDE (id, full_name, phone, file_no)` (17 ms, verified);
     - per-table autovacuum settings for inserts on `patients_patient`;
     - a 250 ms debounce and a 2-character minimum in the client;
     - state the large-clinic plan: a word table `(clinic_id, word, patient_id)` that turns the search into a range scan, built when a clinic passes about 100k files;
     - a plan check for the largest clinic in M1.
6. **Stage 3 is a rewrite as specified** (3.3, 4.0, 12.2, M5).
   - Failure: Django relations by `id` alone make joins and lookups that Citus can't route to one shard. The primary-key step as written fails (verified). Jobs in the same transaction and `audit_event`'s key aren't addressed.
   - Fix:
     - from M0, declare tenant relations as composite `ForeignObject` on `(clinic_id, x_id)`, or ban id-only joins with a lint;
     - a SQL-shape check in the test suite: every statement on a tenant table constrains `clinic_id` for each tenant table in it;
     - run the suite on a Citus container in CI from M1;
     - rewrite the key step as "drop the `id` key, `REPLICA IDENTITY USING INDEX`";
     - decide where jobs live in stage 3;
     - put `clinic_id` in `audit_event`'s key.
7. **The live queue and caching rules are wrong or missing** (2, 3.1, 5.8, 5.9, 5.11, 10).
   - Failure:
     - the 304 cost is understated;
     - a membership cache with no invalidation lets a deactivated member keep reading health data;
     - a Redis version that goes backwards serves stale queues, against R4.3;
     - stock use (the low-stock banner), patient edits, schedule changes and payments for another day don't bump the version.
   - Fix:
     - state the real cost per stage;
     - delete the membership cache entry after the commit that changes it, with a TTL of at most 30 s (or no cache);
     - write the Redis version with "set only if higher" and a short TTL;
     - list every change that bumps the version;
     - put the permission set in the ETag;
     - tests.
8. **Requirements not mapped** (R5, R4.11).
   - Failure: there is no availability design for 99.9% and RTO 2 h, no runbooks, no milestone for the platform admin, and the database doesn't isolate reads before go-live.
   - Fix:
     - an HA section: managed PostgreSQL with HA, or Patroni with a tested promotion; behaviour of PgBouncer and Redis on failover; the RTO at each stage;
     - a list of runbooks (restore, failover, migration, partitions, incidents, key rotation);
     - the platform admin in M1;
     - RLS on every tenant table from M0, through `SET LOCAL app.clinic_id` in the request transaction, with a `BYPASSRLS` role only for migrations and per-clinic jobs;
     - a test showing that a query without a clinic filter returns nothing.

## 5. Improvements (not blocking)

- Follow-up rule: align it with R4.1 ("paid") or record the change as an assumption. Say which follow-up service's price applies, and whether a follow-up starts a new window.
- Get Saged's confirmation for `undo_arrive` and for a no-show "after the session ended". Enforce `max_bookings` in the doctor-day upsert, and check that the doctor works that weekday.
- Prescriptions: `?lang=ar|en` per print, and a clinic default for the print language.
- Specialty templates should include a stock list (R1). Add a duplicate-phone endpoint and a step in the Book dialog (R4.2). Add a consultation state table (R4.8). Define what changing the specialty does (R4.9).
- An access event (or explicit coverage) for every endpoint that returns clinical data, checked by a test built from the URL conf.
- Encrypt TOTP secrets in M0, not M5. Scrub health data from Sentry events. Lock the screen after a period of inactivity on shared front-desk PCs. Adding an existing username should notify that person or need their acceptance. Platform admins hold no clinic memberships.
- Behind PgBouncer's transaction mode: set timeouts per role or with `SET LOCAL`, give the Procrastinate worker a direct connection for LISTEN, state `archive_timeout ≤ 300 s` for the 5-minute RPO, and plan PgBouncer HA.
- Add the missing index `clinical_prescription_item (clinic_id, prescription_id, position)`.
- Finances: add `refunded` to `doctor_day`, for revenue per doctor and the percentage pay.
- Scale model: add the omitted tables, the years after year 2, the biggest clinic, and traffic from typing in the search box.
- Add a small browser smoke suite: RTL layout, printing, retry behaviour.
- Drop "no gaps" for turn numbers, and state how a reschedule handles them.

## 6. Verdict

**REVISE.** The architecture is sound and the right size: one monolith, clinic-scoped keys, summaries in the same transaction, conditional updates, ETag polling. Most of the scale arithmetic checks out. But several of its central safety claims fail when tested: safe repeats, gap-free numbers, no deadlocks, the 20 ms search, and stage 3 as a migration. Two R5 requirements (availability and isolation by the database) have no design.

Blocking problems to fix before approval:
1. Safe repeats: a key and a stored request hash on every mutating POST; key reuse on resubmit; absolute values (4.0, 5.0, 6.1, 7.1).
2. Invoice creation and payment rules: lock the appointment, take a number only after a real insert, a rule for cancelling a paid booking, finish-and-collect in one step (5.3, 5.2).
3. Lock order: same-table rows in key order, a per-flow lock table, retry on deadlock, a safe recompute, reschedule number rules (5.0, 5.2, 5.4, 5.10).
4. A business day that ends at a clinic-set hour instead of calendar midnight, with DST tests (4.0, 5.2, 5.10, 5.11).
5. Search: `id` in the covering index, autovacuum tuning, debounce, a plan for large clinics (4.3, 5.7).
6. Stage 3: composite relations or a lint, a SQL-shape check, CI on Citus from M1, a correct key step, jobs and `audit_event` (3.3, 4.0, 12.2).
7. Live queue: the real 304 cost, membership cache invalidation, a Redis version that only goes up, a complete list of version bumps (3.1, 5.8, 5.11, 10).
8. Unmapped requirements: HA and RTO, runbooks, the platform admin in a milestone, RLS from M0 (R5, R4.11).
