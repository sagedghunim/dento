# Hamada v2, M1 and M1.5: chief judge's decision

Date: 2026-11-11. Scope: commits b31087f, 385ece7 and d56ad47.
Inputs: `m1-backend.md`, `m1-frontend.md`, `m1-database.md` and `m1-judge.md`, checked against the code at d56ad47.

## Verdict: REVISE

The data layer, the isolation and the base of the screens are good, and all four reviews say so. M2 cannot start yet, because five things are still open:
- the money rules can lose a cash payment or zero an invoice;
- bookings can be stranded or overlap;
- the desk screen can save twice or call in the wrong patient;
- sign-in and file opens stop on a fixed date;
- the suite is not reliably green.

Every fix is small. No part of the design needs to be redone.

## What I checked myself

Read in the code at d56ad47. All of these hold as the reviews describe them:
- `scheduling/services.py:218-227`: `_unchanged` returns `None` (a 200) when the status already equals the target, and never looks at `payment` (backend B1).
- `billing/services.py:251-252`: `remove_line` asks for `billing.discount` only when `from_list` is false (backend B2).
- `scheduling/rules.py:167`: `parse_instant` calls `astimezone` unguarded. Line 206 subtracts a timedelta from an unchecked day (backend B3).
- `scheduling/selectors.py:257`: `future_bookings(..., limit=500)` (database B2).
- `scheduling/rules.py:237`: `stranded` compares `starts_at` only (database B3).
- `scheduling/jobs.py:320-322`: the only periodic tasks are the sweep and the recompute. `ensure_partitions` runs only from two migrations (judge B7).
- Front-end B1:
  - `Submitter.reset()` makes a new key on every `open()`;
  - `closeDialog` blocks only while a send is in flight, not while the outcome is unknown;
  - `settlePending()` resends every entry under 24 hours old on every page load.
- `visit.js:50`: "paid" comes from `body.payment`, not from the answer (front-end B2).
- `pay.js:49`: "Remove" is shown for `perms.collect` on every line (front-end B4).
- Design 5.0's lock order has no membership row (database S4).

I accepted front-end B3, the rows that move under the finger, on the reviewer's browser evidence. I did not drive a browser. The code agrees with it: `today.js` patches rows in place with no scroll anchoring, and `hot()` only delays the redraw.

**Suite run** (`HAMADA_TEST_DB_SUFFIX=_rcj`, full suite):
- **Tests:** 226, OK, in 97 s.
- **Teardown:** it then crashed. `DROP DATABASE` hit the role's 5 s `statement_timeout` and left all three `test_*_rcj` databases behind. I dropped them by hand.
- **What it shows:** the judge had 2 errors in each of two runs, and the front-end reviewer's first run died during setup. So the suite passes or fails depending on timing, which confirms judge B8.
- **Leftovers from another review:** the backend reviewer's `test_hamada_*_rbe` databases (dir, s001 and s002) are still on the server. I left them alone; the backend owner should drop them.

## Disagreements settled

1. **Finish after finish with a payment: 409, or record the payment anyway?** **409 `already_finished`**, with the current row and invoice.
   - Recording it silently would turn two desks that each took cash into two payments.
   - A 409 makes the person look, and the screen offers "Collect now" against the payments endpoint, which already exists.
   - For a desk handling cash, an explicit step is safer than an automatic one.
2. **Who may remove an invoice line?** The backend review gives two options. I pick the narrow one, because a front desk must be able to fix its own mis-tap without calling the owner:
   - **The visit's own `service` line:** always needs `billing.discount`. Removing it is a 100% discount.
   - **Any other line:** needs `billing.discount`, unless the same membership added it on the same business day.
   - Add `created_by_id` (membership) to `billing_invoice_line` in a new migration. The table is empty, so the cost is nothing.
3. **How a booking waits for a schedule change: `FOR SHARE` on the sessions (backend S1), or on the doctor's membership row (database B1)?** **The membership row.**
   - The schedule side already takes `FOR NO KEY UPDATE` on that row, so only the booking side changes.
   - The fix was run and verified.
   - It also covers a stale `max_bookings` and, in M3, a doctor who is deactivated.
   - It is one row instead of N session rows.
