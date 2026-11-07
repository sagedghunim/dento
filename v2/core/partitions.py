"""Monthly partitions for the log tables (design 4.10). Reused from the warehouse project.

- A parent table is created with RunSQL, partitioned by month on its timestamp, with a primary key of (id, <timestamp>).
- There is no DEFAULT partition. A missing partition makes inserts fail loudly instead of hiding rows.
- `ensure_partitions()` creates the current month and the next `months_ahead` months. A daily job runs it, and an alert fires when fewer than 2 future partitions exist.
- To add an index to an existing large partitioned table, use `add_index_online()`. It creates the index ON ONLY the parent, builds it on each partition concurrently, and attaches each one.
"""

import datetime as dt
import logging

from django.db import connection

log = logging.getLogger(__name__)

# table -> (partition column, months kept online)
PARTITIONED_TABLES = {
    "audit_event": ("at", 24),
    "accounts_login_event": ("at", 12),
}


def _month_start(day):
    return dt.date(day.year, day.month, 1)


def _add_months(month, count):
    index = month.year * 12 + (month.month - 1) + count
    return dt.date(index // 12, index % 12 + 1, 1)


def partition_name(table, month):
    return f"{table}_y{month.year}m{month.month:02d}"


def existing_partitions(table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s ORDER BY 1",
            [table],
        )
        return [row[0] for row in cursor.fetchall()]


def ensure_partitions(months_ahead=3, today=None, tables=None):
    """Create missing monthly partitions. Returns {table: number of future partitions}."""
    today = today or dt.datetime.now(dt.UTC).date()
    current = _month_start(today)
    result = {}
    for table in tables or PARTITIONED_TABLES:
        existing = set(existing_partitions(table))
        with connection.cursor() as cursor:
            for offset in range(0, months_ahead + 1):
                start = _add_months(current, offset)
                name = partition_name(table, start)
                if name in existing:
                    continue
                end = _add_months(start, 1)
                cursor.execute(
                    f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table}" '
                    f"FOR VALUES FROM ('{start.isoformat()} 00:00:00+00') TO ('{end.isoformat()} 00:00:00+00')"
                )
                existing.add(name)
        future = sum(1 for name in existing if name > partition_name(table, current))
        result[table] = future
        if future < 2:
            log.error("Only %s future partitions exist for %s", future, table)
    return result


def add_index_online(table, index_name, definition):
    """Add an index to a partitioned table without locking every partition at once.

    `definition` is the part after the table name, e.g. "USING btree (sku_id, at)".
    Must run outside a transaction (CREATE INDEX CONCURRENTLY can't run inside one).
    """
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE INDEX IF NOT EXISTS "{index_name}" ON ONLY "{table}" {definition}')
        for partition in existing_partitions(table):
            child = f"{partition}_{index_name}"[:63]
            cursor.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{child}" ON "{partition}" {definition}')
            cursor.execute(f'ALTER INDEX "{index_name}" ATTACH PARTITION "{child}"')


def droppable_partitions(table, keep_months, today=None):
    """Partitions older than the retention window, oldest first."""
    today = today or dt.datetime.now(dt.UTC).date()
    cutoff = partition_name(table, _add_months(_month_start(today), -keep_months))
    return [name for name in existing_partitions(table) if name < cutoff]
