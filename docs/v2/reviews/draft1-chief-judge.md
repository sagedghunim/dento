# Hamada v2 design, draft 1: chief judge's decision

Date: 2026-11-07. Decides on `docs/v2/design.md` (draft 1) against `requirements.md`, `CLAUDE.md`, the four reviews in this folder and the standard of `smart-warehouse/docs/v2/design.md` (its 5.1 lock table, Appendices A and B, `core/idempotency.py`). Also read: the v2 skeleton in commit 328bccd (`config/settings.py`, `core/constraints.py`, `accounts/models.py`).

## 1. Verdict: REVISE

The shape is right and stays: one Django monolith, the clinic as tenant, composite foreign keys, conditional updates with CHECKs, summaries in the same transaction, ETag polling, logs partitioned by month. But open blockers remain on data integrity (invoice gaps, unsafe repeats, deadlocks, recompute drift, money on cancelled visits), tenant isolation (no database read isolation, one active clinic per session shared by tabs, grant escalation, global accounts), security (TOTP gaps, throttles that fail without Redis, unlogged clinical reads) and scale (a growth path that fails as written). Draft 2 applies the 29 changes in section 4 and adds an Appendix A mapping each to the sections that apply it. Changes 1–4 are specified here in enough detail to start M0 code while draft 2 is written.

## 2. What I checked myself

Scratch database `hamada_chief_scratch` on PostgreSQL 18.6 (C collation), dropped afterwards.
- Confirmed (judge 2f): the upsert printed in 5.1 fails ("column reference last_queue_no is ambiguous"); qualified, it works.
- Confirmed (database B3): the slot index's `status` predicate makes status changes non-HOT: 0 of 15,000 one-row updates were HOT. With a stored `holds_slot` column at fillfactor 85: 15,000 of 15,000.
- New: the day-partitioned key table proposed by backend B4, judge 1 and database I1 can't be unique on `(clinic_id, key)`: PostgreSQL refuses a unique key without the partition column, and with it a retry after midnight claims the same key again (both verified).
- New: under RLS, `name_norm LIKE 'q%'` loses its index range because `textlike` isn't leakproof: 852 buffers and 19 ms against 5 buffers and 0.09 ms without RLS (a 100,000-file clinic among 1.1M rows). Written as `name_norm >= 'q' AND name_norm < 'r'` (leakproof operators) it takes 8 buffers and 0.17 ms under RLS.
- New: a policy on `current_setting('app.clinic_id', true)::bigint` raises "invalid input syntax for type bigint" on a connection where an earlier transaction set it, because the setting reverts to an empty string rather than NULL. That is exactly the pooled-connection case. With `NULLIF(current_setting('app.clinic_id', true), '')` the same query returns no rows.
- Confirmed: a role without BYPASSRLS sees no rows without the setting, none for another clinic's id in WHERE, and can't insert another clinic's row.
- Accepted without rerunning, as two or three reviewers reproduced each independently: the invoice-number gap, the crossing-reschedule deadlock, ON CONFLICT needing the partial index predicate, the search index lacking `id`, the failing primary-key swap.
- Read: design 6.2 gives assistants no read of services or doctors (frontend 11); `patients.view` reaches prescriptions (backend B13b); no milestone has the platform admin (judge, R4.11); Procrastinate 3.10's Django contrib uses one alias but `DjangoConnector(alias=...)` exists; the skeleton sets timeouts as connection startup options, which don't reliably survive PgBouncer's transaction pooling (backend B12c).

## 3. Decisions on the disagreements

### D1. Growing past one server: application-level shards of whole clinics

| | Database B1: `PRIMARY KEY (clinic_id, id)` for Citus | Judge 6: `ForeignObject` or a lint, CI on Citus | Chosen: application-level shards |
|---|---|---|---|
| Runs on | The Citus extension; a managed offer may not exist where the legal review puts the data (R5, open question 5) | Same | Stock PostgreSQL 18, managed or self-run, anywhere |
| Django | `CompositePrimaryKey` plus `ForeignObject` (a `ForeignKey` to it is refused, E347), or model state that disagrees with the database | `ForeignObject` on every relation, or a lint | Plain `ForeignKey` on `id`; one database router |
| Tests on the Windows machine | A Citus container | A Citus container in CI | Two shard databases on the local PostgreSQL |
| Cost to the team | Citus upgrades, HA for the coordinator and every worker | Same | A router, migrations over every shard, a clinic-move tool, a few cross-database flows |

**Decision.** Each shard is an ordinary PostgreSQL cluster holding whole clinics. A small directory database holds the global tables. A Django router sends each request and each job transaction to its clinic's shard. Citus leaves the design (3.3, 12.2, 13, 14.2).

**Why, for this business.** What forces a split is the 2-hour restore (database review, scale), not throughput; a shard of whole clinics bounds exactly that, with the same backup, failover and PgBouncer runbooks stage 1 already needs. Nothing on a clinic's hot path crosses clinics, so the router costs nothing per request. The ORM stays plain Django, which a small team can maintain. The whole topology can be tested on the Windows machine today; Citus can't.

