# Hamada v2 design, draft 2: chief judge's check

Date: 2026-11-07. Checks `docs/v2/design.md` and `requirements.md` at commit 1b2eb9e against the 29 changes in `draft1-chief-judge.md`. The in-progress code in `v2/` was not reviewed. From the skeleton (commit 328bccd) I read only `accounts/models.py` and `config/settings.py`, plus Django 6.1.3's `cached_db` session backend.

**Experiments:** none. PostgreSQL wasn't running on 127.0.0.1:5433 (no listener on 5432–5434, no postgres process), and I may not start it. The findings rest on four sources:
- the text of draft 2, by section;
- PostgreSQL's documented rules: `ATTACH`/`DETACH PARTITION` require owning the parent, and partitions have their own RLS;
- Django's source;
- SQL verified in draft 1. The turn upsert in 5.1 now qualifies every column, which is the form I verified works.

Checked and fine:
- `totp_last_step` is `NOT NULL DEFAULT 0`, so the conditional update in 5.9 accepts a first code.
- The skeleton already uses the `db` session backend when there's no Redis (see B3).

## 1. The 29 changes

| # | Change | Status | What is missing or wrong |
|---|---|---|---|
| 1 | Application-level shards | Applied | The clinic-move procedure can lose writes (B5) |
| 2 | Key and index rules | Partly | `UNIQUE (clinic_id, id)` missing on 5 referenced tables (I1) |
| 3 | RLS and database roles | Applied | New partitions and the upkeep role (B4) |
| 4 | Safe repeats, server | Applied | D2's `status` column and temporary-password replay rule dropped (I3, B6); the 24-hour client limit dropped (B2) |
| 5 | Safe repeats, browser | Partly | 409 `busy` treated as final; stored requests never expire (B2) |
| 6 | Front-end architecture | Applied | |
| 7 | Arabic, numbers, bidi | Applied | `,` as decimal point is ambiguous (I10) |
| 8 | Permissions and grants | Applied | The 5.8 formula checks only codes being added (I7) |
| 9 | Accounts, platform admin | Partly | 5.12 attaches an existing account without an invitation (B6) |
| 10 | Sign-in, sessions, codes | Partly | "`cached_db` only" with no shared cache (B3); the rest is applied |
| 11 | Request context | Applied | Wording for page navigations (I9) |
| 12 | Health-data controls | Applied | "Patient fields" undefined; Today and search return names and phones (I9) |
| 13 | Audit and access logs | Applied | Partition upkeep as `hamada_maint` can't run (B4) |
| 14 | The business day | Applied | |
| 15 | Jobs and connections | Applied | Job roles and the fan-out source unstated (I6) |
| 16 | Lock order and lock table | Partly | Booking breaks the global order (B1); refund text, sweep row, expenses (I4) |
| 17 | Invoices, payments, counters | Partly | The counters table covers only status actions (I2) |
| 18 | Appointment table shape | Applied | |
| 19 | Patient search | Applied | Phone prefix should also be a range predicate (I8) |
| 20 | Follow-up price rule | Applied | The check at arrival against an existing invoice (I5) |
| 21 | Front desk in one request | Applied | "Arrive and collect" is two requests (I5) |
| 22 | Reads the screens need | Applied | |
| 23 | Today and the live queue | Applied | |
| 24 | Printing | Applied | |
| 25 | Tests | Applied | Add the cases B1 and B3 need |
| 26 | Visit notes and drafts | Applied | |
| 27 | Availability and runbooks | Applied | |
| 28 | Scale model | Applied | |
| 29 | Requirements map, milestones | Applied | 6.2 lists `undo-arrive` as M1 work without "pending" (I11) |

Result: 23 applied, 6 partly applied, none missing. Draft 2 is a faithful rewrite. The problems are in flows built from several changes at once.

## 2. Blocking problems

