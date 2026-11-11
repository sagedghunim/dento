# Hamada v2, M1 and M1.5: front-end review

Date: 2026-11-11. Reviewer: senior frontend.
Scope: `v2/web/` and `v2/locale/` at d56ad47 (`git diff 794ae18 d56ad47 -- v2/web v2/locale`): the shell, Today, the Book, Pay, Finish, Cancel and Move dialogs, patients, receipts, sign-up, the clinic picker, translations and fonts.
Specs: requirements section 1; design 5.11, 7 and Appendix D; the demo (`docs/v2/model/hamada-app.html`, `docs/v2/screens/demo-*.png`).

## Verdict: revise

The base is good. The request-key plumbing, the single poller with ETag and 304, the "Arrived" and "Call in" toasts that name the patient with an undo, bidi isolation of phones and money, contrast and printing all work when I run them. The screens follow the demo's sections, cards and colours.

Four problems block approval:
- B1: a save whose outcome was unknown is sent again silently at the next page load.
- B2: the finish dialog says "paid" when no money was recorded.
- B3: rows move under a resting finger, so a tap can call in the wrong patient.
- B4: the Pay dialog offers to remove the visit's own service line to a user without the discount permission.

B2 and B4 are the screen half of the backend review's B1 and B2. B1 and B3 come from the screens alone.

## How this was checked

**Run:**
- My own server on 127.0.0.1:8006, on my own databases (`hamada_rfe_dir`, `hamada_rfe_s001`, `hamada_rfe_s002`), migrated and seeded with `seed_demo`. I signed in as `sara`, the assistant, in the dental clinic, which has two doctors.
- Headless Edge driven over the DevTools protocol, with real mouse and touch input, offline mode, cookie deletion, page freezing and CPU throttling. The scripts are outside the repo.
- `manage.py test web` with `HAMADA_TEST_DB_SUFFIX=_rfe`: 30 tests, OK. The first attempt died during test-database setup with `canceling statement due to statement timeout`. A rerun passed.

**Not run:** anything marked "not run" below.

## Blocking

### B1. A save with an unknown outcome is replayed at the next page load, after the person has already redone it: two files, two bookings, two payments

**Where:**
- `web/static/js/book.js`, `open()`: it calls `submitter.reset()`, which makes a new key.
- `dialogs.js`, `wireClose`/`closeDialog`: closing is blocked only while a send is in flight.
- `shell.js:83`: it calls `settlePending()` on every page.
- `api.js`, `settlePending`: it resends every stored entry less than 24 hours old.

**What happens:**
1. A booking times out, and the dialog says "not confirmed, try again".
2. The receptionist closes the dialog. Nothing on screen still shows the request: the pending bar is drawn only by row actions. The entry stays in `sessionStorage`.
3. She books again, which gets a new key and succeeds.
4. Later she opens any page, here Patients. The old entry is resent with its old key, and that key never reached the server, so it creates a second booking.

The same applies to Pay (payments, lines, discount), Finish, Cancel and Move: every `Submitter` dialog that is closed while unknown. Row actions are safe here only because their body carries the expected status.

**Evidence (run, `e2_replay.mjs`):**
- I booked a new walk-in with "collect now" at 300 EGP and went offline before sending. After 20 s the dialog showed "لم نتأكد من الحفظ…" and the stored entry count was 1.
- I closed the dialog, went back online and booked the same person again: toast "تم الحجز: مريض تجربة، الدور 8".
- I opened `/patients`: toast "تم حفظ العمليات السابقة."
- The result was two patient files (file 10 and file 11), two bookings (turns 8 and 9), and both show `data-due="0"`. That is 600 EGP recorded for 300 received.

**Fix:**
- While a dialog's outcome is unknown, closing it must not drop the request from view:
  - either block the close and show only "Try again" and "Discard" (design 7.4);
  - or, on close, move the entry into the page-level pending bar with the patient's name and amount, the same bar row actions use.
- `open()` must not start a new key while an unknown entry exists for that dialog. Reopen it in the unknown state instead.
- At page load, don't resend non-idempotent bodies (bookings, payments, lines) silently. List them as "not confirmed: Booking, Mona Ali, 300 EGP" with "Try again" and "Discard". Auto-resend only within a few minutes of the original send.
- Test in the browser: unknown, close, rebook, navigate. Assert one booking.

