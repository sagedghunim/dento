"""Monthly partitions for the log tables (design 4.10).

- The parent is created by hand with RunSQL, partitioned by month on its time column.
- There is no DEFAULT partition: a missing partition makes inserts fail loudly.
- A new partition is created standalone, with a CHECK matching its bounds, and
  then attached. ATTACH takes only a ShareUpdateExclusiveLock on the parent, so
  inserts (every file open writes one) don't wait, as they would behind
  CREATE TABLE ... PARTITION OF. The CHECK lets ATTACH skip scanning the table.
- `ensure_partitions()` keeps the current month and `months_ahead` (6) more. A daily
  job runs it (core.jobs), and so does every `migrate_all`; fewer than 3 future
  partitions logs an error for the alert. Six months gives a small team a quarter to
  notice a dead worker (chief judge M1, R5).
- Old partitions leave with DETACH PARTITION ... CONCURRENTLY (`detach_partition`).
"""

import datetime as dt
import logging

from django.db import connections

log = logging.getLogger(__name__)

MONTHS_AHEAD = 6
ALERT_BELOW = 3

# table -> (database kind, time column, months kept online)
PARTITIONED_TABLES = {
    "audit_access": ("shard", "at", 24),
    "accounts_account_event": ("directory", "at", 12),
}


def _month_start(day):
    return dt.date(day.year, day.month, 1)


def _add_months(month, count):
    index = month.year * 12 + (month.month - 1) + count
    return dt.date(index // 12, index % 12 + 1, 1)


def partition_name(table, month):
    return f"{table}_y{month.year}m{month.month:02d}"


def existing_partitions(connection, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s ORDER BY 1",
            [table],
        )
        return [row[0] for row in cursor.fetchall()]


def tables_for(alias):
    kind = "directory" if alias == "default" else "shard"
    return [table for table, (table_kind, _, _) in PARTITIONED_TABLES.items() if table_kind == kind]


def ensure_partitions(alias, months_ahead=MONTHS_AHEAD, today=None, tables=None):
    """Create missing monthly partitions on one database. Returns {table: future partitions}."""
    connection = connections[alias]
    today = today or dt.datetime.now(dt.UTC).date()
    current = _month_start(today)
    result = {}
    for table in tables or tables_for(alias):
        column = PARTITIONED_TABLES[table][1]
        existing = set(existing_partitions(connection, table))
        with connection.cursor() as cursor:
            for offset in range(0, months_ahead + 1):
                start = _add_months(current, offset)
                name = partition_name(table, start)
                if name in existing:
                    continue
                end = _add_months(start, 1)
                lower, upper = f"'{start.isoformat()} 00:00:00+00'", f"'{end.isoformat()} 00:00:00+00'"
                cursor.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (LIKE "{table}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS)')
                cursor.execute(
                    f'ALTER TABLE "{name}" ADD CONSTRAINT "{name}_bounds" '
                    f'CHECK ("{column}" IS NOT NULL AND "{column}" >= {lower} AND "{column}" < {upper})'
                )
                # A partition read directly would skip the parent's policy, so it gets the
                # same one, before it is attached (design 4.10).
                cursor.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", [table])
                if cursor.fetchone()[0]:
                    from core.rls import enable_sql

                    for statement in enable_sql(name):
                        cursor.execute(statement)
                cursor.execute(f'ALTER TABLE "{table}" ATTACH PARTITION "{name}" FOR VALUES FROM ({lower}) TO ({upper})')
                # The bound check has done its job; the partition constraint replaces it.
                cursor.execute(f'ALTER TABLE "{name}" DROP CONSTRAINT "{name}_bounds"')
                existing.add(name)
        future = sum(1 for name in existing if name > partition_name(table, current))
        result[table] = future
        if future < ALERT_BELOW:
            log.error("Only %s future partitions exist for %s on %s", future, table, alias)
    return result


def ensure_all(today=None, months_ahead=MONTHS_AHEAD):
    """The directory and every shard, each in its own transaction. Returns {alias: {table: future partitions}}."""
    from django.conf import settings
    from django.db import transaction

    found = {}
    for alias in ["default", *settings.SHARDS]:
        with transaction.atomic(using=alias):
            found[alias] = ensure_partitions(alias, months_ahead=months_ahead, today=today)
    return found


def droppable_partitions(alias, table, keep_months, today=None):
    """Partitions older than the retention window, oldest first."""
    today = today or dt.datetime.now(dt.UTC).date()
    cutoff = partition_name(table, _add_months(_month_start(today), -keep_months))
    return [name for name in existing_partitions(connections[alias], table) if name < cutoff]


def detach_partition(alias, table, name):
    """Must run outside a transaction (DETACH ... CONCURRENTLY can't run inside one)."""
    with connections[alias].cursor() as cursor:
        cursor.execute(f'ALTER TABLE "{table}" DETACH PARTITION "{name}" CONCURRENTLY')
