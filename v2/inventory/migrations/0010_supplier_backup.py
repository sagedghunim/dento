# The supplier backup and the "needs a supplier" list (design H1 backup, app-h1-supplier-backup.md).
# The columns are metadata-only adds (constant defaults). The checks and the key go in NOT VALID so no table is scanned
# under its lock; 0011 validates them and builds the partial index without blocking writes.

import django.db.models.deletion
from django.db import migrations, models

import core.constraints


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0009_supplier_visits'),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '5s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name='item',
            name='need_qty',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='orderline',
            name='is_backup',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='orderline',
            name='missing',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='supplier',
            name='backup',
            field=models.ForeignKey(blank=True, db_constraint=False, db_index=False, null=True,
                                    on_delete=django.db.models.deletion.PROTECT, related_name='+',
                                    to='inventory.supplier'),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name='item',
                    index=models.Index(condition=models.Q(('need_qty__gt', 0)), fields=['clinic'], name='item_need'),
                ),
                migrations.AddConstraint(
                    model_name='item',
                    constraint=models.CheckConstraint(condition=models.Q(('need_qty__gte', 0)), name='item_need_qty_valid'),
                ),
                migrations.AddConstraint(
                    model_name='orderline',
                    constraint=models.CheckConstraint(condition=models.Q(('missing__gte', 0), ('missing__lte', models.F('qty'))), name='order_line_missing_valid'),
                ),
                migrations.AddConstraint(
                    model_name='supplier',
                    constraint=models.CheckConstraint(condition=models.Q(('backup', models.F('id')), _negated=True), name='supplier_backup_not_self'),
                ),
                migrations.AddConstraint(
                    model_name='supplier',
                    constraint=core.constraints.CompositeForeignKey(fields=('clinic', 'backup'), name='supplier_backup_fk', to='inventory.Supplier', to_fields=('clinic', 'id')),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "SET LOCAL lock_timeout = '5s'",
                        'ALTER TABLE "inventory_item" ADD CONSTRAINT "item_need_qty_valid" CHECK ("need_qty" >= 0) NOT VALID',
                        'ALTER TABLE "inventory_order_line" ADD CONSTRAINT "order_line_missing_valid" '
                        'CHECK ("missing" >= 0 AND "missing" <= "qty") NOT VALID',
                        'ALTER TABLE "inventory_supplier" ADD CONSTRAINT "supplier_backup_not_self" '
                        'CHECK (NOT ("backup_id" IS NOT NULL AND "backup_id" = "id")) NOT VALID',
                        'ALTER TABLE "inventory_supplier" ADD CONSTRAINT "supplier_backup_fk" '
                        'FOREIGN KEY ("clinic_id", "backup_id") '
                        'REFERENCES "inventory_supplier" ("clinic_id", "id") ON DELETE RESTRICT NOT VALID',
                    ],
                    reverse_sql=[
                        'ALTER TABLE "inventory_supplier" DROP CONSTRAINT "supplier_backup_fk"',
                        'ALTER TABLE "inventory_supplier" DROP CONSTRAINT "supplier_backup_not_self"',
                        'ALTER TABLE "inventory_order_line" DROP CONSTRAINT "order_line_missing_valid"',
                        'ALTER TABLE "inventory_item" DROP CONSTRAINT "item_need_qty_valid"',
                    ],
                ),
            ],
        ),
    ]