4. **The exclusion constraint for overlaps: now, or later?** **Now.** It can't be built concurrently, and the table is empty today. `btree_gist` is a trusted extension, so the database owner can create it without a superuser. Keep `appointment_slot_uniq` as it is: equal starts still get the clear `slot_taken`.
5. **Narrow screens open on one doctor by default (front-end B3, point 3, and the judge's item 5):** deferred. Scroll anchoring and the tap guard remove the danger, and choosing the default doctor is a usability decision that belongs with the row-height work (front-end S4).
6. **Backend S5 (the reschedule fee) and the judge's "revenue stays with the first doctor":** deferred, as one decision.
   - Arrival already rechecks the fee of a visit with no invoice, so a recheck at reschedule adds little.
   - The case that matters, a prepaid visit moved to another doctor, needs a rule first: does the payment's `doctor_id` move with the visit?
   - The owner decides that rule in design 5.3 before M2's staff-pay work. It is not a coding fix now.

## Required before M2, in this order

Owners: **B** is backend, **F** is frontend, **D** is database. The backend agent applies the D items; they are written out exactly, so no separate database agent is needed. Each money, status or permission fix starts with a failing test (CLAUDE.md).

### R1. Make the suite reliably green (D, applied by B). Do this first: every later fix needs a suite you can trust.
- `patients/selectors.py`: put the search inside a `try` that ends with `SET LOCAL statement_timeout = DEFAULT` in a `finally`. This also fixes the production leak into `clinic_config` and page rendering.
- Test connections, and Django's create and drop of the test databases, run with `statement_timeout = 0`, through the test `OPTIONS` or `PGOPTIONS`. The role's 5 s limit stays for the app.
- Done when the full suite passes twice in a row from clean test databases, and no `test_*` databases are left behind.

### R2. Lost cash on finish after finish (B and F, together)
- **B:** in `change_status`, when `action == "finish"`, a payment was sent and the row is already `done`, raise 409 `already_finished` with the current appointment and invoice. Never answer 2xx without the payment row.
  - Failing tests first: one sequential and one with two threads.
- **F:** `visit.js` decides "collected" from the answer.
  - Show "collected" only when `appointment.invoice.paid` rose by the amount.
  - On 409 `already_finished`, or when nothing was paid, keep the dialog open with "The visit was already finished. Nothing was collected" and a "Collect now" button that uses a new key.
  - Apply the same rule to the "booked and collected" toast in `book.js`.

### R3. Removing invoice lines (B and F, together)
- **B:** the rule in decision 2, and the new `created_by_id` column, returned per line as `removable` for this person.
  - Failing test first: an assistant deletes the visit's service line and gets 403.
  - Also add `POST payments/{id}/refund` and `DELETE invoices/{id}/lines/{line}` to the cross-clinic loop in `billing/tests.py`.
- **F:** `pay.js` shows "Remove" only when the line says `removable`. A 403 reloads the invoice and says why.

### R4. Bookings can't be stranded or overlap (D, applied by B)
- **Booking:** `SELECT 1 FROM clinics_membership WHERE clinic_id = %s AND id = %s FOR SHARE` before the sessions are read, in `rules.doctor_day_and_service`.
- **Reschedule:** the same lock in `rules.doctor_day`, for the target doctor.
- **The stranded check reads every booking:** `future_bookings` uses keyset pages of 500 on `(day, queue_no)`, and only the list returned to the screen is capped (50).
- **`stranded` covers the whole slot:** a slot booking is covered only when both `(starts_at, ends_at)` match a slot of the new grid.
- **A new migration (never edit an applied one):** `CREATE EXTENSION IF NOT EXISTS btree_gist`, then the `appointment_no_overlap` exclusion constraint from database B3. Map its violation to 409 `slot_taken`.
- **Tests:**
  - the paused-thread tests in `scheduling/test_concurrency.py`, for delete, for update, and for reschedule against update;
  - 501 bookings with the stranded one last;
  - a slot length cut from 20 to 15 minutes is refused, and so is a direct insert at 10:15.

### R5. Log partitions and key cleanup are scheduled (B)
- Add `manage.py ensure_partitions`, for the directory and every shard.
- Register it as a daily Procrastinate periodic task. `migrate_all` also runs it, so every deploy refreshes the partitions.
- Schedule the request-key cleanup that `core/models.py` promises.
- **Design change, partition upkeep (4.10 and 5.10):** keep **6 months ahead** instead of 3, and fire the alert below **3**.
  - Empty partitions cost almost nothing.
  - A small team then has a quarter to notice a dead worker, not a few weeks.
- **Tests:**
  - the periodic registry has both new tasks;
  - a run with the clock moved forward creates the missing months;
  - a file open 150 days ahead succeeds after the run.

### R6. Out-of-range dates answer 400 (B)
- `parse_instant` catches `OverflowError` and answers 400 `invalid`.
- `quote` bounds `day` with `rules.check_day`.
- Any other caller of `validate.iso_day` that does date arithmetic catches the same error.
- Add a boundary test for year 1 and year 9999, and for `?after=` cursors at their limits.

### R7. No silent replay of a save whose outcome is unknown (F)
- **While a dialog's outcome is unknown,** it can't be closed by Escape or the close button. It offers only "Try again" (the same key and body) and "Discard". This is design 7.4 as written.
- **`open()` doesn't reset a Submitter** that holds an unknown entry. It reopens in the unknown state.
- **At page load, `settlePending` resends automatically only:**
  - row actions whose body carries the expected status;
  - entries less than 2 minutes old.
- **Every other entry** (bookings, payments, lines, discounts, finish, cancel and move) is listed in the pending bar with its label, for example "Not confirmed: booking, Mona Ali, 300 EGP", with "Try again" and "Discard".
- **Browser test:** the reviewer's E2 scenario (unknown, close, book again, navigate) ends with one booking.

### R8. Rows don't move under the pointer, and stale taps are ignored (F)
- **Anchor:** before a patch, record the row under the last pointer or with focus, or else the first fully visible row, and its `top`. After the patch, `scrollBy` the difference.
- **Tap guard:** stamp `movedAt` on a row whose position or action buttons changed. Ignore a `data-act` tap within 700 ms of it, and flash the row.
- **Browser test:** the reviewer's E1 scenario (a booking for the other doctor) leaves the rested row within a few pixels.
- **The browser tests in R7 and R8** live in `v2/web/browser_tests/`, run by hand with one documented command. They don't need to run in `manage.py test` yet.

### R9. Design text (D and B; written in the same change as R2 to R5)
- **Design 5.0, the lock order:** insert the doctor membership rows after the appointment and before the invoice.
  - Booking and reschedule take `FOR SHARE` on the target doctor.
  - Schedule changes take `FOR NO KEY UPDATE`, in id order.
  - Also list `clinics_schedule`, `patients_patient` and `clinics_clinic`, where flows take them.
  - Add a "Schedule change" row to the per-flow table: key → membership(s) → schedule → read bookings → clinic_day.
  - I checked the new edge: reschedule goes appointment → membership, and booking and schedule changes take no appointment lock before the membership. Two `FOR SHARE` locks don't conflict, so no cycle is added. This agrees with the database review.
- **The batched backfill rule (design 10, the "Migrations on large tables" line):** replace it with database S1's steps.
  - No `NO FORCE ROW LEVEL SECURITY` in a shard migration.
  - Migration A: the nullable column, then the foreign key and a not-null `CHECK`, both `NOT VALID`.
  - A resumable management command that backfills in keyset batches of about 5,000 on `(clinic_id, id)`, each in its own transaction with `lock_timeout`.
  - Migration B: `VALIDATE` the constraints, `SET NOT NULL`, then drop the `CHECK`, using `SeparateDatabaseAndState`.
  - `CompositeForeignKey` gets a `validate=False` option.
  - The code is due with the first backfill on a table that has data, not now. The `section_id` backfill already applied stays as it is, because no shard holds live data. The text is due now, so M2 is written to it.
- **Design 5.3:** who may delete an invoice line (R3), and the 409 `already_finished` (R2).
- **Design 4.10 and 5.10:** the 6-month horizon and the alert (R5).

## Deferred (not blocking M2), with the gate each must pass

**Before the first real clinic (go-live):**
- **Sign-up abuse limits (backend S3):**
  - a global hourly ceiling with an alert;
  - a per-account limit on `me/places`;
  - sliding windows;
  - a `CLIENT_IP_HEADER` warning in `.env.example`;
  - no uniqueness and no mobile sign-in for unverified mobiles until the SMS check exists.
- **Orphan shard clinics after a failed sign-up (backend S4, database S2):** follow design 5.12's order, so the directory commits first, or add a reconciliation that finds shard clinics with no directory row. No real place can be orphaned before sign-up is public.
- **The session-expiry overlay (front-end S1).** At least stop the poller from navigating away while a dialog is open.
- **The frozen leader tab (front-end S2):** a follower takes over polling, and "Try now" works.

**During M2:**
- **`web/http.py`:** use one public `clinic_request` pipeline in `core.api` (backend S2). This also gives `/patients?q=` a 503 instead of a 500.
- **The busy-day cost on cheap laptops (front-end S3):** a row hash, a lazy menu, a `Map` index, and checking Caddy's compression and ETag.
- **Row height and one doctor by default on narrow screens (front-end S4, and B3 point 3).**
- **Sign-up validation all at once, in Arabic (front-end S5),** and Arabic messages in `core/validate.py`.
- **Row actions after an unknown outcome, and a `pollAgain` flag (front-end S6).**
- **More browser tests (front-end S7):** session expiry, two tabs and the receipt PDF.
- **The reschedule fee and payment ownership:** one rule in design 5.3 (decision 6).
- **Today's other days after a patient edit,** `patients.created_by_id` as a membership id, and the truncation message (the judge's improvements).
- **Section consistency through composite keys (database N1).** Do it while the tables are small.
- **The other nice-to-haves:**
  - an audit row for off-list prices and for clinical fields;
  - require `expected` in PATCH;
  - a day check on `undo_arrive`;
  - a lighter search fragment;
  - a test of the RLS policy text;
  - an `invoice.paid` check in the recompute;
  - `db_default=0` on the counters.

