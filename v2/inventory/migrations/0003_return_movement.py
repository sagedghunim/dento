# Removing a procedure recorded by mistake (Saged, 2026-11-17): its materials come back as a `return` movement,
# found by the record's id.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0002_item_name_en'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='movement',
            name='movement_kind_valid',
        ),
        migrations.AddConstraint(
            model_name='movement',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ['purchase', 'use', 'adjust', 'waste', 'return'])), name='movement_kind_valid'),
        ),
        migrations.AddIndex(
            model_name='movement',
            index=models.Index(condition=models.Q(('visit_procedure_id__isnull', False)), fields=['clinic', 'visit_procedure_id'], name='movement_visit_procedure'),
        ),
    ]
