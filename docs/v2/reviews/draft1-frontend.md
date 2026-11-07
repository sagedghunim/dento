# Hamada v2 design, draft 1: frontend review

Reviewer: senior-frontend, 2026-11-07. Reviewed `docs/v2/design.md` (draft 1, all of it; sections 6 and 7 in detail) against `docs/v2/requirements.md` and `CLAUDE.md`. Domain references: the prototype (`static/app.js`, `static/styles.css`, `clinic/templates/app.html`), the first demo (`C:\work\hamada-app`) and the warehouse v2 (its design section 9, `v2/static`, templates). I did not edit the design, the requirements or any code. Scratch tests ran in my scratchpad only.

**Verdict: revise.** The polling model, request keys, the server deciding "today", integer money, server-rendered print pages and the no-CDN rule are right. Missing: the browser's half of safe repeats, the session and day boundaries, numbers in right-to-left text, print sizes that work, and a one-step walk-in. There are 12 blocking problems. I also recommend a different page architecture (answer 1). Where the backend review (`draft1-backend.md`) covers the server half of a problem, I cite it as backend Bn and cover only the screens.

## Answers to the eight questions

1. **Architecture. My pick: server-rendered Django pages plus a few small plain-JavaScript modules. No HTMX, no framework, no build step.**
   - Why it fits this team and these devices:
     - One translation catalogue, and the server formats times, digits and money (most of blocking 7 goes away).
     - Pages are testable with Django's test client on Windows. The SPA has no client test plan in section 10.
     - The same process built the warehouse v2 this way: 6 modules, 39.6 KB of JS and CSS together (measured).
     - Each page load gets the current hashed assets, so only Today, which stays open all evening, needs the version check (blocking 12).
     - Links, forms, Back and focus after navigation work without a client-side router. On old PCs and cheap phones, server HTML is the lightest page.
   - How: the JSON action endpoints under `/api/v1/` stay as designed. Pages save through them with `fetch` and one `api.js`, and native apps use them later. Today is a page whose queue is an HTML fragment, polled with the ETag and patched row by row (blocking 4). Book, Pay and Prescription are `<dialog>`s. A fragment is rendered only when the version changed; a 304 costs the same as with JSON.
   - Not a framework: a Node toolchain and a build on Windows, 40–150 KB more script on old PCs, and a second skill set for a Django team, for screens that are mostly forms and lists. Not HTMX: ETag polling and request keys need custom code anyway, and the warehouse ended up not using it.
   - If the SPA stays, everything below still applies. Add a JS catalogue (answer 2) and browser tests for every page.
2. **Translations: one `.po` file, no GNU gettext.**
   - `makemessages` and `compilemessages` both fail here without gettext. polib compiles the `.po`, and Django loads the result (both verified).
   - With server-rendered pages, the few strings JS needs come from the page (`data-*` attributes or `json_script`), so there is no JS catalogue.
   - Replace `makemessages` with a test: extract `{% translate %}`, `gettext`/`_` and `pgettext` strings by regex, and fail when `ar` lacks one or keeps an unused one.
   - If the SPA stays: put JS strings in `djangojs.po` and serve them with `JSONCatalog`, with the release in the URL. It serves polib-compiled strings, context and Arabic's 6 plural forms (verified). Not `domain="django"`, which also ships Django's own 447 strings (41 KB).
   - Either way: don't key by English text ("Book" the verb and the noun differ in Arabic: use `pgettext`). Prefer "label: number" (`في الانتظار: 3`) over plural sentences.
