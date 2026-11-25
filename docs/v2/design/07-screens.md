## 7. Screens

### 7.1 Approach

- **Server-rendered Django pages** with a few small JavaScript modules. No framework, no HTMX, no build step, no CDN, no web fonts.
- **Modules** (in `web/static/js/`, plain ES modules):
  - `api.js`: sends actions, owns request keys, retries, the sign-in overlay and CSRF;
  - `today.js`: the poller and row patching;
  - `dialog.js`: Book, Pay, Prescription and confirmations as native `<dialog>`s;
  - `numbers.js`: the shared number parser.
- Client code never builds HTML from strings. It inserts only same-origin server fragments, sets text with `textContent`, and sets styles through `el.style`, which the strict CSP allows (style attributes don't work).
- **Static files:** `ManifestStaticFilesStorage` with `support_js_module_import_aggregation = True`, so imports get hashed names; `<link rel="modulepreload">` for the shared modules. `X-App-Version` on every answer: when it changes, Today reloads at a quiet moment (no open dialog, nothing pending). The previous release's files are kept for a day.
- **Browser floor:** Chrome and Edge 109, Safari 15.4. Not used without a fallback: CSS nesting, `AbortSignal.timeout`, `AbortSignal.any`, `Object.groupBy`, import maps.
- The page hides what the person can't do, and the server enforces it anyway.

### 7.2 Language, numbers and right to left

- `<html lang dir>` comes from the user's language. Arabic is the default and right to left. The CSS uses logical properties, so one stylesheet serves both directions.
- **One catalogue:** `locale/ar/LC_MESSAGES/django.po`, compiled with polib (`manage.py compile_translations`), because GNU gettext isn't available on Windows. Strings that are ambiguous in Arabic use `pgettext` (Book the verb and the noun differ). The few strings JavaScript needs come from the page (`data-*` attributes or `json_script`). A test extracts the translatable strings by pattern and fails when Arabic lacks one or keeps an unused one; it replaces `makemessages`.
- Prefer "label: number" (`في الانتظار: 3`) over plural sentences.
- **Formatting on the server:** times in the clinic's time zone; digits from the clinic's setting (Western by default, Arabic-Indic optional); money with our own formatter (Intl's EGP string carries invisible marks).
- **Bidi:** every data value is wrapped in `<bdi>`. Phones, file, invoice and prescription numbers, amounts and times get `dir="ltr"` with `unicode-bidi: isolate`, inputs included. Names, notes and addresses get `dir="auto"`. A page test checks the on-screen order of a phone's digit groups.
- **Number fields** are `type="text"` with the right `inputmode`, parsed by `numbers.js` and again on the server.
- **Type:** 16 px body text, at least 14 px secondary text, line height about 1.6, no letter-spacing on Arabic. Fonts: `system-ui, "Segoe UI", Tahoma, "Noto Sans Arabic", "Geeza Pro", Arial, sans-serif`.

### 7.3 Pages

| Page | What it does | M |
|---|---|---|
| Sign in | Username or phone and password; then the code or a recovery code; a forced password change; code set-up with recovery codes | M0 |
| Clinic picker | For people in several clinics | M0 |
| Today | Per-doctor queues (tabs on small screens): turn, patient, service, status, paid or owed, the next action. "Arrive and collect" for booked rows. Counts, the low-stock banner, recent changes, search, Book | M1 |
| Book | One dialog: find or add the patient (with the duplicate-phone step), doctor, service, a slot for slot doctors, source. The fee and follow-up from `quote`. One button: "Book, collect and print", defaulting to the business day, the open doctor tab, a consultation, "at the clinic" and collect now in cash | M1 |
| Pay | Amount (the balance by default), method, receipt | M1 |
| Patients | Search, then the patient file: details, visits and payments; notes and prescriptions with `clinical.view`; each list paged with "load more" and a count of what is shown | M1, M2 |
| Visit | For the doctor: the note (with version checks), a prescription, materials used, finish | M2 |
| Prescription | Pick medicines, set dose, amount and days with steppers, print in Arabic or English | M2 |
| Inventory, Finances, Consultations, Team, Settings, Audit | As in R4.6–R4.9 | M3 |
| Account | Password, language, code set-up, recovery codes | M0 |
| Platform | Clinics: create, suspend, reactivate, usage counts | M1 |

### 7.4 Today and resilience

**Polling:**
- One poller per browser and membership: it holds a Web Lock named after the membership and shares each answer, and each of the person's own actions, over `BroadcastChannel`. Where locks don't exist (plain http on a LAN), each tab polls on its own.
- The interval comes from `Poll-After` (5 s while visible, 60 s while hidden, by default), with ±20% jitter. Backoff after errors uses full jitter, and `Retry-After` is honoured.
- "Updated HH:MM" in server time, and the queue dims when the last good answer is older than two intervals. It refreshes at once on `visibilitychange`, `online`, `pageshow` and `focus`, and when a timer fires much later than set (a laptop that slept). The first failure after waking doesn't count. A "try now" button.
- A 401 stops polling and shows the sign-in overlay, not "connection lost". A 409 `clinic_changed` reloads the tab.

**Rows:**
- Rows are ordered by turn or slot time only, never by status. They are keyed by id and patched in place, and the row under the pointer or with focus is never replaced.
- Pending actions are kept in a store keyed by appointment and action, so a redraw keeps their buttons disabled.
- Confirmations name the patient ("Called in: Mohamed Ahmed"). Cancel asks for confirmation with the patient's name.

**Saving (the browser half of safe repeats):**
- A form's request key is made when the form opens, and reused for every send until a definite answer: a 2xx, or a 4xx that wrote nothing. **409 `busy` and 503 are never definite**: the same key is resent after `Retry-After`.
- After a timeout or a network error the outcome is unknown. The form locks its fields, says "not confirmed yet, checking", and resends the same key and body with backoff. It offers only "try again" and "discard".
- `{url, key, body, user, membership, created_at}` is kept in `sessionStorage`. After a reload or a new sign-in, it is settled first, only for the same user and membership. A pending request older than 24 hours is never resent: the screen says "outcome unknown, check the record", because its key row may already be gone (4.11). It is cleared at sign-out and when another person signs in. No patient data ever goes into `localStorage`.
- "Saved" shows only for an answer that echoes the key. "Still saving" shows after 3 s; retries stop after about 20 s.
- Retries happen only after network errors, timeouts, 502, 503, 504, 409 `busy`, and 429 after `Retry-After`. Never after a 500.
- Where `crypto.randomUUID` is missing (plain http), keys are built from `crypto.getRandomValues`.

**The sign-in overlay:**
- It sits over the intact page and handles the password, the code and a forced password change. It returns to the same clinic without the picker.
- After sign-in, a stored request is replayed only for the same user and membership. Otherwise it is dropped with a message, and the page reloads for the new person.
- Sign-in and sign-out are broadcast to the other tabs.
- A warning appears 10 minutes before `session_expires_at`, with sign-in in place.

**Drafts:** visit notes and prescription drafts are kept in `sessionStorage` per appointment until saved, with one save in flight per note (M2).

### 7.5 Printing

- Receipts and prescriptions are Django templates, rendered in the language from `?lang=ar|en` (default: the clinic's print language).
- **A5:** `@page { size: A5; margin: 0 }`, with padding inside, so the browser prints no URL or title.
- **80 mm receipts:** content 72 mm wide, black text only, at least 10 pt. The page height is set from the measured content with `insertRule("@page { size: 80mm <h>mm; margin: 0 }")`, which works under the CSP.
- A letterhead setting: no header, with top and bottom offsets in mm, for pre-printed paper.
- The print tab is opened in the tap handler (Safari blocks one opened later), then pointed at the URL when the save returns, with a link as a fallback. It prints after `document.fonts.ready`.
- No patient name in `<title>`: it would be printed and kept in a shared PC's history.
- M1 tests two common thermal printers.

### 7.6 Accessibility

- Every field has a label. Focus is visible and the keyboard works. `/` focuses search, checked with `event.code === "Slash"` and ignored inside fields (on the Arabic layout that key types ظ).
- Tap targets of at least 44 px with 8 px gaps.
- Native `<dialog>` with `showModal()`, returning focus to the button that opened it. Navigation focuses the `h1`.
- Results go to a polite live region (`role="status"`); errors use `role="alert"` and stay until dismissed.
- Every status is shown as text plus an icon, never by colour alone. Contrast meets WCAG AA.
