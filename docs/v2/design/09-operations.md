## 9. Operations

### 9.1 Deploying and migrating

- `migrate_all` runs as a one-off step, as `hamada_owner`, before the web containers update. Schema changes use expand and contract, so old and new code both work during a deploy.
- **Migrations on large tables:** `lock_timeout` with retries, `CREATE INDEX CONCURRENTLY`, and a stored generated column only in a table's first migration (it can't be added cheaply later). A constraint that needs a full scan (an exclusion constraint) goes in while the table is empty.
- **Backfilling a column on a table with data** (M2 medicines and M3 stock items carry `section_id`; M4 recomputes `day`):
  1. **No `NO FORCE ROW LEVEL SECURITY`** in a shard migration: it takes ACCESS EXCLUSIVE until commit and stops every request on the shard. Production migrates as `hamada_owner`, which bypasses RLS; dev and tests give the owner BYPASSRLS or backfill per clinic with `set_config`.
  2. **Migration A, short locks only:** the nullable column; its foreign key `NOT VALID` (`CompositeForeignKey` gets a `validate=False` option); a not-null `CHECK (col IS NOT NULL) NOT VALID`. From then on the code writes the column.
  3. **A resumable management command** backfills in keyset batches of about 5,000 on `(clinic_id, id)`, each batch its own transaction with `lock_timeout`, logging progress.
  4. **Migration B:** `VALIDATE CONSTRAINT` on both (SHARE UPDATE EXCLUSIVE only), then `SET NOT NULL`, which uses the validated check and skips the scan, then drop the `CHECK`; `SeparateDatabaseAndState` keeps Django's state in step.
  - The tooling is written with the first backfill on a table that holds data. The `section_id` backfill already applied stays as it is: no shard held live data.

### 9.2 Availability

- **Every database** (the directory and each shard) runs with high availability: a managed PostgreSQL with HA, or Patroni with a tested promotion.
- **PgBouncer** points at the cluster's write endpoint and reconnects after a promotion. In-flight requests fail and the client resends them with the same keys (7.4), so nothing is done twice.
- **Redis**, when used, is only a speed-up: if it fails, requests fall back to PostgreSQL.
- **Backups:** pgBackRest or WAL-G with continuous WAL archiving to object storage, `archive_timeout` of at most 300 s for the 5-minute RPO, a daily backup, encryption, and a monthly restore drill per database.
- **RTO:** failover within minutes. A restore from backup within 2 hours per shard, which sets the shard size (3.4).

### 9.3 Monitoring

- `/healthz` and `/readyz`.
- Structured logs with the request id, clinic id, shard, user id, path, status, time and query count; no query strings.
- `pg_stat_statements` and a slow-query log (over 200 ms) per database.
- Alerts on error rate, p95, replication lag, disk, partition runway, job queue age, request-key table size, and summary differences found by the recompute.

### 9.4 Connections and roles

- Roles and per-role timeouts as in 4.0. With PgBouncer in transaction mode: `DISABLE_SERVER_SIDE_CURSORS`, no session-level state, and the clinic set per transaction.
- The Procrastinate worker connects directly for LISTEN, or polls.

### 9.5 Runbooks

Restore a database; fail over; migrate across shards; move a clinic; partition upkeep; incidents (slow every screen with `Poll-After`); rotate the field-encryption key and the secret key; support resets and ownership transfer; a PC with a wrong clock (HTTPS fails, so "check the date and time" comes first).

### 9.6 Local runs

`RUN_V2.bat` starts PostgreSQL, runs `migrate_all`, compiles translations, starts the worker, and serves http://127.0.0.1:8002.