### B2. "Finish and collect" shows "collected" from what was sent, not from the answer

**Where:** `web/static/js/visit.js:306-308`, `const paid = result && isOk(result) && body.payment;`.

**Evidence (run, `e3_finish.mjs`):**
- The desk opens Finish for عمر خليل, who owes 300.
- The doctor finishes the visit from another screen (200).
- The desk taps "Finish and collect". Toast: "انتهت الزيارة: عمر خليل، وتم تحصيل 300 ج.م".
- After the poll, the row is `done` and the pill says "عليه 300 ج.م".

The cash is in the receptionist's hand and the system has none of it. She believes the toast, which is gone after 4 s.

**Fix:**
- Decide from the answer. The 200 already carries `appointment.invoice`. Show "collected" only when `invoice.paid` rose by the amount, or when the answer carries the new payment's id. Otherwise keep the dialog open with "The visit was already finished. Nothing was collected" and a "Collect now" button that goes to the payments endpoint with a new key.
- Handle the backend's planned 409 (`already_finished`) the same way.
- Apply the same rule to `book.js`: "booked and collected" should come from `appointment.invoice.paid`.

### B3. Rows move under a resting finger: a booking for one doctor shifts the other doctor's list, and the tap lands on another patient

**Where:**
- `today.js`, `apply()`, `patchGroups()` and `patchRows()`.
- `hot()` protects only for 2.5 s after the last touch, and 1.5 s after the last mouse move.
- `held()` keeps a hovered or focused row from being redrawn, but nothing keeps any row in place when rows above it are inserted, removed or change height.

**Evidence (run, `e1_shift.mjs`, 390 × 844, touch):**
- With "all doctors" shown, I scrolled so that د. أحمد سامي's first row (كريم عادل, "وصل" button at y = 477) was mid-screen. Then I waited 3 s with no touch, the finger resting.
- Another screen booked a walk-in for د. حمادة عزيز. After the next poll, كريم عادل's row had moved down 213 px.
- The point where the finger was is now on the new walk-in row "زائر جديد", just under its "استدعاء" (Call in) button. A few pixels higher, the tap meant to mark كريم عادل as arrived calls in a different patient.
- The same happens on desktop when the mouse rests for more than 1.5 s.
- Separately, Arrived, Call in and Finish take the same spot in a row. Run with the mouse (`e8c.mjs`): a second click at the same spot after the row redrew called the patient in.

**Fix:**
1. **Anchor.** Before patching, pick the anchor: the row under the last pointer position or with focus, else the first fully visible row. Record its `getBoundingClientRect().top`, patch, then `scrollBy` by the difference. Browser scroll anchoring doesn't cover this case, because the inserted row was below its chosen anchor.
2. **Tap guard.** Mark a row with `movedAt` when its position or its action buttons change. In the click handler, ignore a `data-act` tap within about 700 ms of `movedAt`, and flash the row instead.
3. **One doctor by default on narrow screens.** Design 7.3 asks for per-doctor tabs on small screens. Today opens on "all doctors" (`today.js:339` only defaults doctors to their own queue). Default the desk to the doctor of the last booking, or the doctor working now.
4. Add a headless-browser test of the E1 scenario.

### B4. The Pay dialog offers "Remove" on the visit's service line to a user without `billing.discount`

**Where:** `web/static/js/pay.js:49`. Removal is offered when `perms.collect && open && invoice.lines.length > 1`.

**Evidence (run, `e4_pay.mjs`):** sara has `collect: true, discount: false`. I opened "Bill and payments" on a paid visit and added one line. Both lines, including the visit's own consultation, now show "حذف".

**Fix:**
- Ship with the backend's B2.
- Show "Remove" only on lines this person may remove: with `perms.discount`, or on a line they added that business day if the backend adopts that rule. Never on the `kind = 'service'` line created with the visit.
- Handle a 403 by reloading the invoice and saying why.

## Should fix before M2

### S1. An expired session throws away the open dialog (not the overlay in design 7.4)

