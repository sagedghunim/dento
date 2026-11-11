"""Raw SQL on the request's shard, for the statements the ORM can't express (design 5.0-5.3).

Conditional updates with RETURNING, upserts on the summary tables and data-modifying CTEs
need SQL. Tenant tables are written with quoted names so the SQL-shape check sees them.
"""

from django.db import connections


def rows(ctx, sql, params=()):
    with connections[ctx.alias].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def row(ctx, sql, params=()):
    found = rows(ctx, sql, params)
    return found[0] if found else None


def dicts(ctx, sql, params=()):
    with connections[ctx.alias].cursor() as cursor:
        cursor.execute(sql, params)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, values)) for values in cursor.fetchall()]


def run(ctx, sql, params=()):
    with connections[ctx.alias].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def statement_timeout(ctx, value):
    """SET LOCAL, so it ends with the request's transaction (design 4.0: exceptions use SET LOCAL).

    None puts back the role's own setting for the rest of the transaction.
    """
    with connections[ctx.alias].cursor() as cursor:
        if value is None:
            cursor.execute("SET LOCAL statement_timeout = DEFAULT")
        else:
            cursor.execute(f"SET LOCAL statement_timeout = '{int(value)}ms'")
