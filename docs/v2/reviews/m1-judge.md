# Hamada v2, M1 and M1.5: judge's review

Date: 2026-11-11. Reviewer: judge.
Scope: commits b31087f, 385ece7 and d56ad47 (`git diff 794ae18 d56ad47 -- v2`).
Specs: `requirements.md` draft 2, and these sections of `design.md`: 5.0–5.3, 5.10, 5.11, 4.10, 10 and 11.
Earlier reviews: `m1-backend.md`, `m1-frontend.md` and `m1-database.md`. I checked their blocking points and did not repeat their work.

## Verdict: REVISE

The data layer is strong:
- isolation is enforced in the database;
- turn numbers, slots, the daily limit and invoice numbers hold under real threads;
- the hot queries stay on the clinic's part of an index at the target size.

What stops approval:
- **Money rules.** Two of them break in normal desk use.
- **Schedule changes.** They can strand bookings.
- **The desk screen.** It can call in the wrong patient, and it can save something twice.
- **A dated outage nobody has found yet.** On the current databases, from **1 March 2027** nobody can sign in, open a patient file or print a receipt, because nothing creates new log partitions.
- **The test suite.** The claim "226 tests pass" did not reproduce on my first full run.

## How this was checked

**Run:**
- **The full suite**, `HAMADA_TEST_DB_SUFFIX=_rj manage.py test`: **226 tests, 2 errors.**
  - `PatientTests.test_search_runs_the_same_statements_for_10_or_10000_files` and `SearchPlanTests.test_plans_for_a_100000_file_clinic` both hit a statement timeout.
  - Then teardown crashed: `DROP DATABASE` hit the role's 5 s `statement_timeout`.
  - The second run's result is at the end of this file.
- **Probes**, in a scratch copy of the tree made with `git archive d56ad47` (the repo itself was not changed): extra `TransactionTestCase`s with real threads, run against `_rj` test databases. Results:
  - **The daily limit (`max_bookings = 3`) against 8 bookings at once:** 3 × 201 and 5 × 409; `booked` = 3. **Holds.**
  - **Rescheduling into a slot while someone books the same slot**, 5 rounds: each time exactly one wins and the other gets 409. No 500. **Holds.**
  - **Backend B1:** `finish` with no payment, then `finish` with a 300 EGP cash payment from another person. Both get 200, `paid` is 0 and there are 0 payment rows. **Confirmed.**
  - **Backend B2:** the assistant deletes the visit's own service line. 200, and the total is 0. **Confirmed.**
  - **Plans at scale**, as the app role (not superuser, no BYPASSRLS) under forced RLS: two clinics on one shard, each with 100,000 files and 600,000 visits (400 a day). Results:
    - Today's rows: 18.7 ms cold, 400 rows;
    - the patient's history: 0.7 ms;
    - the follow-up check: 0.1 ms;
    - day counts: 0.3 ms;
    - the sweep: 3 ms.
    - No sequential scan, except `has_bookings`, where `LIMIT 1` with only two clinics on the shard makes a sequential scan the cheapest plan. **Holds.** This agrees with the database review's table.
  - **Log partitions** (new, below): after migrating there are exactly 4 monthly partitions (2026-11 to 2027-02) on `audit_access` and `accounts_account_event`. Opening a patient file with the access-log clock set 150 days ahead fails with `IntegrityError: no partition of relation "audit_access" found for row`.
  - **Today for another day after a patient rename:** `GET /api/v1/today?day=<tomorrow>` with the old ETag still answers 304.
- **The timeout leak:** `SHOW statement_timeout` inside a test case gives `5s` before a search request and `300ms` after it. This confirms database review S3, and it is the cause of the first suite error.

**Not run by me (checked against the code only):**
- backend B3 (overflow gives 500);
- front-end B1 to B4 (browser scenarios);
- database B1 to B3.

Each one matches the code path its reviewer cites:
- `rules.parse_instant` → `astimezone`;
- `api.js settlePending` resends every entry under 24 h;
- `selectors.future_bookings(limit=500)`;
- `rules.stranded` compares `starts_at` only.

All my scratch databases were dropped. The dev databases and port 8002 were not touched. I did not start a server.

## Scores

