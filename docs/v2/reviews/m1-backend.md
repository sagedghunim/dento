# Hamada v2, M1 and M1.5: backend review

Date: 2026-11-11. Reviewer: senior backend.
Scope: commits b31087f, 385ece7 and d56ad47 (`git diff 794ae18 d56ad47 -- v2`): patients, scheduling, billing, summaries, sections, sign-up and adding a place, the desk undo, the jobs, and the web pages and fragments.
Specs: `requirements.md` draft 2; `design.md` sections 5.0–5.3, 5.10–5.12, 6.1–6.2 and Appendix D.

## Verdict: revise

The core is sound. Tenant isolation holds on every path I tried, from the API, the fragments and the pages. The state machines decide with one conditional `UPDATE`. Request keys replay correctly, including under threads. The lock order matches design 5.0. The sign-up limits hold under concurrency.

Three problems block approval. Two of them are money rules that a front desk will hit in normal use: a payment that is answered 200 but never recorded, and an assistant without the discount permission who can zero an invoice. All three fixes are small.

## How this was checked

**Run:**
- The full suite on PostgreSQL 18 with `HAMADA_TEST_DB_SUFFIX=_rbe`: 226 tests, OK, in 100 s.
- My own server on 127.0.0.1:8005, against my own databases (`hamada_rbe_dir`, `hamada_rbe_s001`, `hamada_rbe_s002`). It had two clinics on two shards and real sessions, cookies, CSRF, `X-Membership` and request keys. The probe scripts were kept outside the repo.
- Races, using threads over HTTP against that server.

**Not run (reasoned from the code):** anything marked "not run" below.

## Blocking

### B1. A finish with a payment that loses a race answers 200, takes no money, and the screen says "paid"

**Where:** `scheduling/services.py`, `change_status` (lines 146–191), and `_unchanged` (218–227).

**What happens:**
1. The conditional `UPDATE` finds no row, because someone else already finished the visit.
2. `_unchanged` sees `status == target` and returns `None`, "a harmless repeat".
3. `payment` is never looked at. The view answers 200 with the appointment.
4. `web/static/js/visit.js:50-52` then shows `finished_paid` with the amount.

This is not a repeat of the same request. A repeat carries the same request key and is replayed before the service runs. This is a second person's different action.

**Evidence (run):**
- Sequential: desk A sends `finish` with no payment, and the visit is done and owed. Desk B (the doctor) then sends `finish` with `payment {amount: 30000, method: cash}` and a new key. The answer is **200**, with invoice `paid: 0` and `due: 30000`. No payment row is written.
- Concurrent: the same two requests, sent from two threads, both answer **200**, and `paid` is 0.

The doctor hands over 300 EGP of cash, and the system records none of it.

**Fix:**
- In `_unchanged`, when `action == "finish"` and a payment was sent, do not answer the harmless 200. Two options:
  - answer 409 `status_changed` (or a new `already_finished`) with the current row, so the screen offers "take payment" on the done visit; or
  - run `billing.pay` for the done visit in the same transaction. This is acceptable because `finish` already holds `billing.collect`.

  I prefer the 409. It is explicit, and the payment endpoint already exists for done visits.
- Write the failing test first. Two threads: `finish` with no payment, and `finish` with a payment. Assert that the payment is either recorded or refused with a 4xx, never answered 2xx without a payment row.

### B2. `billing.collect` alone can remove the visit's service line and bring the invoice to 0

**Where:** `billing/services.py`, `remove_line` (lines 242–264). Only a line with `from_list = false` needs `billing.discount`.

`create_invoice` (line 71) writes the visit's own service line with `from_list = true`, even when the fee is the follow-up price. So any assistant (the template has `billing.collect` but not `billing.discount`) can delete it while nothing is paid. The total then drops to 0.

The effect is a 100% discount, or a void, with no discount or refund permission. Requirements 4.4 say "a discount (with permission)" and "refunds and voids need a permission". The removal is audited, but an audit row is not a permission.

**Evidence (run):**
- The assistant (codes: book, queue, patients, `billing.collect`, `inventory.use`) calls `POST appointments/{id}/invoice` and gets total 30000, with one `service` line where `from_list = true`.
- `DELETE invoices/{id}/lines/{line}` answers **200**, and the total is now **0**.
- `POST invoices/{id}/void` by the same assistant answers 403. That is right, and it shows the inconsistency.

**Fix:**
- Removing any line needs `billing.discount`.
- At the least, removing the `kind = 'service'` line that `ensure_invoice` created needs `billing.discount`. Lowering an invoice is a discount, whatever the line.
- If the desk must undo its own mistaken list line, allow `billing.collect` only for a line added by the same membership in the same business day. That needs `created_by_id` on the line.
- Add the failing test: an assistant deletes the service line and gets 403.
- Update design 5.3 so it says who may delete.

