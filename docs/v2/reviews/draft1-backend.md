# Hamada v2 design, draft 1: backend review

Reviewer: senior-backend, 2026-11-07. Reviewed `docs/v2/design.md` (draft 1) against `docs/v2/requirements.md`, the prototype (`clinic/http.py`, `clinic/services.py`, `clinic/tests.py`) and the reviewed warehouse patterns (`smart-warehouse/v2/core/idempotency.py`, `identity/services.py`, `identity/totp.py`). I did not edit the design, the requirements or any code. The new code that appeared in `v2/` today is outside this review.

**Verdict: revise.** The architecture is right: one database, rows scoped by clinic, services with conditional updates, polling with a version. The problems are at the edges: who can grant what, safe repeats, money states, the business day, two-factor sign-in, and running with or without Redis. There are 13 blocking problems. Six of the database behaviours behind them were run on PostgreSQL 18.6 (section E).

Where the questions are answered: Q1 → B1, B2, I1 · Q2 → B4, B5 · Q3 → B5–B9 · Q4 → B3, B10, B11 · Q5 → B9, B12, I12 · Q6 → B1–B3, B13, I6 · Q7 → B2, B10, B13 · Q8 → section T.

## Blocking problems

**B1. A non-owner with `team.manage` can give out permissions they don't hold (4.2, 5.8).**
- Breaks:
  - An assistant who was granted `team.manage` adds a new member (a new account with a temporary password they choose). They grant it `billing.refund`, `audit.view`, `finances.view` and `settings.manage`, because 5.8 reserves only `team.manage` for the owner. Then they sign in as that account.
  - Changing a member's role from assistant to doctor silently adds `clinical.write`.
  - Two `team.manage` holders can edit each other. "Nobody can change their own permissions" doesn't cover `PATCH team/members/{own id}` changing one's own role or `is_active`.
