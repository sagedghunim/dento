# Places created before M2 get their sections' procedures, medicines and prescription templates (design E3, E4).
#
# copy_specialty only adds what a section doesn't have, so this is safe to run on any data, and a fresh database
# (no sections yet) does nothing. It uses the live models: on a database with M1 places it must run together with
# the M2 migrations, before later migrations change these tables.

from django.db import migrations


def copy_catalogues(apps, schema_editor):
    from types import SimpleNamespace

    from clinical.services import copy_specialty
    from core.apps import set_sequence_ranges
    from core.routing import clinic_scope, set_clinic_setting

    alias = schema_editor.connection.alias
    set_sequence_ranges(using=alias)  # the new tables' ids come from the shard's range before the first row
    with schema_editor.connection.cursor() as cursor:
        # Listing the places needs no clinic set; RLS is forced even for the owner, so lift it for this one read
        # (production migrates as hamada_owner, which bypasses RLS anyway). Every write below runs with its clinic set.
        cursor.execute('ALTER TABLE "clinics_clinic" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("SELECT id FROM clinics_clinic ORDER BY id")
        clinics = [row[0] for row in cursor.fetchall()]
        cursor.execute('ALTER TABLE "clinics_clinic" FORCE ROW LEVEL SECURITY')
    for clinic_id in clinics:
        with clinic_scope(clinic_id, alias):
            set_clinic_setting(alias, clinic_id)  # row-level security is forced; this transaction only
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("SELECT c.settings->>'print_language', s.id, s.specialty_code FROM clinics_section s "
                               "JOIN clinics_clinic c ON c.id = s.clinic_id WHERE s.clinic_id = %s ORDER BY s.sort, s.id",
                               [clinic_id])
                sections = cursor.fetchall()
            for language, section_id, specialty in sections:
                copy_specialty(clinic_id, SimpleNamespace(id=section_id, specialty_code=specialty), language or "ar")
        set_clinic_setting(alias, None)


class Migration(migrations.Migration):

    dependencies = [
        ('clinical', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(copy_catalogues, migrations.RunPython.noop),
    ]
