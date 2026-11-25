# What a visit took by hand, read by the visit page (Saged, 2026-11-25). Built concurrently: the movement table is
# the big one on the live database, and a plain CREATE INDEX would block stock writes while it builds.

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('inventory', '0004_kits'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='movement',
            index=models.Index(condition=models.Q(('appointment__isnull', False)), fields=['clinic', 'appointment'], name='movement_appointment'),
        ),
    ]
