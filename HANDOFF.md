# HANDOFF

## Project
Dento / Hamada multi-clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql, RLS), plain JS front end, Arabic RTL first.

## State now
- Committed, NOT deployed: booking source labels: الحجز جه منين · في العيادة · بالتليفون · من النت (web tests 183 OK).
- Live on dento-jet (commit fdd5871: waste alert + expenses card, overpaid box fix, FDI text; 0012 applied on all 3 DBs). Earlier, also live:
  - The "need a supplier" box shows the real shortage.
  - An item name in that box opens its card under Items.
  - The item dialog has a most-to-keep number next to the alert line; the card reads "28 من 40".
  - The Team page works again (fix in commit f4644f2).
- Also committed, NOT deployed: test `clinical.test_remove_procedure.DearerThanPaidTests` (dearer visit → rest owed, credit 0; passes).

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
0. Booking + walk-in amount (Saged decided 2026-11-27): less than owed is fine (rest stays owed); MORE is never allowed, no keep-as-credit choice. Findings:
   - Server already refuses more than owed (`billing.services.plan_booking_money`, error `over_balance` with `most`) UNLESS the payment says `keep_credit` (accepted in `scheduling/views.py:37-38`, used at `scheduling/services.py:170`).
   - "Owed" = the visit's fee + the patient's old unpaid bills − credit held. Keep that: paying old debt at booking is still fine.
   - Plan: drop `keep_credit` from the API (views) and from `plan_booking_money`; `credit.js` loses the keep button (`data-keep-credit`, `over_keep`, `over_kept`, `fill()`), the over box keeps one button "خليها {most}" (`data-keep-most`), and the amount input gets `max`. Templates with the box: grep `data-over` (book + walk-in dialogs). Remove unused msgids (CatalogueTests).
   - `billing/test_credit.py` builds credit with `keep_credit` in 6 tests: rebuild that credit another way (e.g. M2 visit: record a procedure, pay all, remove it → credit, as `test_remove_procedure.test_removed_after_paying_leaves_the_patient_a_credit`), and turn `test_more_than_everything...` into "refused, and keep_credit no longer exists".
   - Money → full panel review.
1. Build the approved booked-service change (section below), then run the full panel review.
2. (Prescription issue: Saged solved it himself.)
3. Build the visit-summary WhatsApp button (names only, no prices).

## Approved by Saged 2026-11-27 (build next; money + UI → full panel)
- If the doctor removes the booked line and adds nothing, finishing the visit is blocked.
- Remove the "غيّر الحاجة المحجوزة" box from `web/templates/web/visit.html:107-115`, added in commit 3130aba.
- Give the booked line an ✕ like the procedures.
- When a change leaves money paid over, the patient file's "ليه مردود" says "اتغيّر من X لـ Y", and Hamada gets an alert.
- The exam setting already exists: `consultation_free_with_procedure` in clinics/settings.py.

## Approved by Saged 2026-11-27: visit-summary WhatsApp button (UI → explain done, build it)
- On the visit screen, remove the "ابعت الخطة واتساب" icon in the next-visits part.
- At the very end of the visit screen, add one button "ابعت ملخص الزيارة للمريض". It opens WhatsApp (wa.me) with the patient's phone and a message containing:
  - what was done today
  - the prescription
  - the upcoming plan
- NOTHING else: no medical file, no visit notes, no materials.
- Decided: names only, no prices.

## Open questions
- Should waste from stock entered without a purchase (opening stock) count as a real loss somewhere? unknown
- Later reminders: before/after photos; feature shortlist (B then A); inventory; phone navigation + mobile preview.
