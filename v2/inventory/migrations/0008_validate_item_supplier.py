# Validates the item table's new checks and keys from 0007 and builds its supplier index without blocking stock writes
# (design H1). VALIDATE takes a SHARE UPDATE EXCLUSIVE lock: reads and writes go on while it scans.

from django.db import migrations

VALIDATE = ["item_max_qty_valid", "item_backup_not_supplier", "item_supplier_fk", "item_backup_supplier_fk"]


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('inventory', '0007_suppliers_orders'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[f'ALTER TABLE "inventory_item" VALIDATE CONSTRAINT "{name}"' for name in VALIDATE],
            reverse_sql=migrations.RunSQL.noop,
        ),
        # The index is already in the model state (0007); here it is only built.
        migrations.RunSQL(
            sql=["SET lock_timeout = '5s'",
                 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "item_supplier" ON "inventory_item" ("clinic_id", "supplier_id") '
                 'WHERE "supplier_id" IS NOT NULL',
                 "RESET lock_timeout"],
            reverse_sql=['DROP INDEX CONCURRENTLY IF EXISTS "item_supplier"'],
        ),
    ]
