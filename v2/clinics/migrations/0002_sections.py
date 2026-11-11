# Centers with sections (design draft 2.2, Appendix D1).
#
# Adds clinic.kind and the clinics_section table, then gives every existing clinic one section
# named after its specialty, fills section_id on its services and sessions, and only then makes
# section_id NOT NULL and adds the composite keys. Service names become unique per section, since
# every section of a center has its own "Consultation".

import django.db.models.deletion
from django.db import migrations, models

import core.constraints
from core.rls import EnableRLS

# The specialty names as they were when this migration was written (clinics/specialties.py may change later).
SECTION_NAMES = {
    "dental": ("أسنان", "Dental"),
    "pediatrics": ("أطفال", "Pediatrics"),
    "dermatology": ("جلدية", "Dermatology"),
    "internal": ("باطنة", "Internal medicine"),
    "ophthalmology": ("رمد", "Ophthalmology"),
    "physiotherapy": ("علاج طبيعي", "Physiotherapy"),
}
TABLES = ("clinics_clinic", "clinics_section", "clinics_service", "clinics_schedule")


def backfill_sections(apps, schema_editor):
    """One section per existing clinic, then section_id on its services and sessions.

    Row-level security is forced even for the table owner, and a migration has no clinic set, so the
    owner lifts FORCE for the length of this transaction (production migrates as hamada_owner, which
    bypasses RLS anyway). The new sequence gets the shard's id range before the first row.
    """
    from core.apps import set_sequence_ranges

    connection = schema_editor.connection
    set_sequence_ranges(using=connection.alias)
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("SELECT c.id, c.specialty_code, c.settings->>'print_language' FROM clinics_clinic c "
                       "WHERE NOT EXISTS (SELECT 1 FROM clinics_section s WHERE s.clinic_id = c.id) ORDER BY c.id")
        for clinic_id, specialty, language in cursor.fetchall():
            arabic, english = SECTION_NAMES.get(specialty, (specialty, specialty))
            cursor.execute("INSERT INTO clinics_section (clinic_id, name, specialty_code, is_active, sort, created_at) "
                           "VALUES (%s, %s, %s, true, 0, now())",
                           [clinic_id, english if language == "en" else arabic, specialty])
        for table in ("clinics_service", "clinics_schedule"):
            cursor.execute(f"""
                UPDATE "{table}" t SET section_id = s.id
                  FROM (SELECT DISTINCT ON (clinic_id) clinic_id, id FROM clinics_section ORDER BY clinic_id, sort, id) s
                 WHERE s.clinic_id = t.clinic_id AND t.section_id IS NULL""")
        # Deferred foreign-key checks must not be pending when the tables are altered below.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for table in TABLES:
            cursor.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


class Migration(migrations.Migration):

    dependencies = [
        ('clinics', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='clinic',
            name='kind',
            field=models.CharField(choices=[('clinic', 'Clinic'), ('center', 'Center')], default='clinic', max_length=6),
        ),
        migrations.AddConstraint(
            model_name='clinic',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ['clinic', 'center'])), name='clinic_kind_valid'),
        ),
        migrations.CreateModel(
            name='Section',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('specialty_code', models.CharField(max_length=30)),
                ('is_active', models.BooleanField(default=True)),
                ('sort', models.PositiveSmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('clinic', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.clinic')),
            ],
            options={
                'db_table': 'clinics_section',
                'constraints': [
                    models.UniqueConstraint(fields=('clinic', 'id'), name='section_clinic_id_uniq'),
                    models.UniqueConstraint(condition=models.Q(('is_active', True)), fields=('clinic', 'name'), name='section_active_name_uniq'),
                ],
            },
        ),
        EnableRLS("clinics_section"),
        migrations.AddField(
            model_name='service',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.AddField(
            model_name='schedule',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.RunPython(backfill_sections, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='service',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.AlterField(
            model_name='schedule',
            name='section',
            field=models.ForeignKey(db_constraint=False, db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='clinics.section'),
        ),
        migrations.RemoveConstraint(
            model_name='service',
            name='service_name_uniq',
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(fields=('clinic', 'section', 'name'), name='service_section_name_uniq'),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=core.constraints.CompositeForeignKey(fields=('clinic', 'section'), name='service_section_fk', to='clinics.Section', to_fields=('clinic', 'id')),
        ),
        migrations.AddConstraint(
            model_name='schedule',
            constraint=core.constraints.CompositeForeignKey(fields=('clinic', 'section'), name='schedule_section_fk', to='clinics.Section', to_fields=('clinic', 'id')),
        ),
    ]
