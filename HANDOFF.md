# HANDOFF

## Project
Dento / Hamada multi-clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql, RLS), plain JS front end, Arabic RTL first.

## State now
- Works (local, NOT committed, NOT deployed): supplier backup feature (design `docs/v2/design/app-h1-supplier-backup.md`, panel + chief judge approved).
  - Backend: Supplier.backup, OrderLine.missing/is_backup, Item.need_qty; "مش عنده" endpoint, needs list, pick supplier, dismiss; receive/cancel route short qty to the item's backup → supplier's backup → the list.
  - Front end: "مش عنده" button + "ناقص N" on sent order lines; moved/late messages on receive; "محتاجين مورد" box on the orders page (qty, supplier pick list with "+ مورد جديد", اطلب, مش محتاجه); backup supplier select in the supplier dialog; badge = drafts + needs.
  - Frontend review (senior-frontend) done; fixes 1,2,3,5 applied (aria on two taps, focus after a row goes, stale needs response, item name fallback). Skipped 4 (counts differ) and 6 (alert on every receipt too loud).
- Tests: full suite 880, all pass (route list updated for the 4 new endpoints).
- Not yet looked at in a real browser.

## Decisions
- Item's own backup first, then the supplier's backup; one step only; a short backup line goes to the list.
- Moved qty = exactly what is short.
- Late receipt on a moved line is allowed and reported (alert names the backup supplier's order).
- Any receipt lowers need_qty; setting an item's supplier clears it; refill skips need_qty > 0 items.

## Key paths
- `v2/inventory/orders.py`, `order_views.py`, `urls.py`, `models.py`, migrations `0010_supplier_backup.py`, `0011_validate_supplier_backup.py` (applied locally only)
- `v2/inventory/test_backup.py`, `test_orders.py`, `v2/reports/test_routes.py`
- `v2/web/static/js/orders.js`, `templates/web/_orders_part.html`, `_order_dialogs.html`, `_strings.html`, `static/css/app.css`, `v2/web/templatetags/hamada.py`
- Arabic: `v2/locale/ar/LC_MESSAGES/django.po` (CRLF; append entries, do not resave with polib), compile with polib `save_as_mofile`
- Tests: `cd v2; export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python HAMADA_TEST_DB_SUFFIX=_x && .venv/Scripts/python.exe manage.py test --noinput`
- Local site: 127.0.0.1:8002; live: dento-jet.vercel.app (deploy is manual via the deploy script)

## Next step
Open 127.0.0.1:8002 in Brave → Orders: check the "مش عنده" button, the "محتاجين مورد" box and the backup select at 1280px and 390px; then ask Saged to commit and deploy (migrate + vercel --prod), then open dento-jet in Brave.

## Open questions
- Deferred: a version check on edit_draft; backup chains longer than one step.
- After this: remind Saged about before/after photos and the feature shortlist (B then A).
