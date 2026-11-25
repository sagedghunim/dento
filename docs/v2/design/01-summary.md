## 1. Summary

- **One Django application** (a modular monolith) on **stock PostgreSQL**, plus a background worker. The web tier is stateless.
- **The tenant is the clinic.** Every tenant row carries `clinic_id`. Every index on a tenant table starts with it. References between tenant rows are composite foreign keys on `(clinic_id, id)`. **Row-level security**, forced on every tenant table from the first migration, refuses any row of another clinic even if a query forgets its filter.
- **Growth by application-level shards (3.3).** Each shard is an ordinary PostgreSQL database holding whole clinics. A small **directory** database holds accounts, sessions and the clinic-to-shard map. A Django router sends each request, and each job transaction, to its clinic's shard. Stage 1 is one cluster with one directory and one shard. Ids come from per-shard ranges, so a clinic can move between shards with its ids unchanged.
- **Hot paths are bounded.** Today, booking, status changes, payments and patient search each run a fixed number of indexed queries inside one clinic's rows, whatever the total size.
- **Server-rendered pages** (Django templates) with a few small JavaScript modules. No framework and no build step. Actions go through JSON endpoints under `/api/v1/`. Arabic first (right to left) and English, from one translation catalogue.
- **The live queue uses conditional polling.** Each clinic's business day has a version, bumped in the same transaction as any change Today shows. Today's queue is an HTML fragment polled with an ETag; nearly every poll answers `304`.
- **Concurrency through locks in one global order, conditional updates and constraints.** Every flow's locks are listed in 5.0. Counters and money summaries change in the same transaction as the work.
- **Safe repeats** through a request-key table in each database. A key is claimed first, in the work's own transaction, with a hash of the request. A repeat replays the first result; the same key with a different body is refused.
- **The business day ends at a clinic-set hour** (default 04:00), not at midnight, because evening sessions run late.
- **Health data:** permissions per person checked on the server, with grant rules that stop escalation; an access log for every read of clinical data; a change log; two-factor sign-in for owners; strict headers; no patient data in logs.
