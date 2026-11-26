# HANDOFF

## Project
Dento / Hamada multi-clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql, RLS), plain JS front end, Arabic RTL first.

## State now
- Live on dento-jet (commit fdd5871: waste alert + expenses card, overpaid box fix, FDI text; 0012 applied on all 3 DBs). Earlier, also live:
  - The "need a supplier" box shows the real shortage.
  - An item name in that box opens its card under Items.
  - The item dialog has a most-to-keep number next to the alert line; the card reads "28 من 40".
  - The Team page works again (fix in commit f4644f2).
- Done locally, NOT committed and NOT deployed. inventory + finance + web: 269 tests pass; makemigrations --check is clean.
  1. Waste (توالف), as Saged agreed:
     - `inventory.services.waste` records `item.waste` with {qty, cost = qty × unit_cost, reason}.
     - No expense row and nothing from the drawer, because the money already left with the purchase expense.
     - New alert kind `waste` in the "stock" group, with its own on/off switch.
     - The expenses tab has a new card "التوالف · month" (`_waste` in finance_pages, `waste_in` in services). Its total is not added to the expenses total.
     - New concurrent index `movement_waste` in migration `inventory/0012_movement_waste.py`.
     - Tests: `web.test_alerts.WasteTests`.
  2. The walk-in and booking "you wrote 1,500, only 600 is owed" box went stale. `credit.js` `set()` drew the box and only afterwards was the amount refilled. `set()` now fills an untouched amount before drawing. `walkin.js` and `book.js` lost their own fill lines.
     - Checked for syntax only; no browser test was run.

  3. Wording in the overpaid box: "رصيد ليه" now reads "مردود ليه في ملفه". Only the Arabic msgstr changed (.po and .mo).
     - Behaviour was already right: money paid over a bill shows in the patient file as "ليه مردود" with "رد المبلغ من الخزنة", which opens the register.

## Decisions
- No new expense row for waste, because a second row would count the money twice. It is shown apart on the expenses page instead.
- The supplier fields stay in the item dialog. Saged cancelled their removal and the search bar.

## Key paths
- `v2/inventory/services.py` (waste, waste_in), `v2/inventory/models.py`, `v2/inventory/migrations/0012_movement_waste.py`
- `v2/web/alerts.py` (KINDS, `_labels`, the item.waste line), `v2/web/finance_pages.py` (`_waste`), `v2/web/templates/web/_fin_expenses.html`
- `v2/web/static/js/credit.js`, `walkin.js`, `book.js`
- Arabic: `v2/locale/ar/LC_MESSAGES/django.po`. The file is CRLF, so append raw text; compile the .mo with polib.
- Tests: `cd v2; export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python; .venv/Scripts/python.exe manage.py test inventory finance web -v 1 --failfast`
- Deploy script: `deploy5.sh` in the old session scratchpad. It runs git archive, migrate_all on Neon, then vercel --prod. Open https://dento-jet.vercel.app in Brave.

## Next step
1. Build the approved booked-service change (section below), then run the full panel review.
2. Prescription message: wait for Saged's screenshot, then fix it.
3. Visit-summary WhatsApp button: wait for Saged's answer on prices vs names only.

## Approved by Saged 2026-11-27 (build next; money + UI → full panel)
- If the doctor removes the booked line and adds nothing, finishing the visit is blocked.
- Remove the "غيّر الحاجة المحجوزة" box from `web/templates/web/visit.html:107-115`, added in commit 3130aba.
- Give the booked line an ✕ like the procedures.
- When a change leaves money paid over, the patient file's "ليه مردود" says "اتغيّر من X لـ Y", and Hamada gets an alert.
- The exam setting already exists: `consultation_free_with_procedure` in clinics/settings.py.

## Requested 2026-11-27: the prescription won't close → say what is missing
- The prescription dialog is in `v2/web/static/js/room.js` (rxDialog, ~line 799; submit ~980). The server side is `clinical/views.py:422` and `clinical/services.py` (~338).
- Today the only check before sending is "no lines" (`rx_need_line`). The dose is not required on the server.
- The cause is unknown. Asked Saged for a screenshot of the message he sees, or for the steps.
- Plan once the cause is known: point at the field (which medicine, what's missing) and name it in the message. It's UI, so explain to Saged first.

## Requested 2026-11-27, plan sent to Saged, waiting for his OK
- On the visit screen, remove the "ابعت الخطة واتساب" icon in the next-visits part.
- At the very end of the visit screen, add one button "ابعت ملخص الزيارة للمريض". It opens WhatsApp (wa.me) with the patient's phone and a message containing:
  - what was done today
  - the prescription
  - the upcoming plan
- NOTHING else: no medical file, no visit notes, no materials.
- Open question asked: prices in the message or names only (proposed: names only).

## Open questions
- Should waste from stock entered without a purchase (opening stock) count as a real loss somewhere? unknown
- Later reminders: before/after photos; feature shortlist (B then A); inventory; phone navigation + mobile preview.
