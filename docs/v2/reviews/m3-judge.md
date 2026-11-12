# M3 back office: judge review (commit a9aa201)

Scope: backend only (finance, inventory, reports, billing packages and shifts, patients/tags.py, summaries, clinical record_procedure). Spec: design Appendix F and 5.0.

## What I checked

- Ran `finance inventory billing.test_packages reports` on PostgreSQL (`_judge` suffix): 31 tests, all pass. That includes the shift-close vs cash-payment race, the last stock unit and the last package session.
- Read the code paths for shifts, payments and refunds, packages, stock, expenses, pay, the reports and the recompute.
- Reproduced one money bug with a scratch test outside the repo (see fix 1).
- RLS: every new table calls `EnableRLS` (finance x6, inventory x3, billing_package, billing_patient_package, patients_tag, patients_patient_tag, summaries_money_day, summaries_patient). The generic test in `core/tests.py:152` covers every table that has `clinic_id`. Every cross-table reference is a composite `(clinic_id, id)` FK.
- Codes: every M3 route declares its codes, and `reports/test_routes.py` pins them and checks that each route answers 403 without the code. `finances.view`, `finances.pay` and `patients.export` are OWNER_ONLY.
- Shift vs payment: the payment INSERT takes the open shift `FOR SHARE` in the same statement. Close takes `FOR NO KEY UPDATE`, then sums, then updates. A cash payment that waits behind a close re-checks `status = 'open'`, finds nothing and gets 409 `register_closed`. This is correct, and close takes no lock after the shift, so no cycle is possible.
- Last unit: items are locked in one statement by id, then a conditional UPDATE runs, and a CHECK backs it. Correct.
- Last session: a conditional UPDATE with a `FOR NO KEY UPDATE LIMIT 1` subquery, retried up to 3 times, plus CHECK `used <= sessions`. Correct.
- Money: all amounts are bigint piastres, and the CHECKs on amounts are positive or not negative. Pay share is `(net*bp+5000)//10000`.

## Scores

| # | Criterion | Score | Reason |
|---|---|---|---|
| 1 | Business rules | 7 | F1 to F8 are mostly right, but a prepaid package session is lost forever when its procedure line is removed (reproduced), and the recompute ignores expenses. |
| 2 | Concurrency | 8 | The shift/payment, last-unit and last-session races are handled and tested. The documented lock order does not match the real one (shift and counter positions). |
| 3 | Performance at scale | 8 | Reports read the summaries and partial indexes, with a fixed number of statements per screen. Insights count over all of a clinic's `summaries_patient` rows, which is fine up to about 10^5 patients. |
| 4 | Isolation, security, privacy | 8 | RLS on every table, composite FKs, OWNER_ONLY export with an access event per patient, CSV-injection guard. Gap: `reports.view` (not owner-only) shows revenue per day and per doctor. |
| 5 | Operability | 7 | The recompute does not repair `clinic_day.expenses`. Nothing backfills `summaries_patient` or `summaries_money_day` for data from before M3. The synchronous export is acceptable at its cap. |
| 6 | Usability (backend view) | 7 | Clear error codes. Two traps: the 5,000-row export cap cuts silently, and opted-in materials at zero stock block procedure recording. |
| 7 | Simplicity | 7 | Raw SQL that is consistent and commented. Several files (`clinical/services.py`, `scheduling/tests.py`, `scheduling/selectors.py`, `directory/flows.py`) show about 4,000 lines of line-ending churn for about 70 real changed lines, which hurts blame and review. |
| 8 | Tests | 7 | Good race tests and route-code tests. Missing: removing a line that used a package session, expenses in the recompute, export truncation, and the tag filter on lists. |

## Rulings on the builder's deviations

1. **Lock steps 8a/11a/12a, and the counter taken before items in record_procedure: accept, but the design must be updated (fix 3).** I found no cycle. Only close holds the shift exclusively, and it takes nothing after it. No flow locks items and then the invoice counter. The code also does not follow its own "8a" comment: booking takes the shift `FOR SHARE` after counter invoice and doctor_day, which is harmless for the same reason. Section 5.0 is the source of truth, so it must state the real order and why it is safe.
2. **Packages for procedures only: accept for M3.** F7 says "procedure or service". Record the narrowing in Appendix F and plan services later.
3. **Default stock not seeded at sign-up, opt-in endpoint: accept.** Improvement: the opt-in response or screen should warn that seeded materials at zero stock will refuse recordings (409 `out_of_stock`). Better: seed those items with `allow_negative = true` until the first count.
4. **CSV export synchronous, capped at 5,000 rows: accept, on condition of fix 4.**
5. **Profit uses accrued pay, insight thresholds as constants: accept.** The thresholds are returned in `rules`. Improvement: the default "salaries" expense category plus pay rules double-counts staff pay in profit. Exclude that category from profit, or warn about it.
6. **Staff statement with per-day totals: accept for now.** F4 says "lists the visits". Add a per-day drill-down later through the indexed appointments query (clinic, doctor, day).
7. **No DB constraint for cash without a shift: accept.** All payment and refund rows go through `_insert_payment` (billing/services.py:143), which enforces the rule in the same statement. Payouts do have a CHECK.
8. **Billing migrations 0004 to 0006 not squashed: accept.** v2 is not deployed. Squashing is optional before go-live.

