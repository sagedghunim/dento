# Centers with sections (design draft 2.2, Appendix D1): appointment.section_id, copied from the
# service as booking does, then NOT NULL and a composite key to clinics_section.

import django.db.models.deletion
from django.db import migrations, models

import core.constraints

TABLES = ("scheduling_appointment", "clinics_service")


def backfill_section(apps, schema_editor):
    """Each appointment takes its service's section. FORCE is lifted for this transaction only (see clinics 0002)."""
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("""
            UPDATE "scheduling_appointment" a SET section_id = s.section_id
              FROM "clinics_service" s
             WHERE s.clinic_id = a.clinic_id AND s.id = a.service_id AND a.section_id IS NULL""")
        for table in TABLES:
            cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


class Migration(migrations.Migration):

    dependencies = [
        ('clinics', '0002_sections'),
        ('scheduling', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.RunPython(backfill_section, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='appointment',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.AddConstraint(
            model_name='appointment',
            constraint=core.constraints.CompositeForeignKey(fields=('clinic', 'section'), name='appointment_section_fk', to='clinics.Section', to_fields=('clinic', 'id')),
        ),
    ]