**B1. Booking with payment breaks the global lock order (5.1 against 5.0). Backend, M1.**
- 5.0's table has booking take the invoice counter before `doctor_day`, but 5.1 upserts `doctor_day` in step 4 and only reaches the counter in step 6, through the payment flow's `ensure_invoice`.
- Finish with payment takes the counter, then the same `doctor_day`. So "Book, collect and print", the default button (7.3), deadlocks against a finish with payment for the same doctor and day.
- A future-day booking with prepayment locks two `doctor_day` rows (booking day, then today) in two statements, against key order. A crossing reschedule locks them in key order, so the two deadlock.
- The table's order can't be followed literally either: `ensure_invoice` needs the appointment, which needs the turn.
- **Fix:**
  - with a payment, take the invoice number right after the `patient_file` counter and keep it for `ensure_invoice`;
  - write all of a flow's `doctor_day` changes in one statement in (day, doctor_id) order, and its `clinic_day` changes in one statement in day order (or accept a payment inside a booking only for the current business day);
  - add both deadlock pairs to T5.

**B2. The browser can repeat an action (7.4 against 5.0 and D2). Frontend, M0 (`api.js`).**
- 5.0 has the client resend the same key after 409 `busy`. 7.4 ends a key at "a 4xx that wrote nothing" and doesn't retry 409.
- So: the first copy is slow and holds the claim, the resend times out with 409 `busy`, the form drops its key, the user taps again under a new key, and both copies commit (two bookings or two payments).
- D2's 24-hour limit on pending resends is also missing. A request kept in `sessionStorage` can be replayed after a weekend, through the sign-in overlay or "try again". Its key row is gone after 72 hours, so the action runs twice.
- **Fix:**
  - 409 `busy` and 503 are never final: resend the same key after `Retry-After`;
  - store `created_at` with each pending request and never resend one older than 24 hours ("outcome unknown, check the record");
  - optionally, use UUIDv7 keys and have the server refuse keys older than 48 hours with 422.

**B3. Sign-out doesn't take effect across processes (5.9). Backend, M0.**
- 5.9 says "`cached_db` only", and stage 1 has no Redis (3.1), which means a per-process cache.
- In Django 6.1.3, `cached_db` reads the cache first, and `delete()` clears only the local process's copy, which lives as long as the session. So a sign-out, or "a password change signs out the other sessions", leaves the session valid on other Gunicorn workers for up to 12 hours.
- My change 10 introduced this wording; it was wrong for stage 1.
- **Fix:** use the `db` backend until a shared Redis exists, and `cached_db` only over Redis (5.11 already assumes this for stage 2). Keep the skeleton's settings (lines 116–123). Add to T10: a session signed out through one process is refused by another.

**B4. Partition upkeep can't run as `hamada_maint` (4.0, 4.10, 5.10). Database, M0.**
- `ATTACH` and `DETACH PARTITION` are `ALTER TABLE` on the parent, which requires owning it, and `hamada_owner` owns it.
- The obvious workaround, making `hamada_maint` a member of `hamada_owner`, gives a BYPASSRLS role full rights on patient tables and undoes D4.
- New partitions also need their own `ENABLE`/`FORCE` and policy. Otherwise T11 fails, or a direct read of a partition skips the policy.
- Development and tests use a single owner role, so no M0 test will show this; production will.
- **Fix:** partition DDL runs as `hamada_owner`, from a job like `migrate_all`. `hamada_maint` keeps DML-only upkeep and is never a member of `hamada_owner`. Each partition gets RLS and the policy before it is attached. CI runs upkeep once under the production roles.

**B5. Moving a clinic can lose writes (3.4, 3.3, 5.0). Database and backend: text now, plus the M0 request check.**
- Step 1 freezes only the directory status. A stale directory cache is exactly the case `owned` exists for, yet the source keeps `owned = true` until step 4.
- A write routed by a stale cache therefore commits on the source after the copy, and step 5 deletes it.
- After step 5 there is no source clinic row. A stale POST claims its key before the clinic check (5.0, steps 3–4), fails the key table's foreign key to `clinics_clinic`, and gets a 500, which is never retried.
- **Fix:**
  - step 1 also clears `owned` on the source;
  - wait until no transaction older than that change is still running (`pg_stat_activity.xact_start`), then copy;
  - check the clinic before claiming the key; a missing clinic row counts as not owned (503);
  - any answer other than 2xx rolls back, not only 4xx.

