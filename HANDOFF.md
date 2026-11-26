# HANDOFF

## Project
- Dento / Hamada clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql), plain JS front end, Arabic RTL. Live: dento-jet.vercel.app.

## State now
- **Done, committed (fc48d95), pushed, deployed, live:** refunds follow the visit (money back before going in; after going in only what was paid over the bill; a no-show keeps the deposit unless the owner switch `deposit_back_no_show` is on); voided-expense alert shows category + note.
- Full suite: 860 tests OK on PostgreSQL.
- Flaky, old: scheduling TodayTests `test_queues_counts_and_money_only_with_finances_view` (ordering).
- Nothing half-done.

## Decisions
- Kept the "bill goes down by the refund" option (Saged, 2026-11-23); the owner's alert shows it.
- Keep billing.refund and the approval switch (approval off by default).

## Key paths
- billing/services.py `refundable()`, `refund()`, `check_refund_request`
- clinics/settings.py (DEFAULTS, OWNER_ONLY), web/alerts.py, pay.js, team.html/team.js
- Tests: `cd v2`, then `export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python HAMADA_TEST_DB_SUFFIX=_x && .venv/Scripts/python.exe manage.py test --noinput` (full suite takes over 10 min; run in background)
- Deploy is manual: scratchpad `bfcb25bc-.../scratchpad/deploy5.sh` (migrate + vercel --prod), then open dento-jet in Brave.
- Test files are CRLF: don't edit them with `sed -i` without restoring `\r`.

## Next step
- Ask Saged the open question below on refunds, then explain the supplier backup plan and wait for his OK before building.

## Open questions
- Block the bill-lowering on refunds before the patient goes in? (judge finding: a waiting patient refunded with the bill lowered could go in owing nothing)
- Supplier backup work, agreed but not built; explain before building:
  1. Supplier.backup with a one-step rule.
  2. Missing quantity on order lines.
  3. Auto-move short items to the backup on receive.
  4. A "مش عنده" button on each line.
  5. A "ناقص ومحتاج مورد" list with a supplier pick list and add-supplier in place.
- Parked: booked-discount chips in `stash@{0}`.