**Keys.** `id` (bigint identity) stays the primary key. Ids are unique across shards: shard s draws every identity sequence from [s * 2^48, (s+1) * 2^48), set with MINVALUE and MAXVALUE so exhaustion fails instead of colliding. A clinic's rows therefore move with their ids unchanged. `UNIQUE (clinic_id, id)` exists only on tables a composite foreign key references. No primary-key swap is ever needed.

**What M0 must already do:**
1. Two kinds of alias, `directory` and `shard_<n>`. Directory: users, sessions, login and account events, throttles, the clinic directory `(clinic_id, shard, status, name)`, a `user_clinic` copy for the picker, platform-admin tables, Procrastinate. Shard: `clinics_clinic` and every tenant table, memberships included.
2. `core/routing.py`: `allow_migrate` per alias; tenant models go to the shard named in the context that `@api`, the page decorator and the job runner set; a tenant query with no context raises and never falls back to a default shard.
3. Each shard's `clinics_clinic` row records whether that shard owns the clinic. A request that a stale directory cache sends to the wrong shard is refused (503 with `Retry-After`) and routed again, so a move can never split a clinic's writes.
4. No foreign key, join or transaction spans directory and shard. User ids on shard rows are plain integers. The flows that touch both (create a clinic, add a member with a new account, transfer ownership, suspend) are ordered idempotent steps; the shard is the authority for memberships, a nightly job reconciles `user_clinic`, and the picker re-checks on the shard.
5. A post-migrate step sets each shard's sequence ranges; a test fails if any sequence is out of range.
6. Tests and CI run with the directory and two shards as separate databases on one PostgreSQL 18; isolation, routing and repeat tests put clinics on both. `migrate_all` migrates the directory and every shard and reports each one's state.
7. Procrastinate lives in the directory. Periodic jobs fan out by shard and batch of clinics, one transaction per clinic on its shard. M0–M3 defer no job from a request; when M4 needs that, it chooses a per-shard outbox or a per-shard Procrastinate app, neither of which changes an M0 table.

Stage 1 in production is one cluster with two databases (`hamada_dir`, `hamada_s001`), so moving the directory out later is a dump of a small database. Draft 2 rewrites 3.3 as: one cluster; then a bigger server or a second shard, whichever the measured trigger says; then more shards. Triggers are measured limits (shard size against the restore drill, CPU, WAL rate), not doctor counts. New clinics go to the least-full shard. Moving a clinic (freeze, copy by `clinic_id`, verify, switch the directory, delete in batches, in the 04:00–09:00 Cairo window) is rehearsed in M5.

### D2. Safe repeats: one key table per database, rows kept 72 hours, not partitioned

**Decision.** The warehouse's `run_once`, adapted: a `core_request_key` table in every shard (and one in the directory for account and platform endpoints), claimed first, in the work's own transaction. Request keys leave the business tables.
- Row: `PRIMARY KEY (clinic_id, key)`, `membership_id`, `endpoint`, `request_hash` (SHA-256 of the canonical body and path), `target_type`, `target_id`, `status`, `created_at`; a BRIN index on `created_at`. No response body and no patient data: a replay rebuilds the answer from the target. A member created with a temporary password replays without it and offers to issue a new one.
- Flow: right after `set_config`, `INSERT ... ON CONFLICT DO NOTHING RETURNING`. Claimed: do the work, record the target. Conflict: the same endpoint, hash and membership replay; anything else is 422 `request_key_reused`. A concurrent copy waits on the claim, then replays; past `lock_timeout` it gets 409 `busy` and the client resends the same key.
- Refusals aren't stored: a 4xx rolls back the claim with the work, so nothing was written and the same key may run again. The warehouse stores refusals because a scan is a physical event; a desk action isn't.
- Required on every mutating POST, status actions and reschedule included, in a header the decorator reads. PUT and PATCH send absolute values with the expected old value or version.
- Retention: rows live at least 72 hours; a nightly job deletes older rows in batches of about 5,000 in the 04:00–09:00 window. The client gives up a pending resend after 24 hours.
- Not partitioned by day: a day-partitioned key can be claimed twice across midnight (section 2). Not keys on rows (draft 1): they leave 0.3–0.4 TB of permanent unique-index entries, don't cover lines, discounts or status changes, and carry no request hash.
- At 30M+ keyed requests a day (the full target): about 200 B a row with its index (estimate), so about 6 GB a day and about 18 GB live, spread over the shards. At 10 shards that is about 3M inserts and 3M deletes a day per shard, about 100 a second each at the evening peak, and one extra indexed insert per write transaction. Stage 1 (10,000 doctors): about 1.2M a day.

### D3. Front end: server-rendered Django pages with a few small JavaScript modules

