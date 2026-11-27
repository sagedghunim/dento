# HANDOFF

## Project
Dento / Hamada multi-clinic platform v2: Django 6.1 + PostgreSQL 18 (sharded, raw SQL via core.sql, RLS), plain JS front end, Arabic RTL first.

## State now
- Live on dento-jet and pushed (2026-11-27): a booking or walk-in payment can never be more than what is owed; the keep-as-credit choice is gone (server + UI, commit 623bf83). Full panel approved. Full suite 887 OK.
- Same deploy: booking source labels (الحجز جه منين…).
- Saged has not tested it on the live site yet.

## Decisions
- "Owed" at booking = visit fee + old unpaid bills − credit held. Paying old debt at booking is still fine; more is refused (409 over_balance with most).
- The API ignores a stray `keep_credit` key. It does not reject it, because old tabs still get 409.
- No `max` on the amount input: it is type=text, so `max` would do nothing.
- Credit still comes from a discount after paying or a removed procedure, and it still pays the next booking.

## Key paths
- `v2/billing/services.py` (`plan_booking_money`), `v2/scheduling/views.py` (`_payment`), `v2/scheduling/services.py:~170`
- `v2/web/static/js/credit.js`, `book.js`, `walkin.js`, `v2/web/templates/web/_money_box.html`, `_strings.html`
- `v2/billing/test_credit.py` (`with_credit` helper: pay fee, then owner discount)
- Arabic: `v2/locale/ar/LC_MESSAGES/django.po` (CRLF; Git Bash sed strips CR, so restore CRLF), then `manage.py compile_translations`
- Tests: `cd v2; export PATH=/c/work/pgsql/bin:$PATH PSYCOPG_IMPL=python; .venv/Scripts/python.exe manage.py test --noinput --failfast -v 1`. The full suite takes ~12 min, so run it in the background.
- Deploy: deploy script (git archive, migrate_all on Neon, vercel --prod); open https://dento-jet.vercel.app in Brave.

## Next step
1. Build the approved booked-service change (below), then run the full panel review.
2. Build the visit-summary WhatsApp button (below).
- Small deferred items from the chief judge:
  - The over box shows "خليها 0" when credit covers everything.
  - The over sentence shows twice on submit (form error + box role=alert).
  - A 0-cash, credit-only booking still opens the cash shift.

## Approved by Saged 2026-11-27: booked-service change (money + UI → full panel)
- If the doctor removes the booked line and adds nothing, finishing the visit is blocked.
- Remove the "غيّر الحاجة المحجوزة" box at `web/templates/web/visit.html:107-115` (added in commit 3130aba).
- Give the booked line an ✕ like the procedures.
- When a change leaves money paid over, the patient file's "ليه مردود" says "اتغيّر من X لـ Y", and Hamada gets an alert.
- The exam setting already exists: `consultation_free_with_procedure` in clinics/settings.py.

## Approved by Saged 2026-11-27: visit-summary WhatsApp button
- Remove the "ابعت الخطة واتساب" icon in the next-visits part.
- At the end of the visit screen, add one button "ابعت ملخص الزيارة للمريض". It opens wa.me with the patient's phone and: what was done today, the prescription, the upcoming plan. Names only, no prices, nothing else.

## Open questions
- Fixed deposit (Saged, 2026-11-27): a deposit must never be bigger than the visit's fee. Hamada uses a fixed 200 and nothing costs less, so this won't happen in practice. Proposed guard, not yet confirmed: the deposit asked = the smaller of the fixed amount and the fee (`scheduling/services.py:91-96`). Write a failing test first.
- Should waste from opening stock count as a real loss? unknown
- Later reminders: before/after photos; feature shortlist (B then A); inventory; phone navigation + mobile preview.
