# HANDOFF

## Project
- Dento (Hamada) multi-clinic platform v2: Django 6.1 + PostgreSQL 18 (port 5433, sharded), plain JS front end, Arabic RTL first. Reply to Saged in Egyptian Arabic only.

## State now
- Done + pushed + deployed live (2026-11-26): suppliers work; void-bill button and its alert removed; deleting a patient removed everywhere (button, API, alert, approval switch; old hidden files can still be restored; old pending delete requests can only be refused).
- Done, being committed/pushed/deployed at end of this session: a discount on a paid bill now goes through; the extra shows in the patient file as "paid over the bill" → give back from the drawer; discount + give-back show as ONE owner alert with full details (web/alerts.py `_fold_discounts`); credit selectors now use `paid - greatest(total, deposit)` like give_back. Reviewed: senior-backend (2 bugs, fixed + tests), judge APPROVE.
- If the last commit is missing from `git log`, commit + push + deploy it first.

## Decisions
- Paid-over money after a discount = patient credit, given back by tap (Saged's choice), not refused.
- A give-back folds only a discount made after the bill's last payment; split give-back (several payments) shows as one row with summed amount.
- APPROVAL_KINDS keeps "void" and "patient" only so stored settings stay valid.
- Live site is NOT linked to GitHub: every push needs the manual deploy.

## Key paths
- Deploy: `bash C:/Users/TECHNO~1/AppData/Local/Temp/claude/6410da52230ccd69/bfcb25bc-00ac-4ab7-a0a9-231f41c76a0c/scratchpad/deploy5.sh` (runs migrate_all on Neon + vercel --prod; secrets live in that scratchpad). Check https://dento-jet.vercel.app/static/js/orders.js = 200.
- Tests (Bash, from v2): `export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python HAMADA_TEST_DB_SUFFIX=_main && .venv/Scripts/python.exe manage.py test --noinput`
- Arabic strings: po_edit.py + JSON in this session's scratchpad, then `manage.py compile_translations`; keep django.po CRLF.
- Watch line endings: Python rewrites turned CRLF files into LF (billing/tests.py, selectors.py, alerts.html are CRLF; pay.js, _dialog_pay.html are LF).
- billing/services.py (set_discount, refund, give_back), billing/discounts.py, billing/selectors.py (patient_credits), web/alerts.py, web/templates/web/alerts.html

## Next step
- Discount-type chips in the booking dialog (web/templates/web/_dialog_book.html, walk-in _dialog_walkin.html): desk picks a type, saved on the appointment, applied when the visit's invoice is made (alert fires then); cancelled booking = no discount. Money change → reviewers.

## Open questions
- Every other alert: link to patient file + full details (refund/discount done; expense void, stock count, shift close, price, line remove still to enrich).
- Blacklist for patients: doesn't exist; Saged to say if wanted.
- give_back needs only billing.collect; judge suggests billing.refund — ask Saged.
- Reminders: memory remind-alerts-questions (expense-void group, empty unsent supplier order); then feature shortlist B, A, dental lab.