**B6. Adding a member with a new account takes over existing accounts (5.12 against 5.8 and change 9). Backend: text now, built in M3.**
- 5.12 says "a step that finds its row already there succeeds without changing it". Applied to the account step, that attaches any existing username without an invitation, and the different answer reveals that the username exists.
- 5.8 also says clinic endpoints "never change `accounts_user`", yet it has them create accounts.
- **Fix:**
  - the account step reuses an account only if this same request created it: record the creating clinic and key on the account until its first sign-in;
  - any other existing username takes the invitation path, with the same answer;
  - reword the rule to "never change an existing account";
  - a replay shows no temporary password and offers to issue a new one (D2).

## 3. Improvements (not blocking; I1–I4 are cheap to fix alongside the blockers)

1. Database.
   - Add `UNIQUE (clinic_id, id)` on `billing_payment`, `inventory_item`, `clinical_visit_note`, `finance_category` and `finance_expense`. Without it, the composite foreign keys to them (`refund_of_id`, `item_id`, `note_id`, `category_id`, `expense_id`) can't be created.
   - End the audit list indexes with `id`, e.g. `(clinic_id, patient_id, at, id)`. My change 13 conflicted with change 2 here.
2. Backend.
   - Extend 5.2's table into change 17's counters table: book, pay, refund, line, discount, void, expense and its void, purchase, stock use, the sweep's three transitions, and each counter's recompute formula.
   - Add to `clinic_day` (4.4) the changed-since-recompute column that 5.10 relies on.
   - Pick one clinic-day per transaction or one clinic per transaction; 5.10 currently says both.
3. Backend.
   - Keep D2's `status` column on `core_request_key`.
   - Name the endpoints that need no key: sign-in, code, sign-out. A replayed code request must never sign anyone in again.
4. Backend.
   - The refund text (5.3) locks the payment before the invoice; align it with the table (invoice, then payment).
   - The sweep's row omits the invoice and the counter, but 5.10 creates invoices.
   - Add expenses to the global order, after inventory items.
   - A payment on a past day's visit updates both `clinic_day` rows in one statement.
5. Backend.
   - At arrival, change the fee only while the visit has no invoice; otherwise keep the invoice and flag the visit.
   - Give `arrive` the optional payment, with its own lock row, so "arrive and collect" is one request.
6. Backend and database.
   - Per-clinic jobs run as `hamada_worker` with `set_config` in each transaction.
   - Say how one process uses two roles (e.g. a per-shard maintenance alias).
   - Copy `timezone` and `day_ends_at` into `directory_clinic`, so the sweep visits only clinics whose day has ended.
   - Copy platform usage counts into the directory with a job, so the web tier never holds `hamada_maint` credentials (5.12).
   - The sequence-range step never restarts a sequence that is already in range.
7. Backend. The grant rule should be `(new Δ current) ⊆ actor's codes`, so removals are checked as well as additions.
8. Backend. Write the phone prefix search (5.7) as a range predicate, so it keeps its index range under RLS.
9. Backend.
   - Define "patient fields" for the access test as file details and clinical content. The name, phone and file-number summary on Today and in search doesn't count.
   - `X-Membership` is required on fetches; page navigations use the session.
   - No patient name in any page's `<title>`, not only print pages.
10. Frontend and backend. The number parser refuses `1,500` (a comma followed by exactly three digits) instead of reading 1.50.
11. Backend.
    - Mark `undo-arrive` as pending in 6.2 and build it only after Saged answers (D5 m).
    - The shard step of "create a clinic" is idempotent by clinic id and claims no shard key, since the key table's foreign key would fail before the clinic row exists.

Still deferred: everything in section 5 of the draft 1 decision.

## 4. Verdict: REVISE

Blocking problems:
- B1: booking lock order;
- B2: repeats from the browser;
- B3: sessions across processes;
- B4: partition role and RLS;
- B5: clinic move;
- B6: existing accounts in add-member.

**Rationale.** All 29 changes are present, and the architecture holds. What's left are six flows where correct pieces were combined into one that fails:
- a deadlock on the default front-desk action;
- two ways to do an action twice;
- sign-outs that don't take effect;
- a role design that only works if production weakens it;
- a move that can drop writes;
- a back door into other people's accounts.

Each fix is a few sentences of text and changes no M0 table, so M0 continues. M0 code must follow B2, B3 and B4 before it merges. B1, B5 and B6 are text fixes due before M1, M5 and M3 respectively. Draft 2.1 needs a check of only these six items and I1–I4.