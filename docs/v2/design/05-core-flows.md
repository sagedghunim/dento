## 5. Core flows

### 5.0 General rules

**One transaction per request, on the clinic's shard.** `@api` (and the page decorator) does, in order:
1. checks the method, the session, `must_change_password`, the two-factor state (5.9) and `X-Membership` against the session's active membership (409 `clinic_changed`);
2. opens a transaction on the shard and runs `SELECT set_config('app.clinic_id', <id>, true)`. Never a session `SET`;
3. loads the membership and the clinic (`owned`, status) in one query. A missing clinic row, or one not `owned`, is 503 with `Retry-After` (3.4). It computes the effective codes;
4. for a mutating request, claims the request key (5.0 "Repeats");
5. checks the view's declared permission, builds the `Ctx` and calls the view. Any answer other than 2xx rolls the transaction back, the key claim included.

**`Ctx`** carries the clinic, the membership, the user, the actor kind (`member`, `system` or `platform`), the effective codes, `mfa_verified`, the request id and the IP. Only the decorators and the job runner build it. Services that read patient data refuse `system` and `platform` contexts.

**Repeats.** Every mutating request carries an `Idempotency-Key` header (a UUID), required by `@api`. The exceptions are sign-in, the code and sign-out, which are naturally single-use: a replayed code request never signs anyone in again, because the code's step is already used.
- `INSERT INTO core_request_key … ON CONFLICT DO NOTHING RETURNING 1`, right after `set_config`.
- Claimed: do the work and record the target in the same transaction.
- Conflict, with the same endpoint, hash and membership: replay the first result, rebuilt from the target. Anything else is 422 `request_key_reused`.
- A concurrent copy waits on the claim, then replays. Past `lock_timeout` it gets 409 `busy`, and the client resends the same key.
- A refusal (any 4xx) rolls back the claim with the work. Nothing was written, so the same key may run again.
- `PUT` and `PATCH` send absolute values with the expected old value or version.

**Lock order.** Every transaction takes locks in this global order:
1. the request key;
2. the appointment (`SELECT … FOR NO KEY UPDATE`, or a conditional `UPDATE`);
3. doctor membership rows (`clinics_membership`):
   - booking and reschedule take `FOR SHARE` on the target doctor, in a statement of its own before they read the doctor's sessions (a lock taken inside the session query would still read the sessions from before the wait);
   - schedule changes take `FOR NO KEY UPDATE`, several rows in one statement in id order;
   - the two conflict, so a booking and a schedule change for one doctor go one at a time; two bookings don't wait for each other;
4. session rows (`clinics_schedule`), only in schedule changes;
5. the patient (`patients_patient`), only in a patient edit (a conditional `UPDATE` on the expected values);
6. the clinic row (`clinics_clinic`, `FOR NO KEY UPDATE`), only in a settings change;
7. the invoice;
   - 7a. the patient package (`billing_patient_package`, `FOR NO KEY UPDATE`, M3). Recording a procedure takes it **before** the invoice (plan item → package → invoice); void and remove-line take it **after** (invoice → package). No cycle: a recording waits only for its own visit's invoice, which every other flow reaches through that visit's appointment (held by the recording), except E3's finish, which locks other invoices but never a package;
8. payment rows;
   - 8a. the open cash shift (`finance_shift`, M3). Payments, refunds, cash movements, expenses and payouts take it `FOR SHARE`. Cash never fails for want of a shift: when none is open, the cash entry opens one in its own transaction (`INSERT … ON CONFLICT (clinic_id) WHERE status = 'open' DO NOTHING` on the one-open-shift index, float 0, `auto_opened`, the actor as opener), then selects it again if another request won. Two first entries therefore wait on each other through that index, so every cash flow takes the shift **after** the invoice counter (11) and **before** any summary row, item or expense: booking with cash takes it right after its counters, before the doctor-day. Only close takes it `FOR NO KEY UPDATE`, then sums and updates it, and takes nothing after it. A cash entry that waited behind a close re-checks `status = 'open'`, finds none and opens a fresh shift, so a closed shift never gains an entry after its count;
