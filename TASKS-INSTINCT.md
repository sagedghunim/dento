# Instinct review (2026-11-21): what is left

Done in 2dce5b0 (tested, deployed 2026-11-21):
- `v2/web/templates/web/_dialog_book.html`, `v2/web/static/js/book.js`: booking dialog crashed in a clinic with one section (missing `data-qty-box`). Box now always rendered; JS guarded.
- `v2/web/static/js/today.js`: a Today row held under the mouse is now updated when the mouse leaves it (`pointerout` + periodic `flushHeld`).
- `v2/core/cron.py`, `v2/core/test_cron.py`, `v2/config/urls.py`, `v2/vercel.json`: daily upkeep for Vercel (no worker). Vercel Cron calls `GET /cron/daily` at 01:00 UTC with `Authorization: Bearer $CRON_SECRET`; runs partitions, day sweep, summaries recompute, request-key cleanup.

## Do in order
1. DONE: full suite 688 tests on PostgreSQL; one seed_activity test failed in the full run, passes alone (timing flake).
2. DONE: deployed 2dce5b0; `CRON_SECRET` in Vercel project env (Production) and per deploy; `/cron/daily` returned 200, all four steps ok; 404 without the secret.
3. HALF: live static files hold the fix; Book + Book the next visit with a procedure pass in headless Edge on the local one-section clinic. Not clicked on live (demo password unknown to Claude). Instinct to re-test.
4. Receipt screen: show the receipt after a payment, print on A5 (`print-a5.css` exists), clinic header, "Printed" mark.
5. Reports MVP: income per doctor, by payment method, money owed, top services. Selectors exist in `reports/`; pages only.
6. "Send reminder" button per booking (wa.me link with the booking time). No unofficial automation.
7. Demo clean-up: seed inventory items; make sure `DJANGO_DEBUG` is not 1 on production and seeding refuses to run when DEBUG is off.
8. Patient header: label the counter "Finished visits" (Arabic + English translations).
9. English UI: receipts and labels still in Arabic; translate.
10. Onboarding: every empty screen says the next step.
11. Patient archive: confirm the owner's delete/hide works from the UI; Instinct tested as desk.

## Needs Saged's decision (do not do alone)
- Move hosting to Europe (Vercel `fra1` + Neon EU) before the first real clinic. `vercel.json` region is `cle1` (USA).
- InstaPay/wallet payments (no public merchant API; reference number per invoice is the simple path).
