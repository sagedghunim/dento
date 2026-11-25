## 12. Decisions and trade-offs

1. **A modular monolith,** not microservices. A small team can run it, and the clinic boundary is already in the data.
2. **Application-level shards of whole clinics,** not Citus (chief judge D1). Stock PostgreSQL, plain Django relations, and the whole topology testable on the Windows machine. The cost is a router, migrations over every shard, a clinic-move tool and a few cross-database flows.
3. **An `id` primary key from per-shard ranges, with composite foreign keys,** not composite primary keys. Django handles single-column keys far better, and no key ever needs swapping.
4. **Row-level security from M0** (D4). Isolation by the database before go-live, while it is cheapest, with its two traps handled: range predicates instead of `LIKE` for prefixes, and `NULLIF` for pooled connections.
5. **A request-key table with a request hash** (D2), not keys on business rows and not a day-partitioned table. It is bounded (72 hours), covers every mutating request, and refuses a reused key with a different body.
6. **Server-rendered pages** (D3), not a single-page app. One catalogue, server-side formatting, Django tests, fresh assets on every navigation.
7. **Partition the logs, not the business tables** (4.10).
8. **Polling with an ETag,** not WebSockets or server-sent events. It works through any proxy and is cheap per 304. Revisit if updates must arrive in under a second.
9. **Procrastinate in the directory,** not Celery and a broker.
10. **Status counts from the day's rows,** not counters; summaries hold only money, limits and the version.
11. **Authenticator codes for owners,** not SMS. SMS arrives later with password reset.
