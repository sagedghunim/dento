## 3. Architecture

### 3.1 Components

| Component | Technology | Role |
|---|---|---|
| web | Django 6.1 on Gunicorn | Pages, the JSON action API, print pages |
| worker | Procrastinate, stored in the directory database | The nightly sweep and recompute, request-key cleanup, partition upkeep, directory reconciliation; reminders and exports later |
| directory db | PostgreSQL 18 | Accounts, sessions, throttles, account events, the clinic directory, platform-admin data, jobs |
| shard dbs | PostgreSQL 18, one or more | Clinics and every tenant table |
| pgbouncer | PgBouncer, transaction pooling | Keeps database connections few as web containers grow |
| cache | Redis, optional | Only as a speed-up, from stage 2, after the M5 load test (5.11). Nothing in it is needed for correctness |
| proxy | Caddy | TLS, static files, compression |

### 3.2 Django apps

| App | Database | Owns |
|---|---|---|
| `core` | both | `@api` and the page decorator, `Ctx`, routing, request keys, the composite foreign key and RLS migration helpers, partitions, the business day, normalisation and the number parser, logging |
| `accounts` | directory | Users, sign-in, two-factor codes, recovery codes, throttles, sessions, account events |
| `directory` | directory | The clinic directory (`clinic_id → shard`), the `user_clinic` copy for the picker, the platform admin |
| `clinics` | shard | Clinics, memberships, permissions, services, schedules, counters, invitations; specialty templates live in code |
| `patients` | shard | Patient files and search |
| `scheduling` | shard | Appointments, turns, slots, status changes, reschedule |
| `billing` | shard | Invoices, lines, payments, refunds, receipts |
| `clinical` | shard | Visit notes and their revisions, the medicine list, prescriptions |
| `inventory` | shard | Items and the stock ledger |
| `finance` | shard | Expenses, categories, staff pay, the month view |
| `consults` | shard | Online consultation requests |
| `summaries` | shard | `doctor_day` and `clinic_day`. Depends only on `clinics` |
| `audit` | shard | `audit_change` and `audit_access` |
| `web` | — | Page templates, static files, print templates |

Rules:
- Only `services.py` modules change data. A service runs inside the request's single shard transaction. Views validate input and call one service.
- Each app owns its tables. Other apps read them through selectors.
- Every service takes a `Ctx` (5.0) and checks its permission codes itself, including codes that depend on the request body (5.8).

### 3.3 Databases, routing and shards

**Aliases.** `default` is the directory. `shard_<n>` are the shards (`shard_1`, `shard_2`, …). In development and tests: `hamada_dir`, `hamada_s001` and `hamada_s002` on the local PostgreSQL 18.

**The router** (`core/routing.py`):
- Directory apps (`accounts`, `directory`, `procrastinate`, Django's `contenttypes`, `auth` and `sessions`) read, write and migrate only on `default`.
- Tenant apps migrate on every shard and never on `default`. Their reads and writes go to the shard named in the current context. `@api`, the page decorator and the job runner set that context. A tenant query with no context raises; there is never a default shard.

**Ownership.** Each shard's `clinics_clinic` row has `owned`. The request transaction checks it together with the membership. A request that a stale directory cache sends to the wrong shard gets 503 with `Retry-After` and is routed again, so a clinic's writes can never be split across two shards during a move.

**No cross-database foreign keys, joins or transactions.** User ids on shard rows are plain integers. Flows that touch both databases are ordered, idempotent steps (5.12). The shard is the authority for memberships. The directory's `user_clinic` is a copy for the clinic picker: a nightly job reconciles it, and the picker re-checks on the shard.

**Ids.** Every identity sequence on shard *s* draws from [s × 2^48, (s + 1) × 2^48), set with MINVALUE and MAXVALUE so that exhaustion fails rather than collides. A post-migrate step sets the ranges, and a test fails if any sequence is out of range. Clinic ids come from the directory, and the shard's `clinics_clinic` row uses the same id. Shard numbers stay below 32, so every id stays under 2^53 and survives JavaScript's number type in JSON; a 33rd shard needs ids sent as strings first.

**Migrations.** `manage.py migrate_all` migrates the directory and every shard, and reports each one's state. Every tenant table gets its RLS policy in the migration that creates it (4.0).

**Jobs.** Procrastinate's tables live in the directory. Periodic jobs fan out by shard and by batches of about 500 clinics, with one transaction per clinic on its shard. M0–M3 queue no job from inside a request. When M4 needs that (reminders), it adds a per-shard outbox or a per-shard Procrastinate app; neither changes an M0 table.

### 3.4 Stages of growth

| Stage | Database | Web | Trigger to move on |
|---|---|---|---|
| 1 | One PostgreSQL cluster with two databases (`hamada_dir`, `hamada_s001`), a standby for failover, PgBouncer | 2–4 containers | Measured: the shard's restore drill approaches 2 hours, CPU above 60% at the peak, or the WAL rate near the standby's or archive's limit |
| 2 | A bigger server, or a second shard, whichever the trigger calls for. The directory can move to its own small cluster | 10–20 containers | The same measurements per shard |
| 3 | More shards (about 10 at 2 years at the target), each with a standby | 40+ containers | The same |

- New clinics go to the least-full shard.
- **Moving a clinic** (rehearsed in M5), in the 04:00–09:00 Cairo window:
  1. freeze the clinic: the directory marks it moving, **and the source shard clears `owned`**, so a request routed by a stale cache is refused there too (503 with `Retry-After`);
  2. wait until no transaction that started before that change is still running (`pg_stat_activity.xact_start`);
  3. copy its rows by `clinic_id`, table by table, to the target shard, and verify row counts and sums;
  4. set `owned` on the target, then switch the directory;
  5. delete the rows on the source in batches.
- A request checks the clinic (`owned`, status) **before** it claims its request key, and a missing clinic row counts as not owned (5.0).

### 3.5 Deployment

- One `docker compose` file with the components in 3.1. Configuration comes from environment variables.
- HTTPS everywhere; the proxy terminates TLS.
- **Open:** where to host it, which depends on the data protection review (R5).
- Local development: the portable PostgreSQL 18 in `C:\work\pgsql` on port 5433, shared with the warehouse project, with its own role and databases. The app runs on port 8002.
