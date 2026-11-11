# Two slot visits of one doctor can't overlap (chief judge M1, R4). Built now, while the table is empty: an
# exclusion constraint can't be built concurrently. btree_gist is a trusted extension, so the owner role can create it.

import django.contrib.postgres.constraints
import scheduling.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clinics', '0002_sections'),
        ('patients', '0001_initial'),
        ('scheduling', '0002_appointment_section'),
    ]

    operations = [
        # Never dropped on the way back: other databases on the server (or template1) may already share it.
        migrations.RunSQL("CREATE EXTENSION IF NOT EXISTS btree_gist", reverse_sql=migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name='appointment',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('holds_slot', True)), expressions=[('clinic', '='), ('doctor', '='), (scheduling.models.TsTzRange('starts_at', 'ends_at'), '&&')], name='appointment_no_overlap'),
        ),
    ]
