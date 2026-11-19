# Every place gets the usual patient tags it doesn't have yet (VIP, blocklist...; Saged, 2026-11-19).
# patients.tags.add_defaults adds by name only, so this is safe on any data; a fresh database does nothing.

from django.db import migrations


def add_tags(apps, schema_editor):
    from core.routing import clinic_scope, set_clinic_setting
    from patients.tags import add_defaults

    alias = schema_editor.connection.alias
    with schema_editor.connection.cursor() as cursor:
        # Listing the places needs no clinic set; RLS is forced even for the owner, so lift it for this one read.
        cursor.execute('ALTER TABLE "clinics_clinic" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("SELECT id FROM clinics_clinic ORDER BY id")
        clinics = [row[0] for row in cursor.fetchall()]
        cursor.execute('ALTER TABLE "clinics_clinic" FORCE ROW LEVEL SECURITY')
    for clinic_id in clinics:
        with clinic_scope(clinic_id, alias):
            set_clinic_setting(alias, clinic_id)
            add_defaults(clinic_id, alias)
    set_clinic_setting(alias, None)


class Migration(migrations.Migration):
    dependencies = [("patients", "0005_patient_archive"), ("clinics", "0006_alerts_seen")]
    operations = [migrations.RunPython(add_tags, migrations.RunPython.noop)]
