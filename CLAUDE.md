# Hamada App

Django multi-clinic backend with a plain JavaScript front end. Replaces the demo in `C:\work\hamada-app`.

## v2 rebuild (started 2026-11-06)

The platform is being rebuilt from scratch to grow to 250,000 doctors. Requirements: `docs/v2/requirements.md`, design: `docs/v2/design.md`, reviews: `docs/v2/reviews/`. For v2 work, those documents override the rules below. The rest of this file describes the prototype in `clinic/`, which is now a domain reference only.

- Never read `docs/v2/design.md` whole (about 950 lines). Search it, or read only the sections you need.
- v2 code is in `v2/`: Django 6.1 on PostgreSQL 18 (the portable copy in `C:\work\pgsql`, port 5433, shared with the warehouse project; database and role `hamada`). It is served on port 8002.
- First-time setup: `powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 init` creates the database and writes `v2\.env`. Start and stop PostgreSQL only through `v2\scripts\pg.ps1`; stopping it stops it for the warehouse too.
- Python is 3.15 (`py -3.15`); `python` on PATH is the Microsoft Store stub. Use `v2\.venv\Scripts\python.exe`.
- Tests run against PostgreSQL: `cd v2` then `.venv\Scripts\python manage.py test`.
- Screens follow the first demo (`C:\work\hamada-app`, https://hamada-app-ghunim.vercel.app/): the same sections, layout and look, in Arabic right to left first (requirements 1).
- Agents in `.claude/agents/`: senior-backend, senior-frontend, senior-database, judge, chief-judge, code-scout. A design or a milestone goes to the three seniors and the judge for review, then the chief judge decides.

## Commands (Windows)

```
.venv\Scripts\activate
set DJANGO_DEBUG=1
python manage.py migrate
python manage.py seed_demo
python manage.py test
python manage.py runserver
```

## Layout

- `clinic/models.py`: data model and database constraints
- `clinic/services.py`: all business rules
- `clinic/views.py`: thin API; `clinic/http.py`: auth, permissions, idempotency, validation
- `static/app.js`: front end; `clinic/templates/app.html`: page shell

## Rules

- Every query is scoped through `request.membership.clinic`. Never take a clinic id from the browser.
- Every API declares its permission in `@api(...)`. Hiding a button is not protection.
- Money is integer piastres. Times are stored in UTC and shown in the clinic's time zone.
- Change stock and statuses with conditional `update()` calls, inside services.
- Sensitive changes go through `note(..., sensitive=True)`.
- Do not edit an applied migration. Do not run `seed_demo` on production data.
- Write a failing test first for stock, money, permission or status bugs.

## Known gaps

1. Tested on SQLite only. Run the tests on PostgreSQL before go-live, including `ConcurrencyTests`.
2. `Dockerfile`, `docker-compose.yml` and the production requirements have not been run. Their pinned versions need checking.
3. No password reset, account creation screen, or clinic sign-up yet. Users are created in the shell.
4. No real video calling. Consultation actions only record status.
5. Lists show the latest 100 rows with no "load more" yet.
6. Not load-tested. No error tracking, uptime checks or tested backups yet.
7. Patients are matched by exact name, so two people with one name share a record. Add phone numbers before real use.
8. Read access to patient data is not logged; only sensitive changes are.
9. This holds health data. Get a qualified review of Egypt's data protection rules before real patients.
