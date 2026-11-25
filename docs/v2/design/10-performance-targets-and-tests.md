## 10. Performance targets and tests

| Request | p95 server time at the target | Statements |
|---|---|---|
| Today, unchanged (304) | 20 ms | 2 directory (session, user) + 3 shard (`set_config`, membership, version) |
| Today, full fragment | 150 ms | 3 shard + version, appointments, doctor-days, doctors, items |
| Book (with arrive and payment) | 200 ms | at most 13 (12, plus the `FOR SHARE` on the doctor's row) |
| Status change | 100 ms | 5–7 |
| Payment | 150 ms | at most 10 |
| Patient search | 150 ms (300 ms timeout) | 1–2 |
| Patient file | 200 ms | fixed |

**Tests:**
- **T1, isolation:** every endpoint with another clinic's ids, in paths and in bodies (patient, doctor, service, medicine, item) answers 404 and writes nothing. Lists never show other clinics' rows. Clinics live on both test shards.
- **T2, permissions:** each view without its codes is 403; a single grant gives 200; a removal from the template gives 403; the owner always gets 200; the 5.8 grant rules; a platform admin gets 403 or 404 on every tenant endpoint. Generated from the URL configuration.
- **T3, repeats:** for every mutating endpoint, from the URL configuration: the same key twice gives one effect and the same answer; two threads with one key give one effect and two 200s; the same key with another body gives 422; a retry after a 409 runs again.
- **T4, the transition matrix:** every action against every status; repeats leave the counters unchanged.
- **T5, concurrency on PostgreSQL** with real threads:
  - 8 bookings of one slot: one succeeds;
  - 8 bookings for one doctor-day: turns 1 to 8, never repeated;
  - concurrent first payments: no invoice-number gap;
  - payment against cancel: never a paid cancelled visit;
  - crossing reschedules: no deadlock;
  - "book, collect and print" against a finish with payment for the same doctor and day: no deadlock;
  - a payment, a refund and a discount on one invoice;
  - the last unit of stock;
  - the recompute against live writes;
  - one two-factor code on two connections.
- **T6, throttles** across two processes, not threads.
- **T7, money invariants** after random sequences, including cancel after payment, reschedule after prepayment, refunds on another day, and void then re-invoice: `invoice.paid = Σ payments − Σ refunds`; doctor-day sums equal clinic-day sums; summaries equal the recompute.
- **T8, time:** `day_ends_at`, a session crossing midnight, Egypt's clock changes in April and October, a clinic in another time zone.
- **T9, the access log:** every view declared `access` writes exactly one event, and no other view returns patient fields.
- **T10, security:** a session signed out through one process is refused by another; CSP, `no-store`, CSRF on every mutating method, a new session key at sign-in and after the code, a pending or must-change-password session gets 403 elsewhere, the code-guess limit.
- **T11, row-level security:** no setting gives no rows; a reused connection gives no rows; another clinic's id in WHERE gives nothing; an INSERT for another clinic is refused; every shard table with `clinic_id` is forced and has the standard policy.
- **T12, routing and shards:** clinics on two shards; a tenant query without a context raises; a wrong-shard request is refused; every sequence is in its shard's range.
- **T13, the SQL shape:** a test-time `execute_wrapper` fails any statement whose root tenant table isn't filtered on `clinic_id`.
- **T14, query counts:** each hot request runs the same number of statements with 10 rows or 10,000.
- **T15, pages and fragments:** Django test-client tests for every page and fragment, including the bidi order of a phone and the translation catalogue test.
- **T16, browser scripts** (headless Edge): a double tap, a lost answer, a 401 mid-save, the RTL order of a phone, and the A5 and 80 mm page sizes.
- **Plans** (M1): the hot queries for a 100,000-file clinic, as the web role, under RLS.
- **Synthetic data and load** (M5): `generate_load_data` builds rows the way the app inserts them (indexes grown, not bulk-built), and the load test states its pass criteria at stage-1 size: p95 and error rate for 304s and for the write mix.
- A failing test comes first for every stock, money, permission and status bug (CLAUDE.md).
