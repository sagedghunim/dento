# The month's expired or broken stock, read by the expenses page (Saged, 2026-11-27). Built concurrently: the
# movement table is the big one on the live database, and a plain CREATE INDEX would block stock writes.

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models
from django.db.models import F


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('inventory', '0011_validate_supplier_backup'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='movement',
            index=models.Index('clinic', F('at').desc(), F('id').desc(), condition=models.Q(('kind', 'waste')), name='movement_waste'),
        ),
    ]
