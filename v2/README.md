# Hamada v2

The rebuild described in `docs/v2/design.md` (draft 2.1). Django 6.1 on PostgreSQL 18: a directory database and clinic shards, row-level security on every clinic table, server-rendered pages in Arabic (default, right to left) and English.

## Run it

Double-click `RUN_V2.bat` in the repo root, then open http://127.0.0.1:8002.

### First-time setup on a new machine

1. PostgreSQL 18: unzip the official Windows binaries so that `C:\work\pgsql\bin\postgres.exe` exists. Then run `powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 init`. It creates the `hamada` role, the databases `hamada_dir`, `hamada_s001` and `hamada_s002`, and writes `v2\.env`. The server is shared with the warehouse project, on port 5433.
2. Python packages: `py -3.15 -m venv v2\.venv`, then `v2\.venv\Scripts\python -m pip install -r v2\requirements.txt`.
3. `cd v2`, then `.venv\Scripts\python manage.py migrate_all`, then `.venv\Scripts\python manage.py seed_demo`.

## Demo accounts (from `seed_demo`)

`seed_demo` prints one password for all of them.

| Who | Username | Role |
|---|---|---|
| د. حمادة عزيز | `hamada` | Owner of the dental clinic (shard 1) |
| د. منى فتحي | `mona` | Owner of the children's clinic (shard 2) |
| د. أحمد سامي | `ahmed` | Doctor in the dental clinic |
| سارة محمود | `sara` | Assistant in both clinics, so she sees the clinic picker |

Owners must set up two-step sign-in on first sign-in: scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator) and keep the 10 recovery codes.

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
