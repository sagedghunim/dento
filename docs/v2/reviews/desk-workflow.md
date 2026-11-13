# Front-desk workflow review (receptionist's view)

Date: 2026-11-13. Reviewer: senior-frontend, acting as the evening receptionist of a busy dental clinic.
Scope: `web/templates/web/today.html`, `_row.html`, `_dialog_*.html`, `_when.html`, `web/static/js/today.js`, `book.js`, `walkin.js`, `pay.js`, `visit.js`, `web/pages.py`, `scheduling/views.py` (finish, walk-in).
Review only; no code was changed.

## How this was checked

Verified by running code (dev server on 8002, demo data, signed in as `sara`, headless Edge at 1280 px, DOM inspection, no screenshots):

- Today had 31 rows over 2 doctors: 14 done, 2 in room, 2 `ready`, 7 waiting, 6 booked.
- Rows with status `ready` render the red pill "Didn't come" (لم يحضر) with an "Owes / Collect" button, and no queue button.
- Each doctor's list starts with the done rows (d1 to d7), so the first waiting patient is about 7 rows (about 650 px) below the top of the list. Each row is 95 px tall and the first row starts at y = 527.
- The quick field "Patient name or mobile" with "010" and Enter opens the Book dialog with 20 results. Each result shows the file number, mobile, age, sex and last visit. No result shows a balance or a next booking.
- After picking a patient in the Book dialog with 2 doctors and no doctor filter, no doctor is chosen, so no fee and no slots show. "The patient is here now" and "Collect now" are both ticked by default for today. "Booked by" defaults to "At the clinic". The dialog body is 2,535 px tall.
- The patients search finds by phone digits ("0110703").

Inferred from reading the code (not run):

