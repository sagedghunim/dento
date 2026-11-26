# HANDOFF

## Project
- Dento / Hamada clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql), plain JS front end, Arabic RTL. Live: dento-jet.vercel.app.

## State now
- **Done, committed (8baed5e), pushed, deployed, live:** before going in (booked/waiting), a refund gives back ALL of the visit's money in one action (every payment), voids the bill and cancels the visit (off the book / out of the waiting room). A part gives `refund_all`; more than paid gives `over_refund`. The bill is never lowered by a refund (lower_bill removed). Money paid over the bill goes back alone (give-back / over refund) and the patient stays. After going in: only money paid over the bill. No-show: deposit only with the `deposit_back_no_show` switch.
- Full suite: 862 tests OK on PostgreSQL.
- Flaky, old: scheduling TodayTests `test_queues_counts_and_money_only_with_finances_view` (ordering).
- Nothing half-done.

## Decisions
- A refund before going in = the whole visit cancelled; no partial refunds then (Saged, 2026-11-26).
- An old pending request carrying `lower_bill` is ignored when approved.
- **Supplier backup plan APPROVED by Saged (2026-11-26)**, to be built next.

## Key paths
- billing/services.py `refundable()`, `refund()`, `_refund_visit()`, `check_refund_request`; scheduling/services.py `cancel_refunded()`
- pay.js (whole mode), _dialog_pay.html `data-refund-all`, web/alerts.py
- Tests: `cd v2`, then `export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python HAMADA_TEST_DB_SUFFIX=_x && .venv/Scripts/python.exe manage.py test --noinput` (about 9 min; run in background)
- Translations: no msgfmt; compile with polib (`p.save_as_mofile`). django.po is CRLF.
- Deploy is manual: scratchpad `bfcb25bc-.../scratchpad/deploy5.sh` (migrate + vercel --prod), then open dento-jet in Brave.
- Many files are CRLF: edit with Edit or Python `newline=""`, never `sed -i`.

## Next step
- Build the approved supplier backup plan (full panel review: it touches money and the data model):
  1. Supplier.backup with a one-step rule (a backup has no backup).
  2. Missing quantity on order lines.
  3. On receive, short items move to the backup supplier automatically.
  4. A "مش عنده" button on each order line.
  5. A "ناقص ومحتاج مورد" list with a supplier pick list and add-supplier in place.

## Open questions
- Show the audit `cancelled` flag in the alert text? (optional; the `lowered` readers in web/alerts.py are dead code for new rows)
- Parked: booked-discount chips in `stash@{0}`.