**M5 (load and operations):** the memory sizing for cold Today and cold search (database N7), the directory reads on the poll path, the `due_clinics` index and the partial index for `days_to_recompute`.

## Review points rejected or narrowed

- **The M1 claim "226 tests pass":** not accepted until R1 holds. My run passed, but its teardown crashed; the judge's two runs failed.
- **Backend S1:** replaced by database B1's fix (decision 3). It is not a separate item.
- **Front-end B3 point 3 and front-end S4:** moved out of the blockers (decision 5).
- **Backend S5:** deferred (decision 6). Arrival already makes the recheck it asks for, as long as the visit has no invoice.
- **Database S1 as a code change now:** narrowed to design text now and code at the first live backfill. Building tooling for a migration that will never run on live data is not worth a small team's time.
- **Backend review's cleanup note:** its `test_hamada_*_rbe` databases are still on the server, although the review says its databases were dropped (it named only the non-test ones).

## Rationale

This platform will hold health records and cash for up to 250,000 doctors, and a small team runs it. The bar is set by what is hard to repair later:
- **Money recorded wrongly.** R2 and R3 fix it.
- **Bookings that break the schedule.** R4 adds the overlap constraint while the table is empty, which costs nothing now and needs an outage later.
- **Outages on a date nobody watches.** R5 fixes it.
- **A desk that acts on the wrong patient or saves twice.** R7 and R8 fix it.

Everything deferred can be done later without touching stored data, or it sits behind a gate (public sign-up, go-live) that hasn't opened yet.

R1 comes first, so that each fix after it lands on a suite that tells the truth.

When R1 to R9 are done, the milestone goes back to the judge only, for a check of the required items. A full four-way review is not needed.