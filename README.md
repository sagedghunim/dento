# Hamada App

Multi-clinic management: Django backend, JSON API, plain JavaScript front end.

## Run on Windows

Double-click `RUN_HAMADA.bat`. It installs, creates the database, prints the demo logins (`hamada`, `sara`, `ahmed`, with a generated password), runs the tests and opens the app.

## Why it holds up as it grows

- **One app, many clinics.** Every table has a clinic owner, and the clinic always comes from the signed-in user.
- **Permissions on the server.** Every API checks them; hidden buttons are only a convenience.
- **Rules in the database.** Stock cannot go negative, statuses and categories are checked, money is integer piastres.
- **Safe under pressure.** Stock and status changes are conditional updates, so two people cannot take the last unit or finish a visit twice. Each change is one transaction. Repeated POSTs with the same `Idempotency-Key` act once.
- **Fast with old data.** Indexed by clinic and date, lists are capped, totals are computed in the database.
- **Stateless web tier.** Sessions and login limits live in PostgreSQL and Redis, so more web containers can be added.

## Production

Copy `.env.example` to `.env`, fill it in, then run `docker compose up --build`. Put HTTPS in front, and set `CLIENT_IP_HEADER` to the header your proxy sets.

## Before real patient data

See `CLAUDE.md`, "Known gaps".
