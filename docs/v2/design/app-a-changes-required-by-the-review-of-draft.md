## Appendix A: changes required by the review of draft 1

| # | Change | Applied in |
|---|---|---|
| 1 | Application-level shards, directory, router, ownership check, id ranges, `migrate_all`, Procrastinate in the directory | 1, 3.1–3.5, 4.0, 4.1, 5.10, 5.12, 9, 11, 12.2, 13 |
| 2 | Key and index rules: id ranges, `UNIQUE (clinic_id, id)` only where referenced, child and keyset indexes, `doctor_day` key order, the SQL-shape check | 4.0–4.9, 10 (T13) |
| 3 | Row-level security, roles, per-role timeouts, tests | 4.0, 5.0, 8, 9.4, 10 (T11) |
| 4 | Request-key table with a request hash, claimed first, 72 hours, absolute values | 4.11, 5.0, 5.3, 6.1, 12.5 |
| 5 | The browser half of safe repeats | 7.4 |
| 6 | Server-rendered pages, one catalogue with `pgettext` and a test, static file rules, browser floor | 1, 6.1, 7.1, 7.2, 12.6 |
| 7 | Bidi, the shared number parser, the phone-order test | 6.1, 7.2, 10 (T15) |
| 8 | Grant rules, body-dependent codes, `clinical.view`, `finances.pay` | 4.2, 5.1, 5.3, 5.8 |
| 9 | Accounts across clinics, invitations, platform admins without memberships, support actions, the platform admin in M1 | 4.1, 5.8, 5.12, 6.2, 8, 11 |
| 10 | Two-factor codes, throttles in PostgreSQL, sessions, the overlay | 4.1, 5.9, 7.4, 8 |
| 11 | `X-Membership`, `Ctx` with the actor kind, system contexts for jobs | 5.0, 5.10, 6.1 |
| 12 | Access declared per view, no patient data in logs, `no-store`, the read limit before go-live | 6.1, 8, 10 (T9) |
| 13 | `audit_change` and `audit_access`, account events, attach-based partition upkeep | 4.1, 4.10 |
| 14 | The business day | 4.0, 4.2, 5.1, 5.2, 5.10, 5.11, 7.4, 10 (T8); R4.1 |
| 15 | Jobs and connections | 3.3, 5.10, 9.4 |
| 16 | Lock order and the per-flow lock table, reschedule rules, the safe recompute, the fixed upsert | 5.0, 5.1, 5.2, 5.10 |
| 17 | Invoices under the appointment lock, `ensure_invoice`, the open-invoice index, `refund_first`, payment columns, `doctor_day.refunded`, the counters table, absolute discounts, removing a line | 4.4, 4.5, 5.2, 5.3 |
| 18 | `holds_slot`, fillfactor, no index on `updated_at`, `max_bookings`, the schedule-change rule | 4.4, 5.1 |
| 19 | Patient search: covering index, collation, range predicates, escaping, timeout, autovacuum, debounce, duplicate phone, plan check | 4.3, 5.7, 10 |
| 20 | The follow-up rule | 5.1; R4.1, R7.8 |
| 21 | Front-desk actions in one request | 5.1, 5.2, 6.2, 7.3 |
| 22 | Reads the screens need | 5.1, 6.2 |
| 23 | Today and the live queue | 2, 5.11, 7.4, 10 |
| 24 | Printing | 7.5 |
| 25 | Tests | 10 |
| 26 | Visit notes and drafts | 4.6, 5.6, 7.4 |
| 27 | Availability and runbooks | 9.2, 9.5 |
| 28 | The scale model | 2, 3.4 |
| 29 | The requirements map, milestones and questions for Saged | 11, Appendix B; R7.8 |
