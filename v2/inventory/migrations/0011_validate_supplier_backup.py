# Validates 0010's checks and key and builds the "needs a supplier" index without blocking stock writes.
# VALIDATE takes a SHARE UPDATE EXCLUSIVE lock: reads and writes go on while it scans.

from django.db import migrations

VALIDATE = [("inventory_item", "item_need_qty_valid"), ("inventory_order_line", "order_line_missing_valid"),
            ("inventory_supplier", "supplier_backup_not_self"), ("inventory_supplier", "supplier_backup_fk")]


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('inventory', '0010_supplier_backup'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"' for table, name in VALIDATE],
            reverse_sql=migrations.RunSQL.noop,
        ),
        # The index is already in the model state (0010); here it is only built.
        migrations.RunSQL(
            sql=["SET lock_timeout = '5s'",
                 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "item_need" ON "inventory_item" ("clinic_id") '
                 'WHERE "need_qty" > 0',
                 "RESET lock_timeout"],
            reverse_sql=['DROP INDEX CONCURRENTLY IF EXISTS "item_need"'],
        ),
    ]