## Required fixes

1. **A removed procedure line keeps its used package session.** `v2/billing/services.py:297-322` (`remove_line`) only voids packages for `kind == 'package'` lines. A `procedure` line that used a session (`clinical_visit_procedure.patient_package_id`) is deleted, but `used` is never decremented. There is no other endpoint to give the session back, and use is automatic by default (`use_package` defaults to true, `clinical/services.py:803`), so one mistaken recording costs the patient a prepaid session. I reproduced it in a scratch test: sell 3 sessions, record one, the doctor removes the line (200), and `left` stays at 2.
   - Fix: under the invoice lock, which matches the void path's invoice-then-package order, find the visit procedure with `invoice_line_id = line_id AND patient_package_id IS NOT NULL`. Then run `UPDATE billing_patient_package SET used = used - 1 WHERE clinic_id = %s AND id = %s AND used > 0`, clear the link, and audit it.
   - Write the failing test first.
2. **The recompute ignores expenses.** `v2/scheduling/jobs.py:196-216, 280-283`: `recompute_day` still says "Expenses are left alone until M3 adds them". M3 added them and profit reads `summaries_clinic_day.expenses`, but drift there is never repaired. The new doctor-day report fields are recomputed, so this one stands out.
   - Fix: add `sum(amount) WHERE voided_at IS NULL` from `finance_expense` for the day (index `expense_day`) to `expected_summaries`, and include `expenses` in the clinic-day compare and update.
   - Add a test next to `finance/tests.py:196`.
3. **Section 5.0 does not match the code** (`docs/v2/design.md` section 5.0 list and table). Add:
   - the open shift (`FOR SHARE` for payments, refunds, cash movements, expenses and payouts; `FOR NO KEY UPDATE` only for close, which takes nothing after it, so it may be taken after counters or doctor_day);
   - `billing_patient_package` (record: plan item, then package, then invoice; void and remove-line: invoice, then package);
   - `summaries_patient` (11a) and `summaries_money_day` (12a, written just before the clinic-days);
   - the record-procedure row (counter invoice, then items, then tooth, then doctor_day, then clinic_day) and the purchase row (shift, item, expense, clinic_day);
   - a note that no flow takes items before the invoice counter.

   Also correct the module docstrings in `finance/services.py:3` and `billing/services.py:3` so they agree with section 5.0.
4. **The CSV export truncates without saying so.** `v2/reports/views.py:47-51`: a list over `EXPORT_LIMIT` (5,000) comes back silently cut, and staff calling a "never came back" list would miss patients without knowing it. Fix: fetch `EXPORT_LIMIT + 1` rows, and when there are more, either answer 409 `export_too_large` (ask for a tag filter) or set an `X-Truncated: true` header and a final note row. Test it.

## Improvements (not blocking)

- `reports.view` is not owner-only, but performance, staff performance and insights return revenue. Show the money fields only to holders of `finances.view`, or make the code's label say it includes revenue.
- Add a one-off backfill command for `summaries_patient` and `summaries_money_day` from source rows before any real data is migrated. The recompute does not cover `summaries_patient` at all.
- Removing a procedure line does not return stock or decrement `doctor_day.procedures`. This is consistent with "the record stays", but there is no way to undo a mistaken recording (no delete for the record). Plan an "undo recording" flow that reverses the session, the materials and the counts.
- `add_expense` is not audited (only void is). Back-dated expenses change closed months' profit, so audit them or limit the back-dating.
- The tag filter on search post-filters a page (`patients/tags.py` `keep_tagged`), so pages can come back short. Use the `patient_tag_by_tag` index as the driver when a tag is given.
- Many `FOR SHARE` holders on the shift row can delay a close under heavy load. Unverified at scale; watch it in the load test.
- Add `.gitattributes` (`* text=auto eol=lf` for `*.py`) and renormalise once, so line-ending churn stops hiding real changes.

## Verdict

**REVISE**, with required fixes 1 to 4 above. Fixes 1 and 2 are small code changes, each with a test. Fix 3 is documentation only. Fix 4 is a few lines.