9. inventory items;
10. expenses;
11. counter rows, in the order `patient_file`, `invoice`, `prescription`;
   - 11a. `summaries_patient` rows (finish, sweep; M3);
12. `summaries_doctor_day` rows;
   - 12a. `summaries_money_day` rows, written just before the clinic-days (M3);
13. `summaries_clinic_day` rows.

One exception to the numbering: recording a procedure takes the invoice counter (11) **before** its stock items (9). No flow takes stock items and then the invoice counter, so this cannot cycle.

A flow writes **all** its changes to one summary table in **one statement**, in key order (doctor-days by (day, doctor_id), clinic-days by day), for example a payment on a past day's visit that changes the payment day's `collected` and the visit day's version.

Several rows of one table are locked in one statement in key order (`ORDER BY id FOR NO KEY UPDATE`; doctor-days by (day, doctor_id); clinic-days by day). Inserting a new row is not a lock anyone waits on, except through a unique index.

| Flow | Locks, in order |
|---|---|
| Booking (5.1), with optional arrive and payment | key → doctor membership `FOR SHARE` → [new patient: counter patient_file] → [payment: counter invoice, taken here and kept for `ensure_invoice`] → [cash: open shift `FOR SHARE`, or opened] → doctor_day (one upsert: turn, booked, collected) → money_day → clinic_day (one upsert) |
| Status change (5.2) | key → appointment (conditional UPDATE) → [finish: summaries_patient] → doctor_day → clinic_day |
| Finish with payment | key → appointment → invoice (or counter invoice for a new one) → doctor_day → clinic_day |
| Reschedule | key → appointment → target doctor membership `FOR SHARE` → both doctor_day rows in one statement → both clinic_day rows in one statement |
| Cancel | key → appointment → invoice (read; void if empty) → doctor_day → clinic_day |
| Payment, line, discount, void (5.3) | key → appointment → invoice (or counter invoice) → [payment: open shift `FOR SHARE` (other methods: in the INSERT, if one is open; cash: opened if none is)] → [void, remove a package line: its patient packages; remove a procedure line that used a session: that package, to give the session back] → doctor_day → money_day → clinic_day |
| Refund | key → appointment → invoice → the payment → [open shift `FOR SHARE`; cash: opened if none is] → doctor_day (refund day) → money_day → clinic_day (refund day and the appointment's day, one statement) |
| Stock use, several items (5.4) | key → items in one statement by id → clinic_day (only if an item crosses its threshold) |
| Purchase, adjustment | key → [purchase from the drawer: open shift `FOR SHARE`, opened if none is] → item → [purchase: expense insert] → clinic_day (purchase: expenses) |
| Expense and its void | key → [from the drawer: open shift `FOR SHARE`, opened if none is] → expense → clinic_day |
| Cash in or out, payout (F1, F4) | key → [cash: open shift `FOR SHARE`, opened if none is] → the row |
| Close the shift (F1) | key → open shift `FOR NO KEY UPDATE` → sum what it holds (no locks) → update; nothing after it |
| Prescription (5.5) | key → [medicines added from it: insert, `ON CONFLICT DO NOTHING` on the active-name index] → counter prescription |
| Visit note (5.6) | key → note (`FOR NO KEY UPDATE`, then conditional on version) → [after the visit day: revision insert] |
| Finish with earlier balance and next visit (E3) | key → appointment (conditional UPDATE) → the visit's invoice and the patient's other open invoices with something owed, one statement by id → [counter invoice] → next visit's doctor membership `FOR SHARE` → doctor_day (one upsert: done, collected, the next visit's turn) → clinic_day (one upsert). The membership comes after the invoices here; no cycle, because schedule changes (the only `FOR NO KEY UPDATE` holders of a membership) never lock an invoice. Older invoices are locked without their appointments; flows that lock an appointment then its invoice never wait on this visit's appointment afterwards |
| Vitals (E3) | key → vitals row (insert `ON CONFLICT DO NOTHING`, or UPDATE conditional on version) |
| Record a procedure (E3, F2, F7) | key → appointment → plan item (`FOR NO KEY UPDATE`) → [a session: patient package] → invoice (or counter invoice) → stock items in one statement by id → tooth row (upsert) → doctor_day → clinic_day |
| Tooth state (E3) | key → tooth row (`FOR NO KEY UPDATE`, then upsert) |
| Treatment plan (E3) | key → plan row (`FOR NO KEY UPDATE`, for new items' positions) → its items; a status move is one conditional UPDATE on the item |
| Library rows: medicines, prescription templates, procedures (E3) | key → the row (template: `FOR NO KEY UPDATE`, conditional on version, then its lines replaced) |
| End-of-day sweep (5.10) | the day's open appointments in one statement by id → for in-room visits without an invoice, counter invoice → that clinic-day's doctor_day rows in one statement → clinic_day |
| Recompute (5.10) | that clinic-day's doctor_day rows `FOR UPDATE` in one statement → its money_day rows `FOR UPDATE` → clinic_day `FOR UPDATE` → aggregate the source rows, the day's non-voided expenses included (no locks) → write |
| Schedule change (create, update, delete) | key → doctor membership(s) `FOR NO KEY UPDATE` in id order → the session row → read the doctor's future bookings (no locks, every one, in keyset pages) → clinic_day (today's version) |
| Patient edit | key → patient (conditional on the expected values) → clinic_day (today's version) |
| Settings change | key → clinic row → [time zone or day end: the directory's copy after the shard commits] |

No cycle comes from the membership rows: reschedule goes appointment → membership, while booking and schedule changes take no appointment lock before the membership, and two `FOR SHARE` locks don't conflict. A schedule change never locks an appointment, a counter or a doctor_day row.

- **Deadlocks** (`40P01`): the server retries the whole transaction once. **Lock timeouts** (`55P03`): the request has already waited 2 s, so it answers 409 `busy` with `Retry-After`, and the client resends the same key.
- **Errors:** services raise `ApiError(code, message, status)`. Constraint violations that users can cause map to 409 with a code. Anything else is a 500, logged with the request id, and never retried by the client.

### 5.1 Booking

`POST /api/v1/appointments`: patient (an id, or a new patient's name, phone, sex and age), doctor, service, optionally day (default: the current business day) and a slot time, source, `arrive` (bool), and an optional payment `{amount, method}`.

1. Validate:
   - the doctor is an active owner or doctor who works that weekday;
   - the service is active and in this clinic;
   - the day is the current business day or up to a year ahead;
   - for slot doctors, the time is on that weekday's grid;
   - a new patient needs `patients.edit`, and a payment needs `billing.collect`.
2. A new patient gets a file number from the counter. With a payment, the invoice number is taken right after it and kept for `ensure_invoice`, so the counters come before the summary rows, as in every other flow. A payment inside a booking is accepted only for the current business day; a prepayment for a later day uses the payment endpoint after booking.
3. **Fee and the follow-up rule** (an assumption for Saged, R4.1):
   - a consultation for a patient whose last consultation with the same doctor arrived or was paid within the follow-up window, counted back from the new appointment's day, uses the price of the consultation's `follow_up_service_id`, and `is_follow_up` is set;
   - a follow-up doesn't start a new window;
   - the rule is checked again at arrival.
   This costs one indexed query on the patient's history.
4. **Turn:**
   ```sql
   INSERT INTO summaries_doctor_day (clinic_id, day, doctor_id, last_queue_no, booked, …)
   VALUES (:c, :d, :doc, 1, 1, …)
   ON CONFLICT (clinic_id, day, doctor_id) DO UPDATE
     SET last_queue_no = summaries_doctor_day.last_queue_no + 1,
         booked = summaries_doctor_day.booked + 1
     WHERE :max IS NULL OR summaries_doctor_day.booked < :max
   RETURNING last_queue_no
   ```
   No row returned means the day is full: 409 `day_full`.
5. Insert the appointment. A taken slot violates the slot index; the whole transaction rolls back, the turn number included, and the answer is 409 `slot_taken`.
6. With `arrive`: status `waiting` and arrived_at now (only on the current business day). With a payment: the payment flow in 5.3 inside the same transaction, using the invoice number from step 2. The turn, `booked` and `collected` go into the step 4 upsert, so `doctor_day` is written once.
7. `clinic_day`: version + 1 (and `collected` with a payment), in one upsert.

The answer is the new row (turn, invoice id, amount due) and the new version.

`GET /api/v1/appointments/quote?patient=&doctor=&service=&day=` returns the fee and `is_follow_up` for the Book dialog, using the same rule.

A schedule change that would leave a future slot booking off the grid, or outside the doctor's hours, is refused with the list of affected bookings. Every future booking is checked (in keyset pages), and a slot booking is covered only when its start and its end are a slot of the new grid. The database also refuses two slot visits of one doctor that overlap (`appointment_no_overlap`, an exclusion constraint on `tstzrange(starts_at, ends_at)` where `holds_slot`), answered as 409 `slot_taken`.

### 5.2 Status changes

| Action | From | To | Sets | doctor_day | clinic_day |
|---|---|---|---|---|---|
| arrive | booked | waiting | arrived_at | | version |
| undo_arrive (pending Saged) | waiting | booked | clears arrived_at | | version |
| call | waiting | in_room | called_at | | version |
| finish (optional payment) | in_room | done | finished_at | done + 1 | done + 1, version |
| cancel | booked | cancelled | cancelled_at, reason | booked − 1 | version |
| no_show (sweep only) | booked | no_show | | | version |
| reschedule | booked | booked | new day, doctor, slot, turn | old row booked − 1, new row booked + 1 | version on both days |

- The client sends the status it saw. The service runs `UPDATE … SET status = :to … WHERE clinic_id = :c AND id = :id AND status = :from RETURNING …`.
  - One row: update the counters and bump the version.
  - No row: read the appointment. Missing is 404. Already in the target status is 200 (a harmless repeat). Anything else is 409 `status_changed` with the current status.
- **arrive** is allowed only on the appointment's business day.
- **cancel:** while the visit's invoice has `paid > 0`, the answer is 409 `refund_first`. An open invoice with nothing paid is voided in the same transaction.
- **reschedule:**
  - a target equal to the current doctor, day and slot is a 200 that changes nothing;
  - a move within the same doctor-day keeps its turn number;
  - otherwise it takes a new turn on the new doctor-day, so the old number is a gap (turn numbers may have gaps; invoice, file and prescription numbers may not);
  - `last_queue_no` never goes down.
- **finish** with a payment runs the 5.3 payment flow in the same transaction (R4.3).
  - If someone else already finished the visit, a finish **with a payment** is not a harmless repeat: its cash was never recorded. It answers 409 `already_finished` with the current appointment and its invoice, and writes nothing. The desk then takes the payment on the done visit through the payments endpoint. A finish without a payment stays a harmless 200.
- Every action answers with the updated row and the new version.

### 5.3 Invoices and payments

- **ensure_invoice** (under the appointment lock): read the visit's open invoice; if there is none, take a number from the counter and insert it, with one line for the service at the appointment's fee. Every billing action calls it: a payment, a line, a discount, finish. Nothing at arrival, so no-shows take no numbers.
- **Payment** `POST /api/v1/appointments/{id}/payments` `{amount, method}`:
  1. lock the appointment (`FOR NO KEY UPDATE`); refuse a cancelled visit;
  2. ensure_invoice;
  3. `UPDATE billing_invoice SET paid = paid + :a WHERE clinic_id = :c AND id = :i AND status = 'open' AND paid + :a <= total`. No row is 409 `over_balance`;
  4. insert the payment with the patient, the appointment's doctor and the business day;
  5. `doctor_day.collected` (payment day, doctor) and `clinic_day.collected` (payment day) go up; the version of the appointment's day is bumped.
- **Lines:** `POST /api/v1/invoices/{id}/lines` adds a line. A list price needs `billing.collect`; any other price needs `billing.discount`. Each line records who added it (`created_by_id`, the membership) and when (`created_at`).
- **Removing a line:** `DELETE /api/v1/invoices/{id}/lines/{line}`, only while `paid` stays <= the new total. Who may:
  - **the visit's own service line** (kind `service`, for the visit's service) always needs `billing.discount`: removing it is a 100% discount;
  - **any other line** needs `billing.discount`, unless the same membership added it on the same business day, so the desk can fix its own mis-tap with `billing.collect`.
  - Every line in an invoice's answer carries `removable` for the person asking, and the screen offers "Remove" only then. A refusal is 403. The removal is audited.
- **Finish after finish:** a finish with a payment on a visit someone else already finished answers 409 `already_finished` (5.2); nothing is collected.
- **Discount:** `PUT /api/v1/invoices/{id}/discount` with `{discount, expected_discount}`, absolute, under `billing.discount`. It is refused if the expected value doesn't match or if `paid` would exceed the new total. Audited.
- **Discount types** (Saged, 2026-11-13; `billing/discounts.py`): the clinic defines up to 12 active types (`billing_discount_type`: name, `percent` 1–100 or `fixed` piastres, `desk_can_apply`, active, sort), managed under `settings.manage` (`GET`/`POST discount-types`, `PATCH discount-types/{id}`; the clinic row is locked, order 6, so the cap holds).
  - **Apply:** `PUT /api/v1/invoices/{id}/discount-type` with `{type_id | null, expected_discount}` under `billing.collect`; a type without `desk_can_apply` also needs `billing.discount`. Without `billing.discount` a person replaces or removes only no discount or a desk type. The same type again is a harmless repeat; `null` removes it. Same locks, stale check and audit as the typed discount, which stays under `billing.discount` and clears the type.
  - **The invoice keeps** `discount_type_id`, `discount_name` and `discount_percent` as they were when applied (the receipt prints the name; editing a type changes no invoice). CHECK: with a percent, `discount = (subtotal * percent + 50) / 100`. Adding or removing a line computes a percent again in the same statement (`move_subtotal`); any other discount must fit what is left before a line is removed (`discount_exceeds`).
  - **Suggest:** `PUT /api/v1/appointments/{id}/suggested-discount` with `{type_id | null}` under `procedures.record`, on the locked appointment (`suggested_discount_type_id`), for a visit that has arrived. It changes no money and no version. `GET appointments/{id}/discount-types` (`billing.collect`) gives the Pay dialog the active types and the suggestion.
- **Refund** (`billing.refund`): lock the appointment, then the invoice; `UPDATE billing_payment SET refunded = refunded + :a WHERE … AND refunded + :a <= amount`; insert a refund row (its own business day, the original's doctor); take the amount off `invoice.paid`; `doctor_day.refunded` and `clinic_day.refunded` go up on the refund's day. Audited.
- **Void** (`billing.refund`): only while paid = 0. Audited. A void invoice no longer blocks a new one for the visit, since the one-invoice index covers only open invoices.
- Revenue is counted when cash comes in (`payment.day`). Net revenue = collected − refunded, per doctor and per clinic.

**Counters each action changes.** The recompute (5.10) and the invariant tests (T7) both read this table. Every row also sets `changed_since_recompute` on each `clinic_day` it touches.

| Action | `doctor_day` | `clinic_day` | Recompute formula |
|---|---|---|---|
| book | appointment's day and doctor: `last_queue_no` + 1, booked + 1 | appointment's day: version | booked = appointments of that doctor-day not cancelled; `last_queue_no` is never lowered |
| arrive, undo_arrive, call | | version | |
| finish | done + 1 | done + 1, version | done = appointments with status done |
| cancel | booked − 1 | version | (as book) |
| reschedule | old row booked − 1, new row booked + 1 and a turn (one statement) | version on both days (one statement) | (as book) |
| sweep: booked → no_show; waiting → no_show; in_room → done | done + 1 for in-room visits | done, version | (as above) |
| pay | payment day, appointment's doctor: collected + a | payment day: collected + a; appointment's day: version (one statement) | collected = Σ payment rows of that day (and doctor) |
| refund | refund day, original doctor: refunded + a | refund day: refunded + a; appointment's day: version | refunded = Σ refund rows of that day (and doctor) |
| line, discount, void | | appointment's day: version | |
| expense; its void | | expense day: expenses ± amount | expenses = Σ expenses of that day not voided |
| purchase | | today: expenses + amount | (as expense) |
| stock use crossing a threshold | | today: version | |
| patient edit, schedule change | | today: version | |

### 5.4 Stock

- **Use**, `POST /api/v1/inventory/uses` with one or more `{item, qty}`, optionally for a patient or appointment:
  - lock the items in one statement by id;
  - for each, `UPDATE inventory_item SET quantity = quantity - :q WHERE clinic_id = :c AND id = :i AND quantity >= :q RETURNING quantity, unit_cost, threshold`. No row is 409 with the quantity left;
  - a `use` movement per item, with the cost at that moment;
  - bump the version only if an item fell to or below its threshold (Today's banner).
  - The `CHECK` stops a negative quantity even if a service forgets the condition.
- **Purchase:** quantity up, the unit cost set, an expense (`purchase = true`), a `purchase` movement, and `clinic_day.expenses`.
- **Adjustment** (`inventory.manage`): `UPDATE … SET quantity = :counted WHERE … AND quantity = :expected`, an `adjust` movement with the difference and the reason, and an audit event.

### 5.5 Prescriptions

- 1 to 30 items. Each is a medicine from the list (copied) or typed text, with the dose, the amount (1–200), the days (1–365) and instructions. It needs `clinical.write`.
- The number comes from the counter. One insert for the prescription and one bulk insert for the items.
- The print page `/print/prescriptions/<id>?lang=ar|en` (default: the clinic's print language) writes an access event.

### 5.6 Visit notes

- A note has a `version`. `PUT /api/v1/appointments/{id}/note` sends `If-Match: <version>`. The update is conditional on the version. A mismatch is 409 with the current text, which the screen shows next to the draft to merge.
- An edit after the visit's business day first copies the old text into `clinical_visit_note_revision`.
- Reading a note needs `clinical.view` and writes an access event.

### 5.7 Patient search

- **Normalising**, in Python (`core/text.py`), for both stored names and queries: Arabic-Indic digits become 0–9; the forms of alef become ا, ة becomes ه, ى becomes ي, ؤ becomes و, ئ becomes ي; diacritics and tatweel are removed; text is case-folded and spaces are collapsed.
- **A query of digits:** phone_norm by prefix (normalised from `+20`, `0020` and `20`), or file_no exactly.
- **A name query:**
  1. prefix, written as a range so it keeps its index range under RLS: `name_norm >= :q AND name_norm < :q_next`, limit 20;
  2. if that found fewer than 20, a word prefix: `name_norm LIKE '% ' || :q || '%'` with `%`, `_` and `\` escaped, filling up to 20.
  - The second query scans only this clinic's part of the covering index, as an index-only scan: about 17 ms for 100,000 files when vacuumed (verified by two reviewers).
  - Search runs with `SET LOCAL statement_timeout = '300ms'`.
- **The client:** a 250 ms debounce, at least 2 characters, the previous request aborted.
- Results show the name, phone, file number, age, sex, any booking today and the last visit. Picking a result doesn't open the file, so it writes no access event.
- **Duplicate phone:** `GET /api/v1/patients/by-phone?phone=` lists this clinic's files with that phone. The Book dialog shows them before a new file is created (R4.2).
- M1 checks the plans of both queries for a 100,000-file clinic as the web role.
- Clinics past about 200,000 files can get a word table `(clinic_id, word, patient_id)` that a job fills (deferred).

### 5.8 Team, permissions and specialty

- **Granting:**
  - only `team.manage` changes members or permissions;
  - a non-owner can give, remove or reach through a role change only codes they hold themselves (`new − current ⊆ actor's codes`);
  - only the owner gives or removes the owner-only codes (4.2);
  - nobody edits their own membership; only the owner edits a member who holds `team.manage`;
  - the owner can't be removed or demoted; ownership moves only through the platform admin, to an active doctor of the clinic (5.12).
- **Adding a member:**
  - a new account: created in the directory with a temporary password that expires in 72 hours (5.12);
  - an existing account (M3): an invitation its holder accepts. The answer is the same for known and unknown usernames;
  - clinic endpoints change memberships, and may create a new account, but never change an existing account.
- Every change writes an `audit_change` event with the before and after. Permissions are read from the membership row on every request, so a change applies at once.
- **Changing the specialty** (M3) copies the new template's services, medicines and stock items that the clinic doesn't have yet. It never deletes or deactivates anything. Audited.

### 5.9 Signing in

1. `POST /api/v1/auth/login` with a username or phone and a password.
   - Throttles in PostgreSQL (`accounts_throttle`), counting failures only:
     - 5 per (username, IP) in 15 minutes;
     - 20 per username in 15 minutes;
     - 200 per IP in 15 minutes, loose because clinic routers and carrier NAT share addresses.
   - When a limit is hit, the answer says how long to wait. The throttle fails closed. Redis can only speed it up.
   - An unknown username still runs a dummy password check. One generic error for a wrong username or password.
2. **Two-factor codes** are required for owners and platform admins, and for every member when the clinic's setting says so.
   - After the password, the session holds a pending sign-in (user id, expires in 5 minutes). It doesn't count as signed in for `@api`.
   - `POST /api/v1/auth/totp` accepts a code (±1 step) by `UPDATE accounts_user SET totp_last_step = :s WHERE id = :u AND totp_last_step < :s`, so one code can't be used twice, even on two containers. A recovery code also works, once.
   - Five wrong codes end the pending sign-in and count toward the username limit.
   - After the code, the session key changes and `mfa_verified` is set.
3. `@api` refuses everything except changing the password while `must_change_password` is set, and everything except code set-up while a required code isn't set up. The password must be changed before the code is set up. Code set-up shows the QR code (segno, as SVG) and 10 recovery codes, which are stored hashed.
4. **The active clinic:** with one active membership it becomes the active one; with several, the picker. `POST /api/v1/me/clinic` re-checks the membership on the shard. When the chosen clinic requires a code and `mfa_verified` isn't set, the code is asked for first.
5. **Sessions:** the `db` backend until a shared Redis exists, and `cached_db` only over Redis: a per-process cache would keep a signed-out session valid on other workers. A fixed 12 hours, never saved by a poll. `GET /api/v1/me` returns `session_expires_at`. A password change signs out the other sessions.
- TOTP secrets are encrypted with an application key (`HAMADA_FIELD_KEY`, AES-GCM through Python's standard library primitives or a vetted package; rotation is a runbook).

### 5.10 Jobs

All periodic jobs fan out by shard and by batches of about 500 clinics, with one transaction per clinic.

- **End-of-day sweep**, per clinic after `day_ends_at` plus 30 minutes, for the business day just ended:
  - `booked` → `no_show`;
  - `waiting` → `no_show` with `arrived_at` kept and `auto_closed` set ("left without being seen", pending Saged);
  - `in_room` → `done` with `auto_closed` set, its invoice ensured and flagged for the owner.
- **Recompute**, one clinic-day per transaction, for every past day whose `changed_since_recompute` is set. It runs after the business day ends. It locks the summary rows `FOR UPDATE` first, then aggregates the source rows, then writes. A difference is fixed, logged, and counted in a metric.
- **Partition upkeep**, daily (`core.ensure_partitions`): the current month and 6 more on the directory and every shard, with an alert below 3 (4.10). `migrate_all` runs it too.
- **Request-key cleanup**, daily in the 04:00–09:00 Cairo window (`core.clean_request_keys`): rows older than 72 hours, in batches of about 5,000. Until a `hamada_maint` alias exists it walks each shard's places from the directory, one transaction per place under row-level security.
- Expired sessions and throttles, and the `user_clinic` reconciliation, as `hamada_maint`.
- Jobs run with a system `Ctx`. Their audit actor is "system", and their `created_by_id` is null.
- The Procrastinate worker connects straight to PostgreSQL for LISTEN, not through PgBouncer's transaction mode; or it polls.

### 5.11 The live queue

- **The page:** `/today` renders the shell. Its queue is the fragment `/today/queue`, polled with `If-None-Match`.
- **The ETag:** `"<clinic>.<day>.<version>.<hash of the person's codes and language>"`. Without `?day=`, the server uses the current business day. The answer carries the day, the server time and the next rollover, so at the rollover the ETag changes and a normal 200 arrives.
- **The server reads the version before the data**, so a commit landing in between never pairs a new ETag with old data.
- **The real cost of a 304** in stage 1: the session and the user (directory, 2 queries), `set_config`, the membership with the clinic, and the version (shard, 3 statements). There's no membership cache. From stage 2, `cached_db` takes the session off the database. A Redis version path waits for the M5 load test; if built, it only ever sets a higher value, with a 30–60 s TTL.
- **Every answer, 304s included, carries `Poll-After`** (seconds), so operations can slow every screen at once. `Cache-Control: no-store` on every answer.
- **What bumps the version:** every status change, booking, reschedule, cancel; every payment, refund, line, discount and void (on the appointment's day); a patient edit (today's version); stock use that crosses a threshold; a schedule change (today's version).
- **The full fragment:**
  - the doctors working that day;
  - the day's appointments, with patient name, phone, file number, service, status, `is_new`, `is_follow_up`, the invoice id and the amount due (one query with joins inside the clinic, at most 1,000 rows);
  - the doctor-day rows;
  - the low-stock items (a scan of the clinic's items);
  - the last 10 changes, worked out from the fetched rows.
- Money totals appear only for people with `finances.view`.

### 5.12 Flows across the directory and a shard

Each is a sequence of idempotent steps with its own request key. A failed step is safe to run again.

- **Create a clinic** (platform admin, M1):
  1. directory: insert `directory_clinic` (the id, the least-full shard, status `active`);
  2. directory: create the owner's account if new, with a temporary password expiring in 72 hours and `must_change_password`;
  3. shard: insert `clinics_clinic` (`owned`), the owner's membership, the specialty template's services, medicines and stock items, the counters and the categories;
  4. directory: insert the owner's `user_clinic` row.
  A step that finds its row already there succeeds without changing it. A nightly reconciliation repairs a half-finished run.
- **Add a member with a new account** (team, M3): directory account first, then the shard membership, then `user_clinic`.
  - The account step reuses an account **only if this same request created it**: the account records the creating clinic and request key until its first sign-in. Any other existing username goes through the invitation path, with the same answer, so the step never attaches someone else's account or reveals that a username exists.
  - A replay shows no temporary password, and offers to issue a new one.
- The shard step of "create a clinic" is idempotent by clinic id and claims no shard request key (the clinic row doesn't exist yet); the platform request key in the directory covers the whole flow.
- **Suspend or reactivate a clinic:** directory status first, then the shard's `clinics_clinic.status`. Requests check the shard's status.
- **Transfer ownership, support resets** (platform admin): a `directory_support_action` with a reason, a second admin's approval, an `audit_change` event in the clinic's log, and a notice to the account at its next sign-in. Ownership moves only to an active doctor of the clinic. Django's admin site is off in production.
- Platform admins see clinic names, statuses and usage counts (from `clinic_day`, read as `hamada_maint`), never patient data.
