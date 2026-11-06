# Hamada App

Django multi-clinic backend with a plain JavaScript front end. Replaces the demo in `C:\work\hamada-app`.

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