3. **Live updates.** Conditional polling is right for 100,000 screens and this team. Keep it rather than SSE or WebSockets (12.4). As written, though, it multiplies with tabs, shows old data after sleep without saying so, can't be slowed once shipped, and breaks at midnight: blocking 5 and 6. On clocks: the version is server-side (good); nothing else may use the browser's clock (blocking 6 and 7).
4. **Resilience.** Work is done twice in blocking 1, and lost in blocking 2 and 3. A save can spin for 40 s (4 attempts × 10 s). And "wasn't saved" (7.4) is often false.
5. **Speed** (counted from 7.3, with the defaults the design leaves open).
   - Today's row actions are one tap each, which is right.
   - Returning walk-in (book, arrive, collect, print): 13–16 taps and 3 requests in a row. A new walk-in adds the name, an 11-digit phone, the age and the sex.
   - Collecting from a booked patient on arrival: Arrive, Pay, Save, Print, then the browser's Print: 5 taps and 2 requests.
   - Finding a patient by phone: `/` or a tap, digits, a tap. Prefix search narrows only after 5–6 digits, because the first 3 are the carrier code.
   - Targets: blocking 10, improvements 2–4.
6. **Arabic and right to left:** blocking 7, 8 and 9; improvements 9 and 10.
7. **Accessibility and fonts:** improvements 8 and 9. The system font stack with no web fonts is right for the app.
8. **API:** blocking 10 and 11; improvements 5 and 6.

## Blocking problems

**1. Safe repeats, the browser's half: a resend after "not saved" books or charges twice (7.1 `api.js`, 7.4, 6.1).** The server half is backend B4.
- **Breaks on** a weak 3G or Wi-Fi link where the request reaches the server and the answer is lost.
  - After 3 retries the form says it wasn't saved. The receptionist taps Save again, `crypto.randomUUID()` makes a new key, and the payment, the booking (with a second turn number) or the new patient file is made twice.
  - A reload or a closed tab during a save loses the key the same way.
  - On plain http to a LAN address (testing on a real tablet), `crypto.randomUUID` doesn't exist (verified), so every create fails.