| # | Criterion | Score | Reason |
|---|---|---|---|
| 1 | Clinic business rules | 6/10 | Turns, statuses, undo, the follow-up rule, the business day and partial payments are right. But a lost payment (finish race), a 100% discount without permission, stranded or overlapping bookings, and a reschedule that keeps a stale fee all break stated rules. |
| 2 | Correctness under concurrency | 7/10 | No double turn or slot, the cap holds, refunds can't go over, and invoice numbers have no gaps (run). But a second finish swallows a payment, and a booking can race past a schedule change (database B1, run there). |
| 3 | Performance at the target scale | 8/10 | A fixed number of statements is tested (T14), and the plans are indexed at 100k files and 600k visits under RLS (run). The open risks are the browser's cost on a busy day (front-end S3) and two directory reads on every 304 poll at 20k polls a second. The design leaves both to stage 2. |
| 4 | Tenant isolation, security, privacy | 7/10 | Forced RLS on every table, with a test. Composite keys, 404 across clinics, `no-store`, strict CSP and an access log on every file open and print. But the access log is a hard dependency that will fail (B7), sign-up limits are per IP only, and unverified mobiles stay unique. |
| 5 | Operability | 4/10 | Shards, id ranges, `migrate_all` and per-shard jobs are a good base. But partition upkeep, request-key cleanup and session/throttle cleanup are designed and not scheduled; `manage.py ensure_partitions` doesn't exist; the backfill pattern locks a whole shard; and the test harness trips over the role's 5 s timeout. |
| 6 | Usability for staff, Arabic and English | 5/10 | The demo's look, RTL, bidi-safe phones and money, receipts on A5 and 80 mm, and undo toasts that name the patient. But rows shift under the finger (wrong patient called in), lost saves are silently replayed, "collected" is shown when nothing was collected, rows are twice the demo's height, and some messages are English only. |
| 7 | Simplicity and maintainability | 7/10 | Thin views, one service per flow, a written lock order and one helper for the business day. The `Deltas` upsert with CASE branches is clever and dense. `web/http.py` copies the private request pipeline (backend S2). |
| 8 | Test coverage | 6/10 | Strong isolation, permission, repeat, transition and thread tests. But the suite is not reliably green, there are no browser tests (T16 is an M1 deliverable in design 11), and none of the blocking bugs has a failing test. |

## What the earlier reviews' blocking points come to

| Point | Holds? | Judge |
|---|---|---|
| Backend B1: finish with payment after finish answers 200 and records nothing | Yes (run) | Blocking |
| Backend B2: `billing.collect` alone removes the service line, total 0 | Yes (run) | Blocking (with front-end B4) |
| Backend B3: out-of-range dates give 500 | Yes (code) | Blocking. Small, and "never a 500" is a stated rule. |
| Front-end B1: an unknown save is replayed at the next page load | Yes (code) | Blocking. It double-books and double-records money. |
| Front-end B2: "collected" shown from the request, not the answer | Yes (code) | Blocking. It is the screen half of backend B1. |
| Front-end B3: rows move under a resting finger | Accepted (browser evidence, not re-run) | Blocking. A wrong patient can be called in. |
| Front-end B4: "Remove" offered on the service line | Yes (code, `pay.js:49`) | Blocking, together with backend B2 |
| Database B1: booking against a schedule change | Yes (code; run by the database reviewer) | Blocking |
| Database B2: the stranded check reads only 500 bookings | Yes (code) | Blocking. Trivial fix. |
| Database B3: overlapping slot visits | Yes (code) | Blocking. Add the exclusion constraint while the table is empty. |
| Database S3: `SET LOCAL statement_timeout` outlives the search | Yes (run) | Required: it is the cause of B8 below |

## New blocking problems (missed by the earlier reviews)

### B7. Nothing creates the next month's log partitions: sign-in, file opens and receipts break on a fixed date

**Where:**
- `core/partitions.py` says "a daily job runs it", and design 4.10 and 5.10 name `manage.py ensure_partitions` and a daily run;
- `scheduling/jobs.py` registers only the sweep and the recompute;
- `ensure_partitions` is called only from `accounts/migrations/0001_initial.py` and `audit/migrations/0001_initial.py`.

**What happens:**
- The migrations create the current month plus 3 more, and there is no DEFAULT partition, on purpose.
- From the first month without a partition (1 March 2027 for databases migrated this month), every insert fails:
  - `accounts.services.event`: every sign-in, failed sign-in, sign-up and password change;
  - `audit.services.record_access`: opening a patient file, a patient's visits and payments, the invoice, and printing a receipt.
- The result is a full outage of sign-in, and every file and print, on a date nobody is watching.

**Evidence (run):**
- 4 partitions after migrate, `y2026m11` to `y2027m02`, on both tables.
- `GET /api/v1/patients/<id>` with the access-log clock 150 days ahead raised `IntegrityError: no partition of relation "audit_access" found for row`.