**Decision:** the frontend engineer's proposal. No framework, no HTMX, no build step.
- Why: a small Django team keeps one skill set and one translation catalogue (`.po` compiled with polib; GNU gettext isn't available here). The server formats times, digits and money in the clinic's zone, so old PCs' time-zone data and `Intl` differences stop mattering. Django's test client covers pages and fragments on Windows. Every navigation loads current hashed assets. The warehouse built its v2 this way in 39.6 KB of JS and CSS. Interactivity is concentrated in Today and three dialogs, which small modules handle.
- What stays from draft 1: the JSON action endpoints under `/api/v1/` (pages save through one `api.js`), request keys, ETag polling, the CSP, no CDN, server print pages.
- Today's queue is an HTML fragment polled with the ETag and patched row by row by id; Book, Pay and Prescription are `<dialog>`s. Client code never builds HTML from strings: it inserts only same-origin server fragments and sets styles through `el.style`. JSON read endpoints for future native apps are added over the same selectors when needed (R6).
- Trade-off accepted: some JSON reads arrive later than with an SPA, and a fragment costs a little more CPU than JSON on a version change (2–5% of polls).

### D4. Row-level security: from M0, on every shard table that has `clinic_id`

**Decision:** the judge's position (judge 8), not stage 2 (database I9). R5 asks for isolation "by the database", this is health data, and adding RLS later means finding every read that runs outside a transaction: the rewrite to avoid. The cost is one `set_config` per transaction.
- Policy: `ENABLE` and `FORCE ROW LEVEL SECURITY`; one policy for `USING` and `WITH CHECK`: `clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::bigint` (`id = ...` on `clinics_clinic`). No setting means no rows, on fresh and reused connections (verified).
- Requests: `@api` and the page decorator open one transaction on the shard, run `SELECT set_config('app.clinic_id', %s, true)` first, load the membership, then run the view. Never a session `SET`.
- Transaction pooling: the setting ends with the transaction, so PgBouncer can give the connection to anyone after COMMIT. Timeouts become per-role settings (`ALTER ROLE ... SET lock_timeout, statement_timeout, idle_in_transaction_session_timeout`), replacing the skeleton's startup options.
- Roles: `hamada_owner` owns the tables and runs migrations (BYPASSRLS, so backfills work). `hamada_web` and `hamada_worker` have no bypass, DML grants only, and only INSERT and SELECT on audit tables. `hamada_maint` has BYPASSRLS for cross-clinic upkeep (partitions, key cleanup, platform counts from `clinic_day`) and no grants on patient tables. Per-clinic jobs run as `hamada_worker` with the clinic set in each transaction.
- Migrations: every new tenant table gets its policy in the migration that creates it; a test fails for any shard table with `clinic_id` lacking FORCE and the standard policy.
- Tests: the test role has no BYPASSRLS, so with FORCE the suite exercises the policies. Fixtures are created inside a `clinic_context`. Tests cover no setting, a reused connection, and another clinic's id in WHERE and in INSERT.
- Queries: under a policy, non-leakproof operators lose their index ranges (verified for LIKE). Prefix search uses range predicates, and M1 checks the plans of the hot queries as `hamada_web`.
- The directory has no RLS (no patient data); its web grants are narrow.

### D5. Other conflicts

- a. Deadlocks and lock timeouts (judge: retry once on both; backend: 409 `busy`). The server retries `40P01` once; `55P03` has already waited 2 s, so it is 409 `busy` with `Retry-After`, and the client resends the same key. Safe because of D2.
- b. Retrying after a 500 (draft 1: yes; frontend: no): no. A 500 is a bug; retries are for network errors, timeouts, 502/503/504 and 429.
- c. Storage plan (judge: 6–8 TB; database: about 10 TB): plan about 10 TB at 2 years and 4–5 TB more a year, because the database figure includes measured organic index growth. With shards capped by the restore drill, that is about 10 shards at 2 years.
- d. Cancelling a visit with money on it (backend: refuse, or cancel with refund; judge: refuse): refuse with 409 `refund_first`. A combined cancel-and-refund is deferred.
- e. When the invoice is created (draft 1: first payment or finish; backend: before payment): one `ensure_invoice` under the appointment lock, called by any billing action (a line, a discount, a payment, finish). Nothing at arrival, so no-shows don't take numbers.
- f. Follow-up rule (backend: blocking; judge: improvement): blocking for M1, because the fee is fixed at booking.
- g. Turn numbers: never given twice, gaps allowed (judge). Invoice, file and prescription numbers stay gap-free.
- h. Membership cache (judge: invalidate with a TTL of at most 30 s; backend: drop): drop it. Under D1 each shard serves only its clinics' polls, and the membership is one key lookup.
- i. SQL-shape check (judge: every tenant table in a statement): the root table of each statement must filter on `clinic_id`. Joins along composite foreign keys needn't repeat it: within a shard they can't cross clinics, and RLS covers the joined table.
- j. TOTP secret encryption (judge: M0; backend: before go-live): M0. Accounts are built in M0, and encrypting later is a data migration of secrets.
- k. Session length (frontend: end at the clinic's day end): keep 12 hours with a warning 10 minutes before and sign-in in place; aligning to the day end is deferred.
- l. Platform admin (judge: M1; draft 1: no milestone): M1. It exercises D1's cross-database flow early, and without it clinics are created in the shell again.
- m. `finish` from `waiting` (backend B9), `undo_call` (frontend 4) and `undo_arrive` add transitions R4.3 rules out ("Only these changes are allowed"). They become questions for Saged.

## 4. Required changes for draft 2

Milestone: where code first depends on the change. "Text": draft 2 states it now; code comes later.

**1. Application-level shards (D1).** Database (topology, ranges), backend (router, context, cross-database flows). Sections 1, 3.1–3.4, 4.0, 4.1, 9, 11, 12, 13, 14. **M0.**
- Everything in D1's "What M0 must already do"; 3.3 rewritten with measured triggers and the clinic-move procedure; Citus removed.

**2. Key and index rules.** Database. Sections 4.0–4.9, 12.2. **M0** (rules), M1–M3 (tables).
- `id` primary key with per-shard ranges; `UNIQUE (clinic_id, id)` only where referenced; the primary-key swap deleted.
- Every child table has an index for its read path, including `UNIQUE (clinic_id, prescription_id, position)` on `clinical_prescription_item` (database B5). Every keyset index ends with `id` (database I5).
- `summaries_doctor_day` unique on `(clinic_id, day, doctor_id)`: 1 index descent for Today against 31 (database I4).
- A test-time `execute_wrapper` fails any statement whose root tenant table isn't filtered on `clinic_id` (D5 i).

**3. Row-level security and database roles (D4).** Database (policies, roles), backend (one transaction per request, contexts, tests). Sections 4.0, 5.0, 8, 9, 10. **M0.**

**4. Safe repeats, server side (D2).** Backend; database for the table and cleanup. Sections 4.0, 5.0, 5.3, 6.1, 12.5. **M0** (`core`), used from M1.
- Every value change (discount, stock count, setting) is an absolute value with the expected old value. Merges backend B4, judge 1, database I1.

**5. Safe repeats, browser side.** Frontend. Sections 7.1, 7.4. **M0** (`api.js`), M1.
- The key belongs to the form: made when it opens, reused until a definite answer. After a timeout or network error the outcome is unknown: lock the fields, show "not confirmed yet, checking", resend the same key and body with backoff; offer only "try again" and "discard".
- `{url, key, body, user, membership}` kept in `sessionStorage`, settled before anything else after a reload or a new sign-in (same user and membership only), cleared at sign-out and when another person signs in. Never patient data in `localStorage`.
- "Saved" only for an answer that echoes the key; "still saving" after 3 s; stop after about 20 s. Keys fall back to `crypto.getRandomValues` where `randomUUID` is missing (plain http on a LAN). Merges frontend 1, judge 1.

**6. Front-end architecture (D3).** Frontend. Sections 1, 6.1, 7.1, 7.2, 12.6. **M0.**
- Server-rendered pages; one `.po` compiled with polib; `pgettext` instead of English-text keys; a test for missing or unused Arabic strings replaces `makemessages`.
- JSON gives times as ISO 8601 with the clinic's offset; display values are formatted on the server; the numbering system comes from the clinic setting; money uses our own formatter.
- Static: `support_js_module_import_aggregation = True` or no module imports, `modulepreload` for shared modules, `X-App-Version` on every answer so Today reloads at a quiet moment, the previous release's files kept a day. State the browser floor (Chrome/Edge 109, Safari 15.4) and the features banned without a fallback.

**7. Arabic, numbers and bidi.** Frontend (templates, client parser), backend (server parser). Sections 5.7, 6.1, 7.2. **M0.**
- Every data value in `<bdi>`; phones, file, invoice and prescription numbers, amounts and times `dir="ltr"` with isolation, inputs included; names and notes `dir="auto"`.
- One number parser on client and server for amounts, quantities, phones, ages and days: Arabic-Indic (U+0660–0669) and extended Arabic-Indic (U+06F0–06F9) digits, U+066B, `.` or `,` as the decimal point; money to piastres by string arithmetic; number fields `type="text"` with `inputmode`.
- A page test for the on-screen order of a phone's digit groups.

**8. Permissions and grants.** Backend. Sections 4.2, 5.8, 6.2. **M0** (engine), M3 (screens).
- A non-owner can give, remove or reach through a role change only codes they hold. Only the owner gives or removes `team.manage`, `billing.refund`, `audit.view` and `finances.view`. Nobody edits their own membership; only the owner edits a holder of `team.manage`. Stored codes are intersected with the known codes.
- Services check body-dependent codes: `patients.edit` for a new patient in a booking, `billing.collect` for a payment inside another action, `billing.discount` for any price not from the list.
- Add `clinical.view` for notes and prescriptions (doctor template; the owner may grant it to an assistant) and a code to write staff pay. Merges backend B1, B13b, I6.

**9. Accounts across clinics, and the platform admin.** Backend. Sections 4.1, 5.8, 6.2, 8, 11. **M0** (rules), **M1** (platform screens), M3 (invitations).
- Clinic endpoints change memberships only, never `accounts_user`. An existing account joins by an invitation its holder accepts; adding answers the same for known and unknown usernames.
- Platform admins hold no memberships (checked on both sides). Ownership moves only to an active doctor of that clinic; `CHECK (role <> 'owner' OR is_active)`.
- Support resets: a reason, a second admin, an event in the clinic's audit log, a notice at the next sign-in. Django's admin site is off in production.
- The platform admin (create, suspend, reactivate, list with usage counts) in M1. Merges backend B2, I6, judge 8.

**10. Sign-in, sessions and two-factor codes.** Backend (server), frontend (overlay). Sections 4.1, 5.9, 8. **M0.**
- TOTP: `totp_last_step` set by a conditional update (the skeleton has the column); five wrong codes end the pending sign-in and count toward the username limit; the pending state expires in 5 minutes and isn't signed in for `@api`; a new session key after the code; `mfa_verified` in the session, checked by `@api` against the active membership's requirement; 10 hashed recovery codes; secrets encrypted with an application key.
- `@api` enforces `must_change_password` and code set-up; temporary passwords expire in 72 hours and are changed before the code is set up.
- Throttles in PostgreSQL (directory), Redis only as a speed-up, failing closed; count failures; limits per (username, IP), a looser one per username, and a loose per-IP one (clinic routers, carrier NAT); a dummy hash check for unknown usernames.
- Sessions: `cached_db` only, a fixed 12 hours, never saved by a poll; `GET me` returns `session_expires_at`.
- Overlay: over the intact page, handles password, code and forced change, returns to the same clinic; CSRF read from the cookie at each send; a JSON CSRF-failure view with one refetch and resend; a 401 stops polling; sign-in and sign-out broadcast to other tabs. Merges backend B10, B11a-b, I10, I14; frontend 3, I13; judge improvements.

**11. Request context.** Backend, frontend. Sections 3.2, 5.9, 6.1. **M0.**
- Every call sends `X-Membership` for the page it was loaded for; a mismatch is 409 `clinic_changed` and the tab reloads. The clinic still comes from the session (CLAUDE.md).
- `Ctx` carries the actor kind (member, system, platform), the effective codes and `mfa_verified`; only the decorators and the job runner build it. Jobs use a system `Ctx`; `created_by_id` and `updated_by_id` are nullable and the audit actor is "system"; services that read patient data refuse system and platform contexts. Merges backend B3, B12d, I1; frontend 5.

**12. Health-data controls.** Backend. Sections 6.2, 8, 9. **M0** (decorator, logging, headers), M2 (clinical views), text now for M5.
- Views declare `access="patient"` and the selector writes the access event; a URL-configuration test fails for any view returning patient fields without it (sub-lists, notes, prescriptions, invoices, print pages).
- No query strings or bodies in proxy, Gunicorn or application logs; Sentry with `send_default_pii=False` and scrubbing.
- `Cache-Control: no-store` on every API, page and print answer; no patient name in `<title>`.
- Before real patients (M5): a per-membership limit on file opens, with an alert. Merges backend B13a, c, d, f; judge.

**13. Audit and access logs.** Database. Sections 4.10, 8, 11. **M0.**
- `audit_change` (low volume, keeps `details`, retained as long as financial records; the period is Open for the legal review) and `audit_access` (monthly partitions, `clinic_id NOT NULL`, `PRIMARY KEY (clinic_id, at, id)`, index `(clinic_id, patient_id, at)`, 24 months). Account and platform events go to `account_event` in the directory.
- Partitions are created standalone with a matching CHECK, then attached; old ones leave by `DETACH ... CONCURRENTLY`; upkeep runs as `hamada_maint`. The scale model uses 200–280M access rows a month. Merges database B6, judge 6.

**14. The business day.** Backend (server), frontend (Today). Sections 4.0, 5.2, 5.10, 5.11, 7.4. **M0** (setting and `business_day()` helper), M1.
- Clinic setting `day_ends_at` (default 04:00); every server-computed `day` uses it. The time zone and `day_ends_at` change only through a job that recomputes `day`, or are locked once bookings exist.
- `GET today` without `day` means the current business day; the answer carries `day`, `server_time` and the next rollover; the client never works out "today".
- The no-show sweep runs per clinic after the day ends plus a grace period. A written rule for leftover `waiting` and `in_room` visits (in_room: finish and create the invoice, flagged).
- Tests across the boundary and Egypt's daylight-saving changes. Merges backend B9, I11; judge 4; frontend 6.

**15. Jobs and connections.** Backend, database. Sections 3.1, 5.0, 5.10, 9. **M0.**
- Procrastinate in the directory (D1); its worker connects directly for LISTEN, or polls.
- Per-role timeouts replace session settings and startup options; any exception uses `SET LOCAL`.
- One job per batch of about 500 clinics, one transaction per clinic; the recompute covers every past day changed since its last run. Merges backend B12b-c, I12; database I8 (LISTEN), I11.

**16. Lock order and the lock table.** Backend. Sections 5.0–5.4, 5.10. **M1.**
- Global order: request key → appointment → invoice → payment → inventory items → counter → `doctor_day` → `clinic_day`. Several rows of one table are locked in one statement in key order (`ORDER BY id FOR NO KEY UPDATE`; doctor-days by (day, doctor_id), clinic-days by day).
- A per-flow lock table as in the warehouse's 5.1: booking, every status action, reschedule, payment, refund, discount, void, finish with payment, stock use with several items, purchase, adjustment, the sweep and the recompute.
- Reschedule: the current target is a 200 that changes nothing; a move within the same doctor-day keeps its number; `last_queue_no` never goes down.
- The recompute locks the day's summary rows `FOR UPDATE` first, then aggregates and writes, one clinic-day per transaction, after the business day ends. Fix the ambiguous upsert in 5.1. Merges database B4.1, B4.5; backend B5, B12a; judge 3.

**17. Invoices, payments and money counters.** Backend (rules), database (columns). Sections 4.4, 4.5, 5.2, 5.3. **M1.**
- Payment, finish, cancel and reschedule lock the appointment first (`FOR NO KEY UPDATE`); `ensure_invoice` reads the invoice under that lock and takes a number only when it inserts one.
- The one-invoice-per-visit index is `WHERE status = 'open'`; every `ON CONFLICT` on a partial index repeats its predicate. Cancel answers 409 `refund_first` while `paid > 0` and voids an empty invoice.
- Payment and refund rows carry `patient_id`, `doctor_id` (copied at insert) and `day`; index `(clinic_id, patient_id, received_at DESC, id DESC)` for the patient's payments page. `doctor_day.refunded`; a refund counts on its own day; net = collected − refunded.
- A table in 5.2/5.3 of the counters each action changes (book, arrive, undo, finish, cancel, no-show, reschedule, pay, refund, discount, void, expense and its void); the recompute and the invariant tests read it. Discounts are absolute values with the expected old value; an endpoint removes a line. Merges database B4.2–B4.4, I7; backend B6, B7, I5; judge 2.

**18. The appointment table's shape.** Database. Sections 4.4, 5.1, 5.11. **M1**, in the table's first migration (a stored column added later rewrites the table).
- `holds_slot boolean GENERATED ALWAYS AS (...) STORED`, slot index `WHERE holds_slot`, `fillfactor = 85` (verified: 100% HOT against 0%).
- Never index `updated_at`; recent changes come from the day's rows Today already reads.
- `max_bookings` enforced in the turn upsert (`WHERE booked < :max`, else 409 `day_full`); a schedule change that would misalign future slot bookings is refused. Merges database B3, backend I3.

**19. Patient search.** Database (index, settings), backend (queries), frontend (box). Sections 4.3, 5.7. **M1.**
- Index `INCLUDE (id, full_name, phone, file_no)`; `name_norm` and `phone_norm` declared `COLLATE "C"`.
- Prefix search as `name_norm >= :q AND name_norm < :q_next`, not LIKE (verified under RLS); word-prefix LIKE with `%`, `_` and backslash escaped, sent as a literal; a 300 ms `statement_timeout` on search. Per-table autovacuum settings on patients, appointments and invoices (database B2's values).
- A 250 ms debounce, a 2-character minimum, the previous request aborted. A duplicate-phone suggestion endpoint and step in Book (R4.2).
- An M1 plan check for a 100,000-file clinic as `hamada_web`. Merges database B2, judge 5, frontend I3 (part).

**20. The follow-up price rule.** Backend. Section 5.1; the R4.1 assumption. **M1.**
- Measured from the new appointment's day; counts the patient's consultations with the same doctor that arrived or were paid; each consultation service names its follow-up service (`follow_up_service_id`); a follow-up doesn't start a new window; checked again at arrival. Recorded as an assumption for Saged.

**21. Front-desk actions in one request.** Backend (API), frontend (screens). Sections 5.1–5.3, 6.2, 7.3. **M1.**
- `POST appointments` accepts `arrive: true` and an optional payment, in one transaction under one key; `finish` accepts an optional payment (R4.3); booked rows offer "arrive and collect".
- Status actions send the status the screen saw and return the updated row and the new version. Defaults: the business day, the open doctor tab, a consultation, "at the clinic", collect now in cash. Merges frontend 10, 11; judge 2; backend I2.

**22. Reads the screens need.** Backend. Section 6.2. **M1** (Book, Today, quote), M2–M3 (the rest).
- The clinic's configuration readable by any signed-in member: active services and prices, doctors with their mode and today's hours, medicines, payment methods, display settings.
- `GET appointments/quote` (fee and `is_follow_up`); Today's rows add `due`, `invoice_id`, phone, file number, `is_new`, `is_follow_up`; `GET appointments/{id}`; unpaid invoices; `PATCH inventory/items/{id}`; expense categories; `POST platform/clinics/{id}/reactivate`. Merges frontend 11, backend I6.

**23. Today and the live queue.** Backend (server), frontend (client). Sections 2, 3.1, 5.11, 7.3, 7.4, 10. **M1.**
- Server: the real 304 cost per stage (session, user, `set_config`, membership, version); no membership cache; a list of every change that bumps the version (patient name edits, stock use near the threshold, schedule changes, payments for any day shown); the ETag is clinic, day, version, a hash of the person's codes and the language; the version is read before the data; a `Poll-After` header on every answer, 304s included; money totals on Today only with `finances.view`.
- Client: one poller per browser and membership (a Web Lock, answers shared over `BroadcastChannel`; each tab polls alone where locks are missing); ±20% jitter, full-jitter backoff, `Retry-After` honoured; "updated HH:MM" in server time and a dimmed queue when stale; refresh on `visibilitychange`, `online`, `pageshow`, `focus` and late timers; "try now".
- Rows ordered by turn or slot time only, keyed by id and patched in place, never replacing the row under the pointer or with focus; pending actions kept in a store; confirmations name the patient.
- The Redis version path waits for the M5 load test; if built, it sets only a higher value, with a 30–60 s TTL. Merges judge 7; frontend 4, 5; backend B11c-d, I7, I8, I9; database I10.

**24. Printing.** Frontend. Section 7.5. **M1** (receipts), M2 (prescriptions).
- `@page { size: A5; margin: 0 }` with padding inside; 80 mm: content 72 mm wide, black, at least 10 pt, the page height set from the measured content with `insertRule`.
- `?lang=ar|en` per print with a clinic default; a letterhead setting (no header, offsets in mm). The print tab opens in the tap handler. M1 tests two common thermal printers. Merges frontend 9, judge R4.5.

**25. Tests.** Backend (server), frontend (client), database (plans). Section 10. **M0–M1.**
- Backend T1–T11, plus: RLS (no setting, a reused connection, another clinic in WHERE and INSERT); routing with clinics on two shards; sequence ranges; every shard table forced and policied; the key table (same key twice, two threads with one key, another body gives 422, a retry after a 409); crossing reschedules; first payments without a gap; payment against cancel; the recompute against live writes.
- A failing test first for stock, money, permission and status bugs (CLAUDE.md).
- Client: Django test-client tests for every page and fragment; headless browser scripts for a double tap, a lost answer, a 401 mid-save, the RTL order of a phone, and the A5 and 80 mm page sizes. Load-test pass criteria at stage-1 size (p95 and error rate for 304s and the write mix).

**26. Visit notes and drafts.** Backend, frontend. Sections 5.5, 5.6, 7.3. **M2.**
- Notes carry a version; a `PUT` sends `If-Match` and a conflict gets 409 with the current text to merge; an edit after the visit day keeps the old text as a revision.
- One save in flight per note; drafts of notes and prescriptions kept in `sessionStorage` per appointment until saved. Merges backend B13e, frontend 2.

**27. Availability and runbooks.** Database. A new section in 9. Text in draft 2; built before go-live (M5).
- High availability per database (the directory and each shard): managed PostgreSQL with HA, or Patroni with a tested promotion; how PgBouncer and Redis behave on failover; the RTO per stage; `archive_timeout` of at most 300 s for the 5-minute RPO.
- Runbooks: restore, failover, migrations across shards, moving a clinic, partitions, incidents, key rotation. From judge 8.

**28. The scale model.** Database. Sections 2, 3.3. Text in draft 2.
- Plan about 10 TB at 2 years and 4–5 TB more a year. Add login events, job rows, request keys and 200–280M access rows a month; the biggest clinic (30 doctors, 100,000+ files, a hot `clinic_day`); the peak hour at 1.5 times the evening average (160–210 visits a second); the RTO per stage, with shard size set by the restore drill. From the database scale section and judge 2g.

**29. The requirements map and milestones.** Backend, with frontend and database for their rows. Section 11 and a new Appendix A. Text in draft 2.
- A full table from R-section to design section and milestone, as in the judge's section 3, with every gap closed or marked Open.
- The platform admin in M1; refunds, discounts and voids named in M1; specialty templates include a stock list (R1, M0); a consultation state table (R4.8, M3); what changing the specialty does (R4.9, M3); a purchase's expense is reversed with the purchase, never voided alone (backend I4, M3).
- New questions for Saged: the follow-up rule, `undo_arrive` and `undo_call`, finish from `waiting`, no-show after the session ends, leftover `waiting` visits.
- Appendix A lists every change in this section with the sections that apply it.

## 5. Deferred improvements (none causes a rewrite later)

- The foreign key to `clinics_clinic` only on root tables, none on actor columns (database I2): dropping a foreign key later is instant; M1 migrations may simply omit them.
- Fillfactor 70–80 on summaries and counters; dropping the low-stock and unpaid partial indexes if no screen uses them (database I3): fillfactor can be set any time; the indexes are decided when M3 builds inventory.
- A token table for word-prefix search in clinics over about 200,000 files (database B2, judge 5): an extra table a job fills.
- Normaliser additions (joining "abd al", hamza forms, the +20 and 0020 prefixes), a normaliser version, and phone search by the last digits (database I12, frontend I2): a batch job can re-normalise at any time; the phone search is a new column and index.
- Per-doctor queue versions (frontend I1): an added column; revisit if large clinics refetch too often (design 13).
- Combined cancel-and-refund; "finish and call next"; one endpoint for the visit screen (frontend I5).
- Sessions that end at the clinic's day end (frontend 3) and an idle lock on shared desk PCs (backend I13): before go-live (M5).
- Screen details built with each screen: Book on phones, server-clock times, feedback, accessibility, Arabic type sizes, RTL details, "load more" (frontend I4, I6–I11, I14). None changes the API or the schema.
- One permission declaration per service (backend I1): the URL-configuration tests cover it meanwhile.
- Server settings (`wal_compression`, `checkpoint_timeout`: database I11) and the M5 data-building method (database I13): at deploy and in M5. Reminder job cleanup (database I14): with reminders in M4. A vendored print font (frontend I9): only if line breaks differ across devices.

## 6. Review points rejected

1. Database B1, `PRIMARY KEY (clinic_id, id)` from the first migration via `CompositePrimaryKey` and `ForeignObject` or `SeparateDatabaseAndState`. Under D1 the key is never swapped. `ForeignObject` is semi-internal API the ORM and its tooling resist; the state-only fallback leaves Django sending id-only lookups, which the reviewer measured as 22–26 ms skip scans. The saving (0.4–0.5 TB of about 10 TB) shrinks further with change 2. Kept from B1: the swap doesn't work (deleted), and the clinic-filter check.
2. Judge 6, `ForeignObject` or a lint on every relation, CI on Citus from M1, and where jobs live under Citus: Citus leaves the design (D1). Kept: the SQL-shape check (narrowed, D5 i) and `clinic_id` in the audit key (change 13).
3. Database I8, the Citus details (reference tables, partitioned login events, shard count, PostgreSQL 18 support, `create_distributed_table_concurrently`, `update_last_login`): moot under D1. Kept: LISTEN bypasses PgBouncer (change 15).
4. The day-partitioned key table (backend B4, judge 1, database I1): wrong as specified, since a key can be claimed again after midnight (verified). The key table itself is accepted (D2).
5. Judge 8, a BYPASSRLS role for per-clinic jobs: those jobs can set the clinic as requests do; bypass is only for cross-clinic upkeep that reads no patient tables (D4).
6. Judge 3, retry once on `55P03`: the request has already waited 2 s; the client retries instead (D5 a).
7. Database I9, RLS in stage 2: replaced by D4.
8. Database B2, a literal pattern as the way to keep the prefix range: true without RLS, false with it (verified). Replaced by range predicates (change 19).
9. The judge's single RLS test, "a query without a clinic filter returns nothing": on a pooled connection the naive policy raises an error instead (verified). `NULLIF` and a reused-connection test are added (changes 3, 25).
10. Database I6, an exclusion constraint against overlapping slots: a GiST constraint on the largest table for a case the schedule-change rule prevents (change 18). It can't be added later without a blocking build, so this is a decision, not a postponement.
11. Backend B9's finish from `waiting` and frontend 4's `undo-call`: both contradict R4.3 ("Only these changes are allowed"); questions for Saged (change 29), not design changes.
12. Frontend 7's old Chrome and Edge time-zone data, and most of frontend 12 (the module waterfall): moot under D3, which formats on the server and loads fresh assets per page. What still applies is in change 6.
13. The judge's "6–8 TB is plausible": replaced by the database engineer's measured figure of about 10 TB (D5 c).
14. Duplicates, merged rather than rejected; each appears once in section 4 with its sources: the invoice-number gap (database B4.2, backend B6c, judge 2); the reschedule deadlock (database B4.1, backend B5, judge 3); recompute drift (database B4.5, backend B12a, judge 3); refunds per doctor (database B4.4, backend B7, judge); the search index (database B2, judge 5); the business day (backend B9, judge 4, frontend 6); the membership cache and Redis version (backend B11c-d, judge 7); per-IP sign-in limits (backend I14, frontend I13); access-log coverage (backend B13a, judge); TOTP encryption (backend B10h, judge); the prescription item index (database B5, judge).

## 7. Rationale

Draft 1's architecture is right for a small team: a Django monolith on PostgreSQL with clinic-scoped keys, conditional updates and polling needs no rethinking. What failed under test were its safety claims (safe repeats, gap-free numbers, no deadlocks, the 20 ms search, "stage 3 is a migration") and two R5 requirements with no design (availability, isolation by the database).

The decisions fix these with the fewest moving parts:
- Stock PostgreSQL and plain Django relations instead of Citus (D1). Each shard is restored, failed over and tested like stage 1, including on the Windows machine.
- One bounded key table with a request hash instead of an unbounded index on every business table (D2).
- Server-rendered pages for one catalogue, server-side formatting and Django tests (D3).
- RLS now, while it is cheapest, with its two traps (leakproof operators, pooled settings) found and fixed here rather than in production (D4).

Every M0 and M1 change is either a migration line that can't be added cheaply to billions of rows later, or a rule in `core` that every later app inherits. Everything deferred can be added without rewriting code or rebuilding large tables. Draft 2 with these 29 changes applied goes to a check against this list, as in the warehouse's Appendix B, while M0 proceeds.
