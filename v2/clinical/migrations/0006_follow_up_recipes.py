# Sections created before the follow-up recipes get the standard ones on their procedures (Saged, 2026-11-13).
#
# Only a procedure with the catalogue's Arabic name and no recipe yet is filled, so a clinic's own changes stay and
# running it again is harmless. A fresh database (no sections yet) does nothing.

import json

from django.db import migrations


def fill_recipes(apps, schema_editor):
    from clinics.specialties import SPECIALTIES
    from core.routing import set_clinic_setting

    alias = schema_editor.connection.alias
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('ALTER TABLE "clinics_clinic" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("SELECT id FROM clinics_clinic ORDER BY id")
        clinics = [row[0] for row in cursor.fetchall()]
        cursor.execute('ALTER TABLE "clinics_clinic" FORCE ROW LEVEL SECURITY')
    for clinic_id in clinics:
        set_clinic_setting(alias, clinic_id)  # row-level security is forced
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SELECT id, specialty_code FROM clinics_section WHERE clinic_id = %s", [clinic_id])
            for section_id, specialty in cursor.fetchall():
                spec = SPECIALTIES.get(specialty)
                for p in spec["procedures"] if spec else ():
                    if p["follow_up"]:
                        cursor.execute(
                            "UPDATE clinical_procedure SET follow_up = %s::jsonb WHERE clinic_id = %s AND section_id = %s "
                            "AND name_ar = %s AND follow_up = '[]'::jsonb",
                            [json.dumps(p["follow_up"], ensure_ascii=False), clinic_id, section_id, p["name_ar"]])
    set_clinic_setting(alias, None)


class Migration(migrations.Migration):

    dependencies = [
        ('clinical', '0005_follow_ups'),
    ]

    operations = [
        migrations.RunPython(fill_recipes, migrations.RunPython.noop),
    ]
