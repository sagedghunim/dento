## Appendix C: changes from the check of draft 2

| # | Change | Applied in |
|---|---|---|
| B1 | Booking with payment takes the invoice number before the summary rows; each summary table written in one statement; payment inside a booking only for the current business day; T5 cases | 5.0, 5.1, 10 |
| B2 | 409 `busy` and 503 are never final for a form's key; no resend after 24 hours | 7.4 |
| B3 | The `db` session backend until a shared Redis exists; a two-process sign-out test | 5.9, 10 |
| B4 | Partition DDL as `hamada_owner`; RLS on a partition before it is attached; `hamada_maint` DML-only and never a member of the owner | 4.0, 4.10 |
| B5 | The move clears `owned` on the source first and waits out older transactions; the clinic is checked before the key is claimed; any non-2xx rolls back | 3.4, 5.0 |
| B6 | Adding a member reuses only an account the same request created; otherwise the invitation path; "never change an existing account"; no temporary password on replay | 5.8, 5.12 |
| I1 | `UNIQUE (clinic_id, id)` on payment, item, visit note, category and expense; audit list indexes end with `id` | 4.5–4.8, 4.10 |
| I2 | The counters table; `changed_since_recompute`; one clinic-day per recompute transaction | 4.4, 5.3, 5.10 |
| I3 | `status` on request keys; the endpoints that take no key | 4.11, 5.0 |
| I4 | Refund lock order in the text; the sweep's lock row; expenses in the global order; one statement for two clinic-days | 5.0, 5.3 |