**Fix:**
- Add `manage.py ensure_partitions`, for every shard and the directory.
- Register it as a daily Procrastinate periodic task next to the sweep.
- Log an error, as the module already does, when fewer than 2 future months exist.
- Test that the periodic registry has the task, and that a run creates the missing months.
- In the same change, schedule request-key cleanup, which `core/models.py` promises ("a nightly job deletes older ones").

### B8. The suite is not reliably green, so "226 tests pass" doesn't reproduce

**Where:**
- `patients/selectors.py:50,69`: `SET LOCAL statement_timeout = 300ms` lasts for the rest of the outer test transaction (database review S3).
- `scripts/pg.ps1:85`: the role's 5 s `statement_timeout` also applies to test fixtures and to Django's `DROP DATABASE`.

**Evidence (run):**
- My first full run: 2 errors, both statement timeouts in fixture inserts.
- Then a teardown crash that left `test_*_rj` databases behind.
- The front-end reviewer's first run also died on a timeout while creating the test databases.
- `scheduling.test_judge*` teardowns in my scratch copy hit the same `DROP DATABASE` timeout.

**Fix:**
- Reset the timeout after the search, with `SET LOCAL statement_timeout = DEFAULT` in a `finally`. This also fixes the production effect: the 300 ms limit leaks into `clinic_config` and page rendering.
- For tests, set `statement_timeout = 0` on the test connections (`OPTIONS` or `PGOPTIONS`) and run the 100k-row fixtures with it.
- Keep the 5 s limit for the app at run time.

## Required fixes (numbered, for REVISE)

1. **Finish after finish with a payment** (backend B1, front-end B2):
   - answer 409 `already_finished` with the current row; never 2xx without a payment row;
   - the screen decides "collected" from `invoice.paid` in the answer;
   - failing thread test first.
2. **Removing a line needs `billing.discount`** (backend B2, front-end B4), at least the visit's `service` line. Hide "Remove" in `pay.js` to match. Failing test first.
3. **Out-of-range dates and instants answer 400** (backend B3). Bound `quote`'s day with `check_day`. Add a boundary test.
4. **No silent replay of a save whose outcome is unknown** (front-end B1):
   - keep the dialog in its unknown state, or move the entry to the visible pending bar;
   - don't make a new key while one is unknown;
   - list stale entries at page load with "Try again" and "Discard".
5. **Rows don't move under the pointer, and taps are guarded** (front-end B3): anchor the scroll, ignore taps within about 700 ms of a row moving, and default narrow screens to one doctor.
6. **Bookings can't be stranded or overlap** (database B1 to B3):
   - `FOR SHARE` on the doctor's membership row in booking and reschedule;
   - check every future booking, not the first 500;
   - add the `btree_gist` exclusion constraint while the table is empty;
   - `stranded` compares both ends of the slot.
7. **Partition upkeep and key cleanup are scheduled** (B7, above).
8. **The suite is green from a clean start, twice in a row** (B8, above).

## Improvements (not blocking)

- **Other days on Today go stale after a patient edit.** `patients.services.update` bumps only today's version, so tomorrow's list keeps the old name until its next change (run: 304). Bump the days of the patient's open visits from today on.
- **A prepaid visit moved to another doctor leaves the revenue with the first doctor** (run: the owner's doctor-day keeps the 100). This matters for staff pay as a percentage (requirements 4.7). Decide the rule, then move the payment's `doctor_id` with the visit while it has no refund, or record the rule in design 5.3. It goes with backend S5 (the fee isn't rechecked on reschedule).
- **The truncation message.** "Use the doctor filters" can't help: the filters work on the first 1,000 rows already fetched. Either filter on the server, or reword the message.
- **`patients.services.create` writes `created_by_id = ctx.user_id`.** Every other table stores the membership id (design 4.0).
- **The poll path** reads the directory twice on every 304. At 20k polls a second that is about 40k directory reads a second. Keep the stage-2 `cached_db` plan on the M5 load-test list with a pass criterion.
- **The earlier reviews' should-fix lists stand as written:**
  - backend S1 to S5;
  - front-end S1 to S7, especially a small headless-browser smoke suite (T16 is listed for M1 in design 11);
  - database S1, S2 and S4.

## Second full run
Run again from clean test databases: **226 tests, the same 2 errors** (174 s). Both are statement timeouts in fixture inserts. So the failure is reproducible on this machine, not a one-off.

## Cleanup

- Dropped `test_hamada_dir_rj`, `test_hamada_s001_rj` and `test_hamada_s002_rj` after each run.
- The scratch copy and the probe tests are in the session's temporary folder, outside the repo.
- No repo file was changed except this one.