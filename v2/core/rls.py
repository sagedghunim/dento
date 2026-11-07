"""Row-level security for tenant tables (design 4.0).

Every shard table with `clinic_id` gets, in the migration that creates it:

    operations = [..., EnableRLS("patients_patient")]

The policy reads `app.clinic_id`, which `core.routing.set_clinic_setting` sets
at the start of each transaction. NULLIF matters: on a pooled connection where
an earlier transaction set it, the setting reverts to '' rather than NULL, and
''::bigint would raise instead of returning no rows.
"""

from django.db import migrations

POLICY = "clinic_isolation"


def policy_expression(column="clinic_id"):
    return f"{column} = NULLIF(current_setting('app.clinic_id', true), '')::bigint"


def enable_sql(table, column="clinic_id"):
    expr = policy_expression(column)
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY {POLICY} ON "{table}" USING ({expr}) WITH CHECK ({expr})',
    ]


def disable_sql(table):
    return [
        f'DROP POLICY IF EXISTS {POLICY} ON "{table}"',
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY',
    ]


def EnableRLS(table, column="clinic_id"):
    """A migration operation. It runs only on shards, because the router allows tenant apps only there."""
    return migrations.RunSQL(sql=enable_sql(table, column), reverse_sql=disable_sql(table))