### B3. Out-of-range dates give 500

**Where:**
- `scheduling/rules.py:158-167`, `parse_instant`. `.astimezone(dt.UTC)` raises `OverflowError` for an instant near year 1. It is used by `POST appointments` and `POST appointments/{id}/reschedule`.
- `scheduling/rules.py:205-207`, `history_params`. `day - timedelta(days=window)` underflows. `GET appointments/quote` takes any `day` without `check_day`.

**Evidence (run), each a 500 with an `OverflowError` traceback in the server log:**
- `POST /api/v1/appointments` with `starts_at = "0001-01-01T00:00:00+05:00"`.
- `GET /api/v1/appointments/quote?...&day=0001-01-02&patient=<id>`.

The rule is that bad input is a clear 4xx, never a 500.

**Fix:**
- Catch `OverflowError` in `parse_instant` and in `validate.iso_day` callers, and answer 400 `invalid`.
- Bound `quote`'s day with `rules.check_day`. It is the same rule as booking, and the Book dialog never asks for another day.
- Add these cases to a small boundary test, together with `?after=` cursors at the limits.

## Should fix before M2 (not blocking)

### S1. A schedule change can race a booking and strand it (not run)

`clinics/services.py:_lock_doctors` takes `FOR NO KEY UPDATE` on the membership. A booking takes only `KEY SHARE` on it, through the composite foreign key, and the two locks don't conflict. This is as the comment there says.

So a booking that read the doctor's sessions before `delete_session` or `update_session` commits can insert a slot or turn that `_check_not_stranded` never sees. It can't see an uncommitted row.

**Fix:** in `rules._session_rows_sql`, read the sessions with `FOR SHARE OF s` (only on the booking and reschedule path, not on quotes). A session update or delete then waits for in-flight bookings, and a booking waits for the schedule change. This adds no new lock-order edge: the schedule row comes after the request key and before the summaries in both flows. Agree it with the database reviewer, and add a thread test.

### S2. `web/http.py` copies the request pipeline from private helpers in `core.api`

`fragment()` repeats `_clinic_call`'s order of checks, using `_active_scope`, `_require_user`, `build_ctx` and `_db_error`. Today it is equivalent: it checks the session, the forced password change, the two-factor state and `X-Membership`, then sets RLS in one shard transaction. I found no isolation hole. Cross-clinic `/print/receipts/<id>`, `/patients/<id>` and their fragments are 404 (run).

It will drift, though. `page()` in `core/api.py` already differs:
- It doesn't map `OperationalError`, so a search past its 300 ms `statement_timeout` on `/patients?q=` is a 500 page. It is a 503 on the fragment and the API. (Not run.)
- It re-raises any `ApiError` whose code isn't in its list as a 500.

**Fix:** one public context manager in `core.api`, for example `clinic_request(request, codes, any_of)`, that does steps 1–5 and the error mapping. `@api`, `@page` and `@fragment` all use it.

### S3. Sign-up abuse limits are per IP only

The limits work as designed. Five concurrent sign-ups from one address gave exactly 3 × 201 and 2 × 429 (run), and `X-Forwarded-For` is ignored by default (run). Gaps:
- **No ceiling across addresses and no per-account limit on `me/places`.** Anyone with many addresses can create places without limit. Each center writes up to 20 sections, their services, 6 sessions and a directory row. Add a global hourly ceiling with an alert, and a per-account limit on `me/places`, for example 3 a day.
- **Fixed windows.** `throttle._start` aligns the hour and the day to the clock, so "3 an hour" allows 6 within two minutes around the hour. Use a sliding count, or check the current and the previous window together.
- **`CLIENT_IP_HEADER`.** `client_ip` takes the leftmost value of the header. If operations set it to `HTTP_X_FORWARDED_FOR`, the client chooses its own address and every limit is gone, sign-in included. Say in `.env.example` that it must be a header the proxy overwrites (`X-Real-IP`), or take the rightmost hop the proxy appended.
- **The mobile number is unverified but unique.** Until the SMS check exists (requirements 7.6), someone can register a real doctor's mobile, up to 20 tries an hour per address. That blocks the doctor's own sign-up with `phone_taken`, and the 409 tells anyone whether a number has an account. Until SMS arrives, consider not enforcing uniqueness, and not allowing sign-in by mobile, for unverified numbers.

### S4. A failed sign-up can leave a place on the shard with no directory row (not run)

`keyed_public_call` holds the directory transaction. `create_clinic` writes the shard and commits it inside that transaction. If anything after it fails, for example `event`, `_complete`, `pick_clinic` or recording the key, the directory row and the account roll back but the shard's clinic, sections, services and membership stay.

Make sure the nightly reconciliation (design 5.12) also removes or flags shard clinics that have no directory row. Otherwise add a test that fails a step after the shard commit.