- Bypasses through request bodies, which the decorator's single `perm` can't see: `POST appointments` with a new patient creates a file without `patients.edit`; finish with a payment collects without `billing.collect`; `POST invoices/{id}/lines` with a typed price changes the bill without `billing.discount`.
- Fix:
  - A non-owner may grant, remove or reach through a role change only codes they hold (`new − current ⊆ actor's codes`, as the warehouse's `_may_give`, `identity/services.py:97`).
  - `team.manage`, `billing.refund`, `audit.view` and `finances.view` are owner-only to grant or remove. Nobody edits themselves. Only the owner edits a member who holds `team.manage`. Stored `granted`/`revoked` are intersected with the known codes.
  - Services check the body-dependent codes themselves: `patients.edit`, `billing.collect`, and `billing.discount` for any price not taken from the list.

**B2. Accounts are global, so clinic and platform powers over them reach other clinics (4.1, 5.8, 6.2, 8).**
- Breaks:
  - (a) A doctor works in clinics X and Y. If X's `team.manage` can set a temporary password, reset the code, or change that account's username or phone (6.2 doesn't say what `PATCH team/members/{id}` may change), X's manager signs in as the doctor and opens Y's patients.
  - (b) "Add an existing account by username" attaches any platform user without consent and confirms that the username exists. It can also attach an `is_platform_admin` account, which then sees patient data.
  - (c) Ownership transfer, and before M4 every password or code reset, are platform-admin actions with no rules. A platform admin can make an account they control the owner of any clinic.
- Fix:
  - Clinic endpoints change memberships only, never `accounts_user`.
  - An existing account joins by an invitation its holder accepts, and the add answers the same for known and unknown usernames.
  - Platform-admin accounts can't hold memberships; check this when adding a member and when setting the flag.
  - Ownership moves only to an existing active doctor of that clinic, with `CHECK (role <> 'owner' OR is_active)`.
  - Support resets follow a written procedure: a reason, a second admin, an audit event in the clinic's own log, and a notice to the account at its next sign-in. Django's admin site is off in production.

**B3. The active clinic belongs to the session, not to the browser tab (5.9, 5.11, 6.1).**
- Breaks: a doctor in two clinics has clinic A's Today in one tab and picks clinic B in another. Both tabs share one session. Tab A's next "Book" with a new patient name creates the file and the booking in clinic B. Its next poll gets B's queue (a new ETag, so a full answer) and draws it under A's header.
- Fix: every API call sends the membership id its page was loaded for (`X-Membership`). The decorator compares it with the session's and answers 409 `clinic_changed` when they differ. The clinic still comes from the session; the header is only a check, so the CLAUDE.md rule holds. Compare the whole ETag string, including the clinic id.

**B4. Safe repeats: some creates have nowhere to keep the key, a reused key with a different body is accepted, and concurrent copies get errors (4.0, 5.0, 5.3, 6.1, 12.5).**
- Creates in 6.2 whose rows have no `request_key`:
  - `POST invoices/{id}/lines`: `api.js` retries a create that timed out, so the patient is charged twice.
  - `POST staff-pay`: the pay is counted twice in the month.
  - Schedules: with no unique key, a duplicate session row doubles the slot grid.
  - `POST team/members` with a new account: the retry gets 409 "username taken", and the temporary password from the lost answer is gone.
  - Items, services, medicines, categories, `platform/clinics`: the retry gets 409 for a create that worked.
  - `request_key` is nullable, so a create without one has no protection. Finish-with-payment and discount have no key at all.
- Reused key, different body: 5.0 returns the first row. After "not saved" (7.4) the user changes the amount from 200 to 300 EGP and saves. If the key is reused, the answer is the 200 EGP payment, with 200 OK.
- Concurrent copies (verified, E2 and E3): the second copy looks up the key before the first commits, so it doesn't find it.
  - A full-balance payment then waits on the invoice row, re-checks `paid + a <= total` and answers 409 `over_balance`.
  - A slot booking fails on whichever unique index PostgreSQL checks first. With the slot index created first, the answer is 409 `slot_taken`.
  - The desk sees an error for work that was saved, and does it again with a new key.
- Fix (recommended): the warehouse's reviewed `run_once` (`core/idempotency.py`) for every POST.
  - Claim the key first with `INSERT … ON CONFLICT DO NOTHING` into `core_request_key` (clinic_id, key, endpoint, request hash, target id, created_at). A concurrent copy waits on the claim and gets the first result.
  - Another endpoint or a different hash answers 422 `request_key_reused`. Keys are required.
  - Partition the table by day and drop partitions after 3 days. Store the target id, not the response body, so no patient data is copied.
  - This reverses 12.5, whose saving isn't real. Keys stored on rows keep a unique index entry for every row, forever: about 360M keyed rows a month, so 8–9B entries and roughly 0.3–0.4 TB of index after 2 years (estimate), each useful for about a minute.
- If 12.5 stays: add `request_key` and `request_hash` to every created table and make them required. After any IntegrityError or 409 in a create, look the key up again in a new transaction before answering.

**B5. Reschedule isn't safe to repeat and can deadlock (5.2, 5.0).**
- Breaks:
  - A repeat of a successful reschedule (a double click, or a resubmit after a timeout) is a legal booked → booked move. It takes another turn on the same doctor-day, so turn 3 becomes turn 15.
  - Verified (E4): reschedule A from day X to Y and B from Y to X at once. Each updates its new doctor-day row first, and PostgreSQL aborts one as a deadlock, which 5.0 turns into a 500. The two `clinic_day` rows have the same problem.
- Fix:
  - A reschedule to the current doctor, day and slot is a 200 that changes nothing, and it carries a request key (B4).
  - Lock both doctor-day rows, then both clinic-day rows, in ascending (doctor_id, day) order before changing them.
  - Extend 5.0's lock order: the appointment, then the invoice, then payment rows; two rows of one table in key order.
  - Map deadlocks (40P01) and lock timeouts (55P03) to 409 `busy`, so `api.js` retries with the same key.

**B6. Money states that go wrong (5.2, 5.3, 4.5).**
- (a) Cancel after payment. Payment is allowed in `booked` (a phone booking prepaid by InstaPay, or arrive → pay → undo arrive), and cancel checks only the status. The visit ends cancelled with money on it, still counted as collected, and nothing asks for a refund.
  - Fix: cancel answers 409 `refund_first` while `paid > 0`, or a cancel-with-refund service needs `billing.refund`.
- (b) A void blocks the visit forever (verified, E5). `UNIQUE (clinic_id, appointment_id)` counts void invoices, so a voided invoice can never be replaced. The next payment finds it and gets a misleading 409 `over_balance`.
  - Fix: make the index `WHERE status = 'open'`.
- (c) Invoice numbers get gaps (verified, E1). Two first payments for one visit race. The second takes counter value 2, its `ON CONFLICT DO NOTHING` inserts nothing, and it commits: counter 2, invoices [1], against "numbers have no gaps" (4.2).
  - Fix: lock the appointment row, then check for the invoice and create it.
- (d) No invoice exists before the first payment. The usual desk order (a discount or an extra procedure, then payment) is impossible, and paying first then discounting needs a refund.
  - Fix: `POST appointments/{id}/invoice` (get or create), or create the invoice at arrival; add an endpoint to remove a line.

**B7. Refunds and revenue per doctor are undefined (4.4, 5.3, 5.10).**
- Breaks:
  - `doctor_day` has no `refunded` column, so "update the summaries" on a refund can mean two things. Subtracting from the refund day's `doctor_day.collected` fails the `>= 0` check (a 500) when that doctor collected less that day or has no row. Rewriting the payment's day changes a day already closed.
  - Revenue per doctor and percentage pay (R4.7) can't be shown net of refunds.
  - Payments don't store the doctor. After a prepaid visit moves to another doctor, the nightly recompute joins to the current doctor and "fixes" correct numbers into wrong ones.
- Fix:
  - Payment and refund rows carry `doctor_id` (copied at insert) and `day`. `doctor_day` gets `refunded`. A refund counts on its own day; net = collected − refunded.
  - Add a table to 5.2/5.3 listing which counter each action changes (book, arrive, undo, finish, cancel, no-show, reschedule, pay, refund, void, expense and its void). The recompute and the invariant tests both read it.

**B8. The follow-up price rule differs from the requirements and misfires (5.1 vs R4.1).**
- R4.1 says within N days of a *paid* consultation. 5.1 says the last *finished* consultation *with the same doctor*, and doesn't say from which date.
- Breaks:
  - The common case is booking the follow-up at the desk before the doctor presses finish. That booking is charged the full price.
  - A doctor who never presses finish blocks every follow-up.
  - Measured from today, a booking made on day 10 for day 20, after a consultation on day 1, gets the follow-up price by mistake.
  - "The follow-up price" is undefined when a clinic has several follow-up services.
- Fix: settle the rule with Saged. Measure from the appointment's day and count arrived or paid consultations. Link each consultation service to its follow-up (`follow_up_service_id`), and check the rule again at arrival.

**B9. The business day ends at midnight, and the sweep ignores leftovers (4.0, 5.2, 5.10).**
- Egyptian evening sessions often run past midnight, and in Ramadan many clinics open after iftar until 1–2 AM. Breaks:
  - A patient booked for Thursday who arrives at 00:20 can't be marked arrived ("only on the appointment's day").
  - A sweep that runs before the session ends marks waiting patients `no_show`.
  - Payments after midnight land in the next day's totals and day closing.
- The sweep handles only `booked`. A patient who paid and left stays `waiting` forever. A doctor who forgets finish leaves the visit `in_room` forever with no invoice, so the fee owed is never recorded.
- Fix:
  - A clinic setting `day_ends_at` (default 04:00), used everywhere the server computes `day`. The sweep runs per clinic after that time.
  - A written rule for leftover `waiting` visits (no-show keeping arrived_at, or a `left` status) and `in_room` visits (finish, create the invoice, flag it).
  - Allow finish from `waiting`, because assistants often skip "call".

**B10. Two-factor sign-in has gaps (4.1, 5.9, 8).**
- (a) "Each code used once" has no column: 4.1 has only `totp_secret` and `totp_confirmed_at`. Add `totp_last_step`, set with `UPDATE … WHERE totp_last_step < :step`, so two containers can't both accept one code (the warehouse keeps it).
- (b) `auth/totp` isn't throttled. The ±1 window accepts 3 codes in a million, so about 230,000 guesses give an even chance: about an hour at 60 requests a second. Five wrong codes should end the pending sign-in and count toward the username limit.
- (c) The pending state needs a 5-minute expiry, must not count as signed in for `@api`, and needs a new session key after the code.
- (d) `must_change_password` and "set up the code before anything else" are enforced only by the UI, so the API works with a temporary password. `@api` must enforce both.
- (e) Whoever holds the temporary password sets up the authenticator: the admin who created the owner, or anyone who read it in WhatsApp. Temporary passwords should expire in 72 h, and the password must be changed before setting up the code.
- (f) There is no recovery: an owner who loses the phone is locked out at 7 PM. Add 10 single-use recovery codes, stored hashed, plus the B2 support procedure.
- (g) The code is required per role and per clinic but checked only at sign-in, per account. Picking a clinic whose setting requires it, or an ownership change mid-session, skips it. Keep `mfa_verified` in the session and check it in `@api` against the active membership.
- (h) TOTP secrets stay unencrypted until M5 (section 13), so a leaked backup gives both factors. Encrypt them before go-live.

**B11. Redis, and running without it (3.1, 3.3, 5.9, 5.11).**
- (a) Throttles break without Redis, which is how stage 1 launches.
  - Breaks: Django's cache is then per process. The prototype falls back to `LocMemCache` (`config/settings.py:76`), so "8 per username" becomes 8 per process and resets on every deploy. "Fall back to PostgreSQL" has no table to fall back to.
  - Fix: keep counters in PostgreSQL (the warehouse's `LoginThrottle`), or count failures in `accounts_login_event` by (user_id, at) and (ip, at). Redis only speeds this up, and the throttle fails closed. Count failures, not attempts. Limit per (username, IP) with a looser per-username limit, so nobody can lock the owner out during clinic hours.
- (b) Sessions in Redis can't be rebuilt, which contradicts 3.1.
  - Breaks: a flush at 19:00 signs out about 100,000 screens. A password check costs 0.21 s of CPU (verified: Django 6.1.3 uses 1.5M PBKDF2 iterations), so 100,000 sign-ins cost about 21,000 CPU-seconds, while the throttles fire.
  - Fix: use only the `cached_db` backend, with a fixed 12-hour expiry, and never save the session on a poll.
- (c) The queue version in Redis is "set after each commit".
  - Breaks: a crash between commit and SET, a Redis blip, or two SETs out of order (6, then 5) leave Redis behind PostgreSQL. Screens get 304 and miss a waiting patient until the next change.
  - Fix: set only if greater (a small Lua script), with a 30–60 s TTL so a miss reads PostgreSQL.
- (d) The "membership cache" (3.1) contradicts "read from the row on every request" (5.8): a dismissed receptionist keeps access until the entry expires. Drop it, or delete on commit and keep a short TTL.

**B12. Jobs (5.10, 9).**
- (a) Verified (E6): a recompute that counts first and writes later, with a finish committing in between, leaves the counter at 1 when the truth is 2. The safety net creates the drift it reports.
  - Fix: lock the summary rows `FOR UPDATE` first, then count, then write (E6b stays correct), one clinic-day per transaction.
- (b) Procrastinate's worker builds its own psycopg connection from the Django database settings, with `listen_notify=True` (read in the installed 3.10.0). LISTEN doesn't work through PgBouncer in transaction mode.
  - Fix: connect the worker straight to PostgreSQL, or turn LISTEN off and accept 5-second polling.
- (c) "Jobs and migrations raise them per session" and the web's `lock_timeout` are session settings. Through transaction pooling they are lost. Worse, a job's `SET statement_timeout = 0` stays on a server connection that a web request gets next.
  - Fix: per-role settings (`ALTER ROLE hamada_web SET lock_timeout = '2s'`, with separate job and migration roles), or `SET LOCAL` in each transaction.
- (d) Jobs have no membership. Define a system `Ctx`, make `created_by_id`/`updated_by_id` nullable, and record the actor "system" in the audit log. Services that read patient data refuse system and platform contexts.

**B13. Health data: what's missing before real patients (R4.5, R5, 5.6, 6.2, 8).**
- (a) The access log can be bypassed. Only `GET patients/{id}` writes an event. `GET appointments/{id}/note`, `patients/{id}/visits|prescriptions|payments`, `GET prescriptions/{id}` and `GET invoices/{id}` return clinical content without one, so stepping through appointment ids reads diagnoses unlogged.
  - Fix: declare it in the decorator (`@api(…, access="patient")`) and have the selector write the event. A test over the URL configuration fails for any undeclared view that returns patient data.
- (b) Assistants hold `patients.view`, which reaches notes and prescriptions, while R3 lists booking, the queue, payments and stock for them. Add `clinical.view` (doctor template only), so the desk sees demographics, not diagnoses.
- (c) `GET patients?q=` puts patient names and phones into the Caddy and Gunicorn access logs and Sentry. Log no query strings or bodies; set Sentry to `send_default_pii=False` with scrubbing.
- (d) Desks share PCs, so send `Cache-Control: no-store` on every API and print answer.
- (e) Visit notes are overwritten in place ("last writer wins", 5.6). With two devices a note is lost silently, and an edit leaves no trace, so a medical record changes with no history.
  - Fix: `If-Match` on `updated_at`, answering 409 on a conflict, and append-only revisions after the visit day.
- (f) Nothing limits bulk reads: one account can open thousands of files an hour.
  - Fix: a per-membership rate limit on file opens, with an alert. Grant the app role only INSERT and SELECT on `audit_event`; partition upkeep uses a separate role.

## Improvements (nice to have)

1. **One permission declaration.** Each service lists its codes, and a test over the URL configuration checks that every view's `perm` matches. `Ctx` carries the effective codes, `mfa_verified` and the actor kind (member, system or platform). Only the decorator and the job runner build it.
2. **Status actions.** The UI sends the status it saw, so a delayed "arrive" after "undo arrive" is refused. Add a walk-in action (book and arrive, optionally pay, in one transaction). Consultations have a `cancelled` status (4.9) but no action that sets it.
3. **Booking limits.** Enforce `max_bookings` in the turn upsert (`DO UPDATE … WHERE booked < :max` → 409 `day_full`). Add an exclusion constraint against overlapping schedule rows; `btree_gist` is already installed by `v2/scripts/pg.ps1`.
4. **Purchases and materials.** A purchase's expense can be voided through `expenses/{id}/void`, which leaves the stock; reverse the purchase instead. Decide whether materials used in a visit add a `material` line.
5. **Discounts.** A discount is an absolute value sent with the value it replaces, never an increment.
6. **Missing endpoints:**
   - `GET appointments/{id}` (the Visit page) and `GET invoices?unpaid` (the partial index is there for it);
   - read-only lists of doctors, services and medicines for people without `settings.manage`;
   - `PATCH inventory/items/{id}` and expense categories;
   - `POST platform/clinics/{id}/reactivate` (R4.11) and ownership transfer;
   - signing out other sessions, and a write permission for staff pay (`finances.view` must not write).
7. **Money on Today.** Hide money totals on Today from anyone without `finances.view` (R4.7).
8. **Query counts in section 10.** A 304 is 4 queries in stage 1 (session, the user from Django's auth middleware, membership, version), and in stage 2 the user row needs a cache too. A payment with the appointment lock and a new invoice is about 10 statements, not 8.
9. **Version bumps.** Bump the queue version on every change Today shows: patient name edits, stock use (the low-stock banner; 5.4 doesn't bump it) and schedule changes.
10. **Timing-safe sign-in.** Check a dummy hash for unknown usernames (`identity/services.py:33`).
11. **Time zone changes.** Lock a clinic's time zone and `day_ends_at` once it has bookings, or change them only through a job that recomputes `day`.
12. **Nightly jobs.** Run one job per batch of about 500 clinics, with one transaction per clinic, rather than 150,000 jobs. Recompute every past day whose summary changed since the last run, not only yesterday.
13. **Idle lock.** Lock patient-file screens on shared PCs after about 30 minutes without input, separately from the 12-hour session.
14. **Per-IP sign-in limit.** 30 tries in 5 minutes per IP will block clinics behind one router and phones behind a carrier's shared NAT. Count failures only.

## T. Tests to add to section 10

1. **Isolation through request bodies, not only paths.** Another clinic's patient, doctor, service, medicine or item in a POST body gets 404, and nothing is written.
2. **Permissions in both directions.** A single grant gives 200, a removal from the template gives 403, and the owner always gets 200. The B1 rules hold. A platform admin gets 403 or 404 on every tenant endpoint, listed from the URL configuration.
3. **Repeats for every POST, from the URL configuration.** The same key twice gives one row and the same answer. The same key with another body gives 422. Two threads with one key give one row and two 200s (the E2 and E3 cases). A retry after a 409 runs again.
4. **The full transition matrix.** Every action against every status, with repeats leaving the counters unchanged.
5. **Concurrency on PostgreSQL**, beyond the five planned tests: crossing reschedules (E4); concurrent first payments with no invoice-number gap (E1); a payment, a refund and a discount on one invoice; a double click on finish-with-payment; two sweeps at once, and a sweep against a late arrival; the recompute against live writes (E6); one TOTP code on two connections.
6. **Throttles across two processes, not threads**, because a per-process cache passes a thread test. Also run them with Redis stopped.
7. **Money invariants after random sequences**, including cancel after payment, reschedule after prepayment, refunds on another day, and void then re-invoice. Check that `invoice.paid = Σ payments − Σ refunds`, that the doctor-day sums equal the clinic-day sums, and that the summaries equal the recompute.
8. **Time.** `day_ends_at`, a session that crosses midnight, Egypt's clock changes (at midnight, in April and October), and a clinic in another time zone.
9. **Access log.** Every view declared `access="patient"` writes exactly one event, and no other view returns patient fields.
10. **Security.** CSP and `no-store` headers; CSRF on PATCH and PUT; a new session key at sign-in and after the code; a pending or must-change-password session gets 403 everywhere else; the TOTP guess limit.
11. **Load test pass criteria at the stage-1 size.** State the p95 and error rate for 304 polls and for the mix of about 1,000 writes a second, not only "simulated front desks".

## What is right and should stay

- **Tenant isolation.** The clinic comes from the session's membership. Every query and index starts with `clinic_id`, composite foreign keys stop cross-clinic references, another clinic's record answers 404, and the isolation tests are generated from the URL configuration.
- **Services.** Only services write, each in one short transaction, with a `Ctx` and a second permission check. Keep both checks, and put the B1 body-dependent checks in the service.
- **Concurrency.** Conditional `UPDATE`s guard statuses, stock and balances, with `CHECK` constraints behind them. "Already in the target status → 200" is right for the forward moves.
- **Turn numbers** come from the doctor-day upsert, and a rollback gives the number back.
- **Summaries** change in the same transaction as the work, last in the lock order.
- **Invoicing.** Creating the invoice at the first payment fits paying at arrival; keep it, with the B6 fixes. Payments are guarded by `paid + a <= total`, voids are allowed only at `paid = 0`, and refunds use a conditional `refunded`.
- **Prescriptions.** Items are copies that are never updated, and print pages are rendered on the server.
- **Jobs.** Procrastinate jobs are queued in the same transaction as the work (`defer()` runs on Django's connection), and periodic jobs aren't run twice when several workers run.
- **What runs where.** Summaries, access events and the version bump stay in the request. The sweep, the recompute, partitions, session clean-up, exports and reminders run as jobs.
- **Live queue.** Polling with a per-clinic-day version and 304 answers; the server decides what "today" is.
- **Storage.** Only the logs are partitioned; the business tables are sharded by clinic later.
- **Front end and sign-in.** `api.js` retries only creates and never after a 4xx. The `h()` escaping and a strict CSP stay. Two-factor codes come from an authenticator app, not SMS.

## E. What I verified by running, and what I inferred

Verified by running, on PostgreSQL 18.6 (port 5433), in a scratch database `hamada_review_scratch` that I created and then dropped. The script is in my scratchpad, not in the project.

- **E1, invoice numbers.** Two first payments for one visit: the second took counter value 2, its `ON CONFLICT DO NOTHING` inserted nothing, and it committed. The counter is 2 and the invoice numbers are [1].
- **E2, duplicate payment.** A full-balance payment sent twice with one key. The second copy's key lookup found nothing; its conditional `UPDATE` waited, then matched 0 rows (409 `over_balance` in the design). After its rollback, the key was there.
- **E3, duplicate booking.** A slot booking sent twice with one key failed on the slot index when that index was created first, and on the key index when the key index was created first.
- **E4, crossing reschedules.** One transaction committed and the other got `DeadlockDetected`.
- **E5, void invoices.** A second invoice for a visit whose invoice is void was refused by the unique index on (clinic_id, appointment_id).
- **E6, recompute.** Counting first and writing later gave a counter of 1 against a truth of 2. Locking the summary row, then counting, then writing gave 3 against 3.
- **Password cost.** Django 6.1.3's PBKDF2 hasher uses 1,500,000 iterations, which took 0.21 s per check on this machine.

Verified by reading the installed code, without running it:
- Procrastinate 3.10.0: `DjangoConnector.listen_notify` raises `NotImplementedError`; the worker uses a `PsycopgConnector` built from the Django database parameters; `listen_notify` defaults to True, and the worker polls every 5 s regardless; `procrastinate_periodic_defers` is unique on (task_name, periodic_id, defer_timestamp).
- Without `REDIS_URL`, the prototype uses a per-process `LocMemCache`, and its sessions stay in the database. With `REDIS_URL` it uses `cached_db` sessions.

Inferred, not run: the permission and account paths (B1, B2); the multi-tab behaviour (B3); the TOTP guessing arithmetic; Egyptian clinic hours, including Ramadan, and the timing of the clock changes; the storage estimate for request keys; the effect of carrier NAT on per-IP limits; and everything about Redis behaviour under failure.