- There is no button to mark a patient as a no-show. `no_show` is set only by the nightly sweep in `scheduling/jobs.py`.
- `POST /appointments/{id}/finish` accepts `earlier` (collect the patient's older debt) and `next_visit` (book the next visit) in one request (`scheduling/views.py` `finish_view`). The finish dialog in `visit.js` sends neither, and never shows the older balance that the answer carries.
- `book.open({patient})` already supports a preselected patient. Nothing on a row uses it.
- The walk-in dialog has no duplicate-phone check for a new patient. The Book dialog has one.
- `data-open-walkin` opens the walk-in dialog empty, even when a name is already typed in the quick field.
- Phone numbers on rows and in results are plain text, with no `tel:` or WhatsApp link.

## Taps for the common jobs today

Counts are taps plus typing, for an assistant with 2 doctors, starting from Today.

| Job | Now | After the top 10 |
|---|---|---|
| Phone call, existing patient, tonight at 8 | type + Enter, pick (1), doctor (1), slot (1), "By phone" (1), untick "here now" (1), untick "Collect" (1), Book (1) = **7 + typing**. Missing either untick records a false arrival or false cash. | type, pick, doctor, slot, Book = **4 + typing** |
| Phone call, new patient | the above + "New patient" (1) + name + mobile + sex (1) + age = **9 + typing** | **6 + typing** |
| Walk-in, existing patient, pays | "Walk-in now" (1), retype the name, pick (1), tick "Collect" (1), "Add to the queue" (1) = **4 + typing twice** | **3 + typing once** |
| Mark arrived / call in | **1** | 1 |
| Finish and collect in full | Finish (1), "Finish and collect" (1) = **2** | 2 |
| Partial payment | Finish (1), edit the amount (typing), "Finish and collect" (1) = **2 + typing** | same |
| Collect an old debt as well | Not possible from Today. Patients, search, open the file, find the old visit, Collect, amount, Collect = **about 6 + typing**, on another page. | Ticked in the finish dialog: **2** |
| Book the next visit of a treatment plan | Book, search the same patient again, service, doctor, "Another day", date picker, slot, Book = **about 8 + typing** | "Next visit" in the finish dialog or the row menu: day, slot, Book = **3 to 4** |
| Change the time | ⋯, "Change the time", day, slot, "Move the booking" = **5** | same |
| Cancel | ⋯, "Cancel booking", confirm = **3** | same |
| No-show | **Impossible.** The row stays "Booked" until the nightly sweep. | ⋯, "Didn't come" = **2** |
| Find a patient | "/" or tap the field, type, Enter = **1 + typing** (good) | same, with the balance shown |

## Top 10 improvements, ranked by impact on desk speed

### 1. Rows in `ready` or `with_assistant` say "Didn't come" (bug)
- **Now:** `_row.html` handles booked, waiting, in_room, done and cancelled, and every other status falls to the `else` branch, which shows "لم يحضر". The row also shows no queue button. Verified on 2 demo rows. The receptionist sees a patient who is sitting in the chair marked as absent, in red, and stops to ask the doctor.
- **Proposed:** add pills for "With the assistant" and "Ready for the doctor", and offer "Call in" on `ready` (the action map in `today.js` `quick()` already takes any action). Make `else` match only `no_show`, and add a template test that renders every status in `STATUSES`.
- **Effort:** S

### 2. Booking for today defaults to "here now" and "Collect now"
- **Now:** when the day is today, `_dialog_book.html` ticks both boxes, and the amount is prefilled with the fee. During the evening rush most bookings for today are phone calls for a later hour. If the receptionist forgets the 2 unticks, the patient joins the waiting list and a cash payment is recorded that nobody took, and the shift's cash won't match.
- **Proposed:** tick both only when the booking is "now". That means a queue session with no time, or the "At the clinic" source. The walk-in button already covers "here now", so another option is to leave both unticked always. Choosing "By phone" or "WhatsApp" clears them.
- **Effort:** S

### 3. Show the old balance and collect it in the same finish
- **Now:** the row shows only this visit's "عليه". Search results and the finish dialog show no older debt. The server already returns `balance` and accepts `earlier` in `finish_view`, but `visit.js` uses neither. Collecting an old debt takes about 6 taps on the Patients page, so in practice it gets forgotten.
- **Proposed:**
  - A small grey line on the row and in search results: "عليه من قبل ٣٠٠ ج".
  - In the finish dialog, one checkbox "Also collect the earlier ٣٠٠", ticked by default and editable, that sends `earlier` in the same request.
  - In the walk-in and Book dialogs, show the balance next to the picked patient.
- **Effort:** M. Rows need the balance in the today selector: one grouped query per page, not one per row.

### 4. "Next visit" without searching for the patient again
- **Now:** a treatment plan needs a return visit every 1 to 2 weeks. After finishing, the receptionist reopens Book, types the name again, and goes through doctor, "Another day", the date picker and a slot. `finish_view` already accepts `next_visit` atomically, and `book.open({patient})` exists.
- **Proposed:**
  - Quick win: a "Book next visit" item in the row's ⋯ menu that calls `book.open({patient, doctorId})`, with day chips "+1 week" and "+2 weeks" next to Today and Tomorrow.
  - Full fix: a "Next visit" section in the finish dialog with the next open plan item as the service (from `clinical.selectors.plans`), sent as `next_visit`.
- **Effort:** S for the menu item, M for the finish dialog with the plan item.

### 5. Put the people who need action at the top
- **Now:** each doctor's list is ordered by turn, so 7 done rows (95 px each) come before the waiting patients. At 1280 px the first waiting patient is below the fold. A receptionist scrolls on every call-in.
- **Proposed:** order rows as in the room, ready, waiting, booked, then done. Collapse the done rows into "Finished (٧)", expanded in one tap; keep "Collect" visible on finished rows that still owe. Keep the turn number visible. The row-anchoring logic (R8) already handles moved rows.
- **Effort:** S to M (ordering in the selector or the template, plus a `<details>` for done rows).

### 6. A shorter Book dialog with real defaults
- **Now:**
  - With 2 doctors and no filter, no doctor is picked, so no fee or slots appear until a tap.
  - "Booked by" always starts on "At the clinic".
  - The dialog is 2,535 px tall: patient, sex, section, service, doctor, day, time, source, arrive, collect, method and print.
- **Proposed:**
  - Preselect the doctor that has the earliest free slot today, or the last doctor this patient saw.
  - Remember the last source used in this browser (localStorage, as "print" already is).
  - For a returning patient, preselect "Follow-up" when the quote says it is free.
  - Show the first 6 free slots as large chips at the top of the time group.
  - Move "Booked by" and "print" under a small "More" fold.
- **Effort:** S to M.

### 7. Mark a no-show by hand, and make phones tappable
- **Now:** there is no "Didn't come" action. At 9 pm the 6 booked rows still look like patients to wait for, the counts on the cards are wrong, and the doctor asks "who is left?". Phone numbers are plain text, so calling the late patient means retyping the number on the desk phone.
- **Proposed:**
  - Add "Didn't come" (لم يحضر) to the ⋯ menu of `booked` rows on today, past the booking time. It needs a new endpoint `POST /appointments/{id}/no-show` with `appointments.queue`. Offer an undo in the toast.
  - Render phones as `<a href="tel:">` and add a small WhatsApp link (`https://wa.me/20…`). This is a link, not a script, so no CDN is involved.
- **Effort:** S (frontend) plus S (backend endpoint and test).

### 8. WhatsApp confirmations and reminders, as the desk already works
- **Now:** Egyptian clinics confirm bookings and remind the day before on WhatsApp, by hand. The product records "WhatsApp" as a source and does nothing else.
- **Proposed:**
  - After a booking, the success toast offers "Send on WhatsApp". It opens `wa.me/<phone>?text=` with a message the clinic can edit in Settings: name, day, time, doctor, address and a map link.
  - On tomorrow's list, a "Reminders" button walks through the booked rows one tap each.
  - No API cost or approval is needed now. The WhatsApp Business API stays on the agreed backlog.
- **Effort:** M (settings text, template filling, a tomorrow view action).

### 9. Search results that answer the receptionist's questions, and families on one phone
- **Now:** a result shows the file number, phone, age, sex, last visit and "Booked today". It doesn't show whether the patient owes money, has a booking coming up, or has an open treatment plan. Searching a family's shared mobile lists every member with no grouping. The walk-in dialog doesn't warn that a phone is already on a file.
- **Proposed:**
  - Add to each result: "عليه ٣٠٠", "Next: Sun 7:30", "Plan: 2 items left", and "New" for a first visit.
  - When several results share the searched phone, group them under the number ("٣ على هذا الرقم") so the right family member is one tap away.
  - Reuse the Book dialog's `checkPhone` in `walkin.js`.
- **Effort:** M (the search selector must add the balance and the next booking in grouped queries, keeping the 20-row page).

### 10. One search box that leads straight to the action
- **Now:**
  - The quick field searches nothing until Enter, then repeats the search inside the Book dialog.
  - "Walk-in now" ignores the text already typed, so the name is typed twice.
  - With one result, the receptionist still has to tap it.
- **Proposed:**
  - Show live results under the quick field (the same `/patients/search?pick=1`), each with 2 buttons: "Book" and "Walk-in", plus "Collect" when the patient owes.
  - Pass the typed text to the walk-in dialog.
  - Enter on a single exact phone match picks it.
  - "/" already focuses the field. Add Alt+N for a new booking and Alt+W for a walk-in, which work on the Arabic layout by key position, as "/" does.
- **Effort:** M.

## Quick wins (each S, about a day or less)

1. Fix the `ready` and `with_assistant` pills and the Call-in button (item 1).
2. Leave "here now" and "Collect now" unticked unless the booking is for now (item 2).
3. Add "Book next visit" to the row ⋯ menu with the patient prefilled (item 4, first part).
4. Make phones `tel:` links and add a WhatsApp link (item 7).
5. Remember the last "Booked by" source, and preselect the doctor when a filter or a single free doctor makes it obvious (item 6).
6. Pass the quick-field text to "Walk-in now" (item 10).
7. Add the duplicate-phone check to the walk-in dialog (item 9).
8. Gender-neutral Arabic labels:
   - "وصل" reads wrongly for most dental patients, who are women. Use "تم الحضور" or "حضور".
   - "عليه" has the same problem. Use "مستحق" or "باقي".
   - "Turn" shown as "الدور ١" next to a clock time is clear. Keep it.
9. "إنهاء، والمبلغ مؤجَّل" is clear. Do the same for "Collect": "تحصيل" fits better as "استلام المبلغ" on the main button of a finished row.

## What is already good (keep it)

- Arrive and call-in take one tap, with an undo toast. A double tap does nothing: the action is held by key and disabled while it is saving.
- Rows stay under the finger while the list updates live (R8).
- Finish and collect is one request. The answer decides whether "Collected" is shown.
- Walk-in needs no time and lets the server pick the doctor and the service. "أول طبيب يفضى" is excellent wording.
- Search covers name parts, phone and file number in pages of 20. "/" focuses the search box.
- The Book dialog's duplicate-phone step for new patients never blocks the booking.
