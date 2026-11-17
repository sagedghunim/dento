# Removing a procedure recorded by mistake (Saged, 2026-11-17): what the undo puts back.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clinical', '0007_deposit_and_visit_price'),
    ]

    operations = [
        migrations.AddField(
            model_name='visitprocedure',
            name='plan_item_was',
            field=models.CharField(blank=True, db_default='', default='', max_length=12),
        ),
        migrations.AddField(
            model_name='visitprocedure',
            name='tooth_was',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
