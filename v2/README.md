# Hamada v2

The rebuild described in `docs/v2/design.md` (an index of `docs/v2/design/`) (draft 2.2). Django 6.1 on PostgreSQL 18: a directory database and clinic shards, row-level security on every clinic table, server-rendered pages in Arabic (default, right to left) and English.

## Run it

Double-click `RUN_V2.bat` in the repo root, then open http://127.0.0.1:8002.

### First-time setup on a new machine

1. PostgreSQL 18: unzip the official Windows binaries so that `C:\work\pgsql\bin\postgres.exe` exists. Then run `powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 init`. It creates the `hamada` role, the databases `hamada_dir`, `hamada_s001` and `hamada_s002`, and writes `v2\.env`. The server is shared with the warehouse project, on port 5433.
2. Python packages: `py -3.15 -m venv v2\.venv`, then `v2\.venv\Scripts\python -m pip install -r v2\requirements.txt`.
3. `cd v2`, then `.venv\Scripts\python manage.py migrate_all`, then `.venv\Scripts\python manage.py seed_demo`.

## Demo accounts (from `seed_demo`)

`seed_demo` prints one password for the accounts it creates. It only adds what is missing: run it again on a database seeded before the center existed and it adds the center and its four accounts, leaving the other places and accounts (and their passwords) as they are. It never deletes anything.

| Who | Username | Role |
|---|---|---|
| د. حمادة عزيز | `hamada` | Owner of the dental clinic (shard 1) |
| د. منى فتحي | `mona` | Owner of the children's clinic (shard 2) |
| د. أحمد سامي | `ahmed` | Doctor in the dental clinic |
| سارة محمود | `sara` | Assistant in both clinics, so she sees the clinic picker |
| د. كريم يوسف | `karim` | Owner of مركز الشفاء الطبي, a center with dental, children's and dermatology sections (shard 2); works in dental |
| د. عمر خالد | `omar` | Doctor in the center's children's section (15-minute slots) |
| د. نور حسن | `nour` | Doctor in the center: dermatology on Saturday, Monday and Wednesday, dental on the other days |
| ليلى إبراهيم | `laila` | The center's front desk |

Owners must set up two-step sign-in on first sign-in: scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator) and keep the 10 recovery codes.

## The background worker

The end-of-day sweep and the summary recompute (design 5.10) run as Procrastinate periodic tasks, one job per shard every 10 minutes: `scheduling.sweep_shard` at :00, :10, ... and `scheduling.recompute_shard` five minutes later. Each visits only the places whose business day ended at least 30 minutes ago, in their own time zone, and that the job hasn't done yet; the directory keeps a copy of each place's time zone and day end for that, and the sweep repairs the copy if it ever differs from the shard. Jobs live in the directory database (`hamada_dir`).

Two daily upkeep tasks run on the same worker: `core.ensure_partitions` keeps the monthly log partitions 6 months ahead on every database (without them, sign-in and file opens fail on the first day of an unprepared month), and `core.clean_request_keys` deletes request keys older than 72 hours. `manage.py ensure_partitions` does the first by hand, and `migrate_all` runs it on every deploy.

Run the worker in its own window, from `v2`:

```
.venv\Scripts\python manage.py procrastinate worker
```

- It defers the periodic jobs itself, so nothing else is needed. Stop it with Ctrl+C. On Windows it stops at once (Procrastinate has no graceful stop there): a job cut short rolls back and the next run does its work, since every step is safe to repeat.
- One worker is enough. A second one is safe: at most one job per shard waits at a time, and two runs on one place change each visit once.
- In production it connects to PostgreSQL directly, not through PgBouncer's transaction pooling (design 5.10, 9.4).
- `.venv\Scripts\python manage.py procrastinate healthchecks` checks its database connection and schema.
- Without a worker, run the jobs by hand: `manage.py sweep_days` and `manage.py recompute_summaries` visit the places that are due. `--clinic <id>` visits one place whatever its marks (a catch-up); `sweep_days --now 2026-11-08T05:00:00+02:00` pretends it is that time.

## M0, the base

- A directory database (accounts, sessions, throttles, the clinic directory) and two shards (clinics and everything in them). A router sends each request to its clinic's shard and refuses a clinic query without a clinic.
- Forced row-level security on every shard table, partitions included. Ids come from per-shard ranges.
- One transaction per request on the shard; request keys with a stored hash, so a repeat replays and a reused key with another body is refused; `X-Membership` against another tab's clinic.
- Sign-in with throttles in PostgreSQL, two-step codes (encrypted secrets, each code once, 10 recovery codes), forced password change, sign-out of other sessions on a password change.
- Permissions per person with grant rules that stop escalation; clinic settings with absolute values; the change log and the monthly-partitioned access log.
- Pages: sign-in, the clinic picker, password, two-step set-up, and the shell of Today.

Not built yet: everything from M1 on (patients, booking, the queue, payments, receipts, the platform admin).

## Tests

```
cd v2
.venv\Scripts\python manage.py test
```

They run against PostgreSQL with the directory and both shards, because row-level security, the composite foreign keys and the id ranges are part of what they test.

## Translations

Arabic strings are in `locale/ar/LC_MESSAGES/django.po`. After editing it, run `manage.py compile_translations` (GNU gettext isn't needed). A test fails when a string lacks its Arabic translation.