- **Fix:**
  - The key belongs to the form: made when the dialog opens, reused for every send until a definite answer (a 2xx, or a 4xx that wrote nothing).
  - "Keep the key until the content changes" isn't enough. After a timeout or a network error the outcome is unknown: lock the fields, say "Not confirmed yet, checking", and resend the same key and body with backoff. Offer only "Try again" and "Discard".
  - Store `{url, key, body, user, membership}` in `sessionStorage`, cleared at sign-out; never patient data in `localStorage`. After a reload or a new sign-in, settle it before anything else (as the warehouse's 9.4 does).
  - Every answer echoes the key; "Saved" shows only for a matching echo.
  - Retry only after network errors, timeouts, 502/503/504, and 429 after `Retry-After`; never after a 500. Show "still saving" after 3 s; stop retrying after about 20 s in all.
  - If `crypto.randomUUID` is missing, build the key from `crypto.getRandomValues`.

**2. The doctor's note and prescription draft can be lost or silently overwritten (5.5, 5.6, 7.3 Visit).**
- **Breaks on** a doctor's tablet on weak Wi-Fi, a second device, or the browser discarding a background tab.
  - The note is a PUT where the last writer wins. A timed-out save resent later, or an older copy on the doctor's phone, overwrites newer text without a word.
  - The draft prescription and the note live only in page memory. A reload, a discarded tab or a 401 (blocking 3) loses minutes of typing.
- **Fix:** the note PUT sends `If-Match` with the note's version and gets 409 with the current text, and the screen shows both to merge. One save in flight per note; send the latest text when it returns. Keep drafts in `sessionStorage` per appointment until saved. Discounts and stock counts are absolute values with the expected old value (as backend B4).

**3. When a session ends, the sign-in overlay loses work or replays it as someone else (5.9, 7.1, 8).**
- **Breaks on** a reception PC signed in at 09:00 whose 12-hour session ends at 21:00, mid-peak; a password change that signs out other sessions; a shared PC where another receptionist signs in on the overlay; several tabs.
  - `login()` rotates the CSRF token (verified in Django 6.1's source). If `api.js` reads the token once at load, every save after signing in again fails with 403, and Django's default CSRF failure answer is an HTML page, not the error JSON.
  - Sessions don't slide by default (`SESSION_SAVE_EVERY_REQUEST = False`), and must not, or a polling screen never expires. So the end time is fixed and falls mid-shift.
- **Fix:**
  - The overlay sits over the intact page. It handles the password, the code and a forced password change, and returns to the same clinic (`POST me/clinic` with the previous id) without the picker.
  - After sign-in, replay the stored request (blocking 1) only if the same user and membership came back. Otherwise drop it, say so, and reload for the new person. Broadcast sign-out and sign-in to the other tabs (`BroadcastChannel`).
  - Read the CSRF cookie at each send. Use a JSON `CSRF_FAILURE_VIEW` (`csrf_failed`); on it, fetch a fresh token once and resend.
  - A 401 stops polling and isn't "connection lost".
  - `GET me` returns `session_expires_at`: warn 10 minutes before and allow signing in again in place. Better: end sessions at the clinic's day end (blocking 6), not 12 hours after sign-in.

**4. Redrawing Today moves a row under the finger, so the wrong patient is called in (7.3 Today, 7.4, 5.2).**
- **Breaks on** a busy clinic at the peak, on a touch screen or a slow tablet.
  - The receptionist's finger is on row 5's "Call in". A poll lands, a new booking or status change redraws the list, and the button under the finger now belongs to another waiting patient. The server can't catch it: that change is valid too.
  - A full redraw also loses focus, the scroll position and an open menu, and re-enables a button whose save is still running.
- **Fix:**
  - Order rows by turn number or slot time only, never by status. Key rows by id and patch them in place; never replace the row under the pointer or with focus.
  - Keep pending actions in a store keyed by appointment and action, so a redraw keeps their buttons disabled.
  - Name the patient in every confirmation ("Called in: Mohamed Ahmed · Undo"). Add `undo-call` (`in_room → waiting`) next to `undo_arrive`. Confirm Cancel with the patient's name.

**5. Polling as specified fails with several tabs, sleeping laptops and incidents (7.4, 5.11, 12.4).**
- **Several tabs:** every tab polls, and a second window or a waiting-room display counts as visible.
  - Fix: one poller per browser and membership. It holds a Web Lock named after the membership, and shares each answer and each of the person's own actions over `BroadcastChannel`. Where locks are missing (plain http, verified), each tab polls on its own.
  - Send `X-Membership` with every request (backend B3), and reload the tab on 409 `clinic_changed`.
- **Sleep and frozen tabs:** a closed laptop lid, or a tab frozen by the browser (Edge's sleeping tabs), has no failed polls, only old data. So "connection lost after two failed polls" never shows.
  - Fix: show "Updated 20:41" in server time, and dim the queue when the last good answer is older than two intervals.
  - Refresh at once on `visibilitychange`, `online`, `pageshow` and `focus`, and when a timer fires far later than set. Don't count the first failure after waking. Add "Try now": a backoff to 60 s can keep "connection lost" up for a minute after the network is back.
- **Incidents:** 5 s and 60 s are built into JS that stays open 12 hours, and every screen backs off in step.
  - Fix: every Today answer, 304 included, carries a server-set `Poll-After` header in seconds, so operations can halve 20,000 requests a second at once.
  - Add ±20% jitter to intervals and full jitter to backoff; honour `Retry-After` on 429 and 503. Without it, screens opened at 17:00, or reconnecting after a deploy, poll in step.
- **Caching:** the client keeps the last body and sends `If-None-Match` itself. The server sends `Cache-Control: no-store`, so patient data never reaches a shared PC's disk cache.
  - Verified: the 304 still reaches the page with `no-store`, while leaving it to the browser cache returned full 200s.
  - Put the person's permission set and language in the ETag (as judge item 7 also asks).

**6. Today's `day` is a calendar date picked by the client, but clinics run past midnight (5.11, 5.2, 13).** The server half is backend B9.
- **Breaks on** an evening session until 01:00 (common all year, normal in Ramadan), a laptop that slept overnight with Today open, or a PC with the wrong date. At midnight the screen either keeps polling yesterday, where `arrive` is now refused, or empties mid-session.
- **Fix:** with the backend's `day_ends_at`, `GET today` without `day` means the server's current business day. The answer carries `day`, `server_time` and the next rollover. The ETag includes the day, so a rollover arrives as a normal 200. The client never works out "today", including date-picker defaults and limits.

**7. Times, digits and typed numbers depend on the device (6.1, 7.2, 5.7, 7.3).**
- **Time zones:** 6.1 has the browser convert UTC with `Intl`.
  - Chrome and Edge 109 are the last versions on Windows 7 and 8.1, still common on clinic PCs. Their time-zone data predates Egypt's return to summer time in 2023, so they would show every time one hour early from late April to late October (inferred).
  - Fix: the server sends each displayed time with the clinic's offset (`2026-07-15T20:00:00+03:00`), using `zoneinfo` and the pinned `tzdata` (+03:00 in July, verified). The client shows the wall-clock part and converts nothing.
- **Digits:** on one engine, `Intl` gives Western digits for `ar` and Arabic-Indic for `ar-EG` (verified), and CLDR versions differ.
  - Fix: always pass the numbering system from the clinic setting (`ar-EG-u-nu-latn` or `-u-nu-arab`).
  - Format money with our own helper: `Intl`'s EGP string carries invisible U+200F marks (verified), which leak into copied text.
- **Typed numbers:** `Number("١٥٠")` and `parseFloat("١٥٠")` are NaN (verified), and Arabic keyboards on Android type those digits. Section 5.7 normalises search only.
  - Fix: one parser on client and server for amounts, quantities, phones, ages and days. It accepts Arabic-Indic (U+0660–0669) and extended Arabic-Indic (U+06F0–06F9) digits, and `٫`, `.` or `,` as the decimal point.
  - Money becomes piastres by string arithmetic. Number fields are `type="text"` with the right `inputmode`.
- **Browser floor:** state it (Chrome/Edge 109, Safari 15.4). Ban without a fallback: CSS nesting, `AbortSignal.timeout` (Safari 16), `AbortSignal.any`, `Object.groupBy`, import maps (Safari 16.4). This list is inferred from release notes.

**8. Phone numbers display reversed in Arabic (7.2, 5.7, 7.5).**
- **Breaks on** the default Arabic interface, wherever a phone with spaces is shown. A receptionist calling a patient reads out the wrong number. Measured in Edge:
  - `010 1234 5678` in an RTL table cell displays as `5678 1234 010`.
  - `+20 10 1234 5678` in an Arabic sentence displays as `5678 1234 10 +20`.
  - An RTL phone input shows its groups reversed.
- **Fix:** `h()` and the templates wrap every data value in `<bdi>`. Phones, file, invoice and prescription numbers, amounts and times get `dir="ltr"` with `unicode-bidi: isolate`; so do phone and number inputs. Names, notes and addresses get `dir="auto"`; medicine names on Arabic prescriptions `lang="en" dir="ltr"`. Add a page test that checks the groups' on-screen order (method under "Verified").

**9. Print pages print the URL and title, and the 80 mm receipt has no page size that works (7.5).**
- **Breaks on** Chrome and Edge with their default print settings, and roll-paper receipt printers. Measured by printing from Edge to PDF:
  - With the default margins, the A5 prescription had the date, the page title and the URL in its margins. `@page { margin: 0 }` removed them.
  - `size: 80mm auto` is invalid and printed on Letter. `size: 80mm` makes 80 × 80 mm pages, so a long receipt is cut into squares.
- Also: 7.5 prints "in the clinic's language", but the clinic has no language field (4.1), and R4.5 wants Arabic or English per prescription. Many doctors print on pre-printed letterhead. Safari blocks a tab opened after an `await` (inferred).
- **Fix:**
  - `@page { size: A5; margin: 0 }` with padding inside. Keep the patient's name out of `<title>`: it would be printed, and kept in a shared PC's history.
  - 80 mm: content 72 mm wide, black text only, at least 10 pt. Set the page height from the measured content with `insertRule("@page { size: 80mm <h>mm; margin: 0 }")`, which works under the strict CSP (verified; `style` attributes are blocked).
  - `?lang=ar|en` on print URLs, defaulting to a clinic setting. A letterhead setting: no header, with top and bottom offsets in mm.
  - Open the print tab in the tap handler, then point it at the URL when the save returns, with a link as fallback. Print after `document.fonts.ready`, in light colours only. Test two common thermal printers in M1.

**10. A walk-in takes 3 requests and 13–16 taps, and a partial failure leaves the wrong state (6.2, 7.3 Book and Pay).**
- The busiest front-desk job in a queue clinic: a patient walks in, pays and gets a turn. As designed: Book (1), 3–4 letters, pick (1), doctor (2), service (2), source (2), Save (1), Arrive (1), Pay (1), Save (1), Print (1), the browser's Print (1). That's 3 requests in a row, each followed by a Today refresh.
- **Breaks on** 3G with 300–800 ms round trips: 3–5 s with the patient at the desk. If the second or third request fails, the patient is booked but not waiting, or waiting but shown as owing.
- **Fix:**
  - `POST appointments` accepts `arrive: true` and an optional `payment {amount, method}`, in one transaction under one key. It answers with the turn, the invoice id and the version.
  - Defaults: the server's today; the open doctor tab or the only doctor; a consultation (follow-up price applied); "at the clinic"; "Collect now" on, with the fee in cash.
  - One button, "Book, collect and print": a returning walk-in becomes 3 taps, 3–4 letters and the print. For a patient who booked ahead, the row's main action is "Arrive and collect": one dialog, one request.

**11. The API is missing reads the screens need (6.2, 5.11).**
- **Assistants can't fill the Book dialog**: services, prices, doctors and schedules are readable only with `settings.manage`. Associate doctors likewise can't load the medicine list for prescriptions.
  - Fix: `GET clinic` (or part of `GET me`) for anyone signed in. It returns active services and prices, doctors with their mode and today's hours, the medicine list, payment methods, display settings (digits, print language, day end, follow-up days), the person's permission codes and the app version, cached with an ETag.
- **The fee preview in Book (7.3) has no endpoint.** Fix: `GET appointments/quote?patient=&doctor=&service=&day=` returns the fee and `is_follow_up`.
- **Today's rows lack what Pay and Print need.** Fix: add `due`, `invoice_id`, the phone, the file number, `is_new` and `is_follow_up`.
- **Status changes and payments don't return the new state,** so each action costs a second request. Fix: return the updated row and the new version. If that version is exactly one more, the screen patches the row and keeps polling with it; otherwise it fetches Today again.

**12. Unbundled ES modules break after a deploy and load slowly on 3G (7.1, 9 Deploy).**
- **Deploys:** `ManifestStaticFilesStorage` hashes `app.js` but leaves `import "./api.js"` unhashed unless `support_js_module_import_aggregation = True`. With the flag, static and dynamic imports become hashed names (both verified). Without it, after a deploy a cached page mixes old and new modules, or every module must be revalidated on every load.
- **Load time:** each import level costs a round trip before the next is found, about 1–2 s for 4 levels on 3G. Fix: `<link rel="modulepreload">` for the core modules in the shell.
- **Long-lived tabs:** Today stays open 12 hours on old code. Fix: an `X-App-Version` header on every answer; when it changes, reload at the next quiet moment (no open dialog, nothing pending). Keep the previous release's hashed files for a day, so an old tab's `import()` doesn't get a 404.

## Improvements (nice to have)

1. **Per-doctor versions** on `doctor_day`, so a large clinic's screens refetch only the queue that changed. At 30 doctors the clinic-day version changes every few seconds at the peak.
2. **Phone search by the last digits**, with a reversed-phone column and a prefix index: people quote the end of a number, and every Vodafone number starts with 010. Normalise `+20`, `0020` and `20` prefixes. Join "عبد" and "أبو" to the next word; map ؤ → و and ئ → ي.
3. **The Today search box:** debounce 250 ms, abort the previous request, support arrows and Enter. Results show age, sex, any booking today (so nobody is booked twice) and the last visit. Picking a result doesn't open the file, so it writes no access event.
4. **Book on phones:** a full-screen sheet with Save above the keyboard. Day chips (today, tomorrow, +2, a date), since `<input type="date">` shows the Windows date format, not the app's. Age in years or months, for paediatrics.
5. **The doctor's view:** "Finish and call next" as one action, and a warning when someone is already in the room. One `GET appointments/{id}/visit` for the visit screen (patient summary with allergies, note, last prescription with items, invoice), writing one access event.
6. **Server-clock times:** "waiting 25 min" and "updated" from the server's time (offset from the `Date` header). Warn when the PC's clock is more than 5 minutes off. A PC with a dead clock battery fails HTTPS completely, so "check the date and time" goes first in the support runbook.
7. **Feedback:** errors stay until dismissed (the prototype's toast vanishes after 3.2 s). Success names the patient and the amount. `role="status"` for success, `role="alert"` for errors.
8. **Accessibility:**
   - 44 px targets with 8 px gaps (the prototype's `.btn.small` is about 32 px).
   - Native `<dialog>` with `showModal()`, returning focus to the button that opened it. Focus the `h1` on navigation.
   - A polite live region for the person's own results and new arrivals only, never the whole queue.
   - Check `/` with `event.code === "Slash"` and ignore it in fields: on the Arabic 101 layout that key types ظ.
   - Each status as text plus an icon; contrast checked in both themes.
9. **Arabic type:** 16 px body text, at least 14 px secondary text, line height about 1.6. Arabic at the prototype's 12.5 px is hard to read (screenshot). No letter-spacing or uppercase styles on Arabic. Stack: `system-ui, "Segoe UI", Tahoma, "Noto Sans Arabic", "Geeza Pro", Arial, sans-serif`. For print only, consider one vendored OFL font (Noto Naskh Arabic), so a prescription breaks lines the same on every device.
10. **RTL details:** mirror directional icons (back, next, chevrons) and only those. `font-variant-numeric: tabular-nums` for amounts, aligned to the end of the column.
11. **Styles under the strict CSP:** `h()` sets styles through `el.style`, never a `style` attribute (verified: attributes are ignored). Templates never use `style=""`.
12. **Client tests in section 10:** Django test-client tests for pages and fragments. A few headless browser scripts, like this review's, for a double tap, a lost answer, a 401 mid-save, the RTL order of a phone, and the A5 and 80 mm page sizes.
13. **Sign-in throttling:** 30 per 5 minutes per IP can lock out a clinic behind one router at 17:00, or many people behind a mobile carrier's shared address. Throttle mainly per username, and show the wait time.
14. **Partial lists:** "load more" with the cursor, and say how many rows are shown, so a list never looks complete when it isn't (CLAUDE.md known gap 5).

## What is right and should stay

- Conditional polling, with a version bumped in the same transaction and a 304 when nothing changed, not WebSockets (12.4). Verified that the 304 reaches `fetch`.
- Request keys, and conditional status updates: a repeat is a harmless 200, a conflict is 409 `status_changed` with the current status.
- The server decides "today" (13). Times are stored in UTC; money is integer piastres.
- Print pages are rendered on the server and write an access event.
- No CDN, no web fonts, a strict CSP, every value escaped (`h()`, never data in `innerHTML`).
- `<html lang dir>` per user, Arabic first, logical CSS properties. Western digits by default, Arabic-Indic as a clinic setting.
- Hiding what a person can't do, from `GET me`, while the server enforces it.
- Keyset pagination capped at 100. Errors with a code, a translated message and fields. Another clinic's records answer 404.
- The search normalisation in 5.7 (alef forms, taa marbuta, alef maqsura, diacritics, digits).
- A disabled button while saving, the form kept after a failure, a refresh right after one's own action, and slower polling when hidden.

## What I verified by running, and what I inferred

**Verified** with Django 6.1.3 from `v2/.venv`, headless Edge 156 and Node 24 (ICU 78). Scratch files are in my scratchpad (`djtest/`, `edge/`).
1. **Translations without gettext:** `makemessages` stops with "Can't find msguniq", `compilemessages` with "Can't find msgfmt" (no GNU gettext here). A polib-compiled `.po` loads in Django: `{% translate %}` works, including with context. `JSONCatalog` serves those strings with context and the 6-form Arabic plural rule; with `domain="django"` it adds Django's own strings, 447 in all (41 KB). With `contrib.auth` installed, Django's Arabic password messages appear without gettext.
2. **Static files:** without the flag, collectstatic's `app.<hash>.js` still imports `./api.js`. With `support_js_module_import_aggregation = True`, it imports `./api.<hash>.js` and `import("./pages/today.<hash>.js")`.
3. **Django 6.1 source:** `login()` calls `rotate_token` and `cycle_key`. `SESSION_SAVE_EVERY_REQUEST` defaults to False. The default CSRF failure view returns HTML.
4. **ETags in Edge:** a page-sent `If-None-Match` gives `fetch` a 304, even with `Cache-Control: no-store`. Leaving it to the browser cache gave a 200 with the full body, and no conditional request reached the server.
5. **Not localhost:** served from `http://192.168.1.6`, `isSecureContext` is false and `crypto.randomUUID` and `navigator.locks` are undefined. `BroadcastChannel` exists.
6. **Bidi in Edge:** each token wrapped in a plain `<i>` (which doesn't change bidi), then sorted by on-screen x. `010 1234 5678` in an RTL cell → `5678 1234 010`. `+20 10 1234 5678` after Arabic → `5678 1234 10 +20`. Inside `<bdi dir="ltr">`: correct. A screenshot shows an RTL `<input>` with reversed groups, and correct with `dir="ltr"`.
7. **`Intl` and time zones:** Western digits for `ar`, Arabic-Indic for `ar-EG`, in numbers and dates. The EGP money string holds U+200F and a no-break space. `Intl.PluralRules('ar')` has 6 categories. `Number`/`parseFloat` of `١٥٠` is NaN. Python `zoneinfo` with `tzdata` 2026f: Cairo is +02:00 in January and +03:00 in July 2026.
8. **Print to PDF from Edge:**
   - Default margins put the date, the title and the URL on the A5 page; `@page { margin: 0 }` removed all three. A5 came out as 420 × 595 pt. `size: 80mm auto` gave Letter (612 × 792 pt); `size: 80mm` gave 80 × 80 mm.
   - Under `style-src 'self'`, `style` attributes and `setAttribute('style')` were ignored, while `el.style` and `insertRule('@page{size:80mm 143mm}')` worked: the PDF page was 80 × 143 mm. Arabic in Segoe UI rendered correctly at 72 mm.
9. **Size:** the warehouse v2's JS and CSS total 39,587 bytes (`wc -c`).

**Inferred (not run):**
- Chrome and Edge 109 as the last versions on Windows 7 and 8.1, with pre-2023 Egyptian time-zone rules.
- Edge's sleeping tabs; Safari's popup blocking after an `await`; the Arabic 101 key layout.
- Thermal printers: 72 mm printable width, and how grey text prints.
- Clinics running past midnight, and pre-printed letterhead.
- Throttling behind carrier-grade NAT.
- The tap counts, read from 7.3, not measured on a built screen.
- The 1–2 s module waterfall, from round-trip arithmetic.