`shell.js:77-81` sends any 401 to `/login?next=…`. Run (`e5_expiry.mjs`): with the Book dialog half filled (name and mobile typed), I deleted the session cookie. Within 9 s the tab was on `/login?next=%2F` and the typed details were gone.

There is also no warning 10 minutes before `session_expires_at` (grep finds no `expires` in `web/`).

Saves already sent are kept and replayed after sign-in, which is good, but see B1.

Fix: the overlay over the intact page, as designed (password, then the code), returning to the same membership. Until then, at least stop the poller from navigating away while a dialog is open.

### S2. A frozen or hung polling tab starves the others, and "Try now" does nothing

Run (`e7_poller.mjs`):
- Two Today tabs. One poller ran: one fetch, and the second tab updated from the channel. That part is right.
- I froze the polling tab (`Page.setWebLifecycleState frozen`, as sleeping tabs do) and booked a patient.
- After 20 s the visible tab was stale: "لم يتحدث منذ 11:49 ص. جارٍ التحقق…". It said "checking", but no tab sent a request.
- "Try now" sent nothing either. `refreshNow()` in a follower only posts `poll-now` to the frozen leader.
- It updated only after the leader was unfrozen.

Not run: whether Edge's sleeping tabs actually freeze a tab that holds a Web Lock.

Fix:
- A follower that has had no message for two intervals, or whose person presses "Try now", polls itself.
- Or it takes the lock with `navigator.locks.request(name, {steal: true}, …)`, and the old leader stops when its lock promise rejects.

### S3. A busy day is heavy on a cheap laptop

I booked 200 visits on one day. Run (`e15_load.mjs`, `curl`):

| | Measured |
|---|---|
| `/today/queue` | 366 KB, about 1.8 KB a row. 11 KB gzipped. |
| Today's HTML | 400 KB |
| CPU at 1× | One update: one 93 ms long task |
| CPU at 4× | First paint 1.95 s, DOMContentLoaded 7.6 s, and a **656 ms** main-thread block on every change |

At 4× (a 2016 i3 or a cheap tablet), every change in a busy center freezes the desk for about 0.6 s while someone is tapping.

The cost is:
- every row carrying its full `<details>` menu;
- `outerHTML` string comparisons per row;
- `querySelector` per row inside loops, which is O(n²);
- `applyFilters` and `stats` over every row after each change.

Fix:
- Give each row a `data-h` hash from the server and compare that.
- Render the "more" menu once and fill it on open.
- Index rows in a `Map`.
- Consider a delta fragment (rows changed since version N).
- Confirm that Caddy compresses `text/html` fragments. Not run: whether Caddy's `encode` changes the ETag. A weak `W/"…"` already matches (run), but a `"…-gzip"` suffix gets 200 (run).

### S4. Rows are about twice as tall as the demo's

At 1280 px, v2 rows are 125–155 px, because the status pills and the buttons wrap onto a second line (`today1280.png`). The demo's are about 70 px, one line (`demo-1280.png`). The demo shows about seven rows above the fold and v2 about three. More scrolling also means more of B3.

Fix: at 900 px and wider, keep pills and the main action on the main line. Fold "Owes 300" into the Collect button ("تحصيل 300"), and move phone and file into the small second line.

### S5. Sign-up shows one error per submit, partly in English

Run (`e12_signup.mjs`): a form with four mistakes took four submits to clear:
1. `name: "name is required"`, in English on the Arabic page;
2. mobile;
3. password;
4. username taken (409).

Also:
- With no specialty picked, the error appears at the bottom and focus stays on `BODY`, far from step 2.
- While the outcome is unknown, `signup.js` leaves the fields editable but resends the old body. Someone who corrects the username during "not confirmed" sends the old one.

Fix:
- Check required fields, mobile format, username pattern and the 10-character password in the browser, all at once, before sending.
- Map server field codes to Arabic messages.
- Focus and scroll to the first problem.
- Lock the fields while the outcome is unknown, as `Submitter` does.

### S6. Row actions after an unknown outcome, and refreshes lost while a poll is in flight (not run)

- `today.js`, `quick()`: after an unknown outcome the row's buttons are enabled again with the old status, although design 7.4 keeps them disabled while pending. The status precondition makes a second tap harmless (409), but the person sees an error.
- `poll()` returns early when `inFlight`. A `changed()` right after an action can then wait a full interval (5 s) before the row shows its new state. Keep a `pollAgain` flag and poll once more when the in-flight one ends.

