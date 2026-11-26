# HANDOFF

## Project
- Dento / Hamada clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql), plain JS front end, Arabic RTL. Live: dento-jet.vercel.app.

## State now
- **Done, tested, NOT committed, NOT deployed:**
  - Refund rule: money goes back only before going in (booked/waiting). After going in, only money paid over the bill can go back. A no-show keeps the deposit unless the owner switch `deposit_back_no_show` (team page, off by default) is on, and then only the deposit goes back. The billing.refund permission and the "بإذنك" approval stay. Old pending requests on a visit that went in say "ارفضه".
  - The voided-expense alert now shows the category and note (web/alerts.py `items()`).
  - pay.js: a no-show offers only its deposit for refund.
  - New tests: over-bill refund after going in, no-show deposit only, voided-expense alert.
  - billing + finance + web: 362 tests OK. The full suite was 857 OK before the last small changes.
- **Fix before commit:** `v2/web/test_alerts.py` shows a 268-line diff. `sed -i` probably changed its line endings, so restore the original endings and keep only the new test.
- Flaky, old: scheduling TodayTests `test_queues_counts_and_money_only_with_finances_view` (ordering).

## Decisions
- Kept the "bill goes down by the refund" option (Saged, 2026-11-23). The judge flagged it: a waiting patient refunded with the bill lowered could still go in owing nothing. The alert shows it to the owner. Ask Saged whether to block lowering before going in.
- Keep billing.refund and the approval switch (approval off by default).

## Key paths
- billing/services.py `refundable()`, `refund()`, `check_refund_request`
- clinics/settings.py (DEFAULTS, OWNER_ONLY), web/pages.py clinic_config, team.html/team.js, pay.js, web/alerts.py
- Tests: `cd v2`, then `export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python HAMADA_TEST_DB_SUFFIX=_x && .venv/Scripts/python.exe manage.py test billing finance web --noinput`
- Deploy is manual: the scratchpad deploy5.sh (migrate + vercel --prod), then open dento-jet in Brave.

## Next step
- Fix the line endings in test_alerts.py, commit everything (end the message with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"), push, deploy, and open dento-jet in Brave.

## Open questions
- Block the bill-lowering on refunds before the patient goes in? (judge finding)
- Supplier backup work, agreed but not built; explain before building:
  1. Supplier.backup with a one-step rule.
  2. Missing quantity on order lines.
  3. Auto-move short items to the backup on receive.
  4. A "مش عنده" button on each line.
  5. A "ناقص ومحتاج مورد" list with a supplier pick list and add-supplier in place.
- Parked: booked-discount chips in `stash@{0}`.