### S5. The follow-up price doesn't follow a reschedule

`reschedule` keeps `fee` and `is_follow_up` when the doctor or the day changes, but follow-up eligibility depends on both (requirements 4.1). `_recheck_follow_up` at arrival repairs this only while the visit has no invoice. So a prepaid follow-up that is moved to another doctor keeps the lower price, and a booking moved into the window keeps the full price.

Recheck the fee in `reschedule` when there is no open invoice, the same way arrival does.

## Nice to have

1. **Audit:**
   - `add_line` at a price not from the list needs `billing.discount` but writes no audit row. It is a price change and should be audited like a discount.
   - `patients.services.update` audits identity fields only. Changes to allergies, chronic conditions and notes are clinically sensitive and should be logged too.
2. **`PATCH patients/{id}`:** `expected` is optional per field. A field sent in `changes` without `expected` is a silent last-write-wins. Design 5.0 says a PATCH sends the expected old value, so require it for every changed field.
3. **`undo_arrive`** has no day check. It can move a past day's waiting visit back to `booked` after the day ends and before the sweep, and the sweep then makes it a no-show. Allow it only on the visit's business day, like `arrive`.
4. **Messages:** `core/validate.py` messages ("note must be text", "… must be an id") are English only. The design asks for messages in the user's language, and the field codes are already there.
5. **Search fragment queries:** `web/pages.py:patient_search` calls `clinic_config` on every keystroke that finds something, only for doctor names. That costs three more statements: services, distinct schedules and memberships. Read just the doctors' names.
6. **`from_list` label:** `create_invoice` marks the service line `from_list = true` even at the follow-up price. That is harmless once B2 is fixed, but misleading in reports.
7. **Missing tests** (the behaviour was checked by HTTP here, but the suite doesn't hold it):
   - `finish` against `finish` with a payment (B1);
   - deleting a line with `billing.collect` only (B2);
   - date and cursor boundaries (B3);
   - the cross-clinic loop in `billing/tests.py:209` lacks `POST payments/{id}/refund` and `DELETE invoices/{id}/lines/{line}`; both answered 404 in my run;
   - `test_the_lists_cover_every_m1_route` covers only the `patients`, `scheduling` and `billing` URL modules, so `clinics` (sections, schedules), `directory` (sign-up, places) and the `web` pages and fragments can gain a route without isolation and permission rows; extend it, or add one for `fragment_meta` and `page_meta`;
   - sign-up at the per-IP limit under concurrency;
   - schedule change against booking (S1).

## Checked and fine (run unless marked)

- **Isolation:**
  - Another clinic's payment refund, line delete, receipt page, invoice, appointment and patient all answer 404.
  - The suite's T1 loop also covers bodies that point at another clinic's doctor, service or patient.
  - The role `hamada` is neither superuser nor `BYPASSRLS`, so `FORCE ROW LEVEL SECURITY` really applies in tests and on my server.
- **State machines:**
  - `undo-call` against `finish`, 10 rounds in parallel threads: each round, exactly one wins and the other gets 409. The outcomes were 6 undos and 4 finishes, and there were 6 `undo_call` audit rows.
  - Six concurrent refunds of 100 on a 300 payment: 3 × 201 and 3 × 409, with `invoice.paid` at 0.
- **Request keys:**
  - Five threads with one key on `undo-arrive`: five 200s, one state change and one audit row.
  - Replays need the permission again.
  - A reused key with another body or path is 422.
- **Lock order** (not run, checked against design 5.0) for booking with a new patient and a payment, status changes, finish with a payment, cancel, reschedule, pay, refund, lines, discount, void, the sweep and the recompute.
- **Money:**
  - Money is integer piastres everywhere, with `parse_money` capped at 10^10.
  - Database checks hold `paid ≤ total`, `total = subtotal − discount`, `void ⇒ paid = 0` and `refunded ≤ amount`.
  - Revenue counts on the payment's business day.
  - Refunds count on their own day, against the original doctor.
- **Business day:** one helper (`core/days.py`) is used everywhere. The slot grid puts after-midnight sessions on the next calendar date. Arrive is limited to the visit's business day. A payment inside a booking is limited to today. The time zone and day end are frozen once the place has bookings.
- **Undo (D4):** each undo works only from the status right after the step, keeps the turn, bumps the version and writes `appointment.undo_*` with before and after.
- **Sign-up:** the honeypot, Django's password validators, `signup_try` counted outside the transaction and the place limits inside it, the HMAC of the password in the request hash, and the replay that signs in through the normal password path.

## Cleanup

After writing this, I stopped my server on port 8005 and dropped `hamada_rbe_dir`, `hamada_rbe_s001` and `hamada_rbe_s002`. I did not touch the dev databases, the `_rdb` databases or port 8002.