### S7. No browser tests

`web/tests.py` has 30 server-side tests and none run the JavaScript. Every B and S item above was found only by driving a browser. Add a small headless-Edge smoke suite for:
- booking (a double tap, offline then unknown);
- E1;
- session expiry;
- two tabs and one poller;
- the receipt PDF size.

## Nice to have

1. **Taps per walk-in:**
   - In a two-doctor clinic with "all doctors", a known patient takes Book, type, pick, **doctor**, Book and collect: four taps and the typing.
   - Pick the doctor by default: the open tab, else the only doctor working now (`e13_book390.png`).
   - Default paper is A5 (`receiptUrl`). Most desks with a thermal printer want 80 mm. Make it a clinic setting, not per-browser `localStorage`.
2. **Finish with no undo:** a doctor without `billing.collect` finishes in one tap with no confirmation and no undo. Offer undo like Arrive and Call, or confirm with the name.
3. **Patient search** stops at 20 results with no "more" and no "type more to narrow" (run: "مريض" matched 190 and showed 20).
4. **Type sizes:**
   - Secondary text is under the design's 14 px floor: `.muted` 13.6 px, pills 13.1, the turn label 12.8, "Soon" 12.
   - On the 80 mm receipt, the "Printed" line is 8.9 pt, under the 10 pt floor (run, `e11_print.mjs`).
5. **Accessibility:**
   - The error toast (`#alert`, `role="alert"`) closes only by mouse. Make it a button or add a close button.
   - When the row that opened a dialog was redrawn meanwhile, focus falls to `body` on close. Return it to the same `data-act` in the new row.
   - `pay.open` and `book.open` from Today take `document.activeElement` as the opener. Safari doesn't focus buttons on click, so pass the button.
6. **Fonts:**
   - Manrope (24 KB) carries the Western digits but isn't preloaded, so numbers swap after load. Preload it, or check the swap is invisible.
   - Design 7.1 still says "no web fonts". Record the decision to vendor Rubik and Manrope to match the demo.
7. **Shell:**
   - The language switch reloads even when the save failed.
   - Sign-out goes to `/login` even when the request failed, so the session lives on and a shared PC looks signed out when it isn't. Show an error instead.

## Checked and fine (run unless marked)

- **Poller:**
  - `/today/queue` answers 304 to its ETag and to the weak form, with `Poll-After: 5`, `Cache-Control: no-store` and `X-Server-Time`.
  - One tab polls and the other updates from the channel.
  - A 401 stops polling and doesn't say "connection lost".
- **Double tap:** two real clicks 250 ms apart on "Arrived" sent one POST and showed one toast naming the patient. While the mouse rested on it, the row stayed as it was and was redrawn when the mouse left.
- **Keys:** `Idempotency-Key` is echoed on 2xx. The `getRandomValues` fallback has the right UUID v4 bits. Passwords are never stored (sign-up uses `send`, not `save`). Pending entries are filtered by user and membership.
- **Printing:**
  - The print tab opens inside the tap and goes to `/print/receipts/<id>?format=a5&print=1` after the save (`e16_printtab.mjs`).
  - Receipts print on one page: 80 × 133 mm (Arabic) and 80 × 139 mm (English) on the roll, and 148 × 210 mm on A5.
  - Titles are "إيصال 3" and "Receipt 3", with no patient name.
- **Arabic and right to left:** phones read "011 2345 6789" in RTL text, money as "300 ج.م", times as "11:42 ص". Data is in `<bdi>` with `dir="auto"` for names.
- **Contrast** (computed, both themes): every pill, muted text and button is from 5.08:1 to 9.5:1.
- **Look:** the side navigation, cards, stat tiles, colours and type follow the demo. Sign-up's four steps and the specialty cards look right.
- **CSP:** strict, and no inline styles or scripts are needed.

## Cleanup

I stopped my server on port 8006 and dropped `hamada_rfe_dir`, `hamada_rfe_s001` and `hamada_rfe_s002`. The test databases (`test_*_rfe`) were destroyed by the test run. I didn't touch port 8002 or the dev databases.
