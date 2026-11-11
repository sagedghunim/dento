"""The sections migrations on a shard that already holds M1 data (design D1): backfill, then NOT NULL."""

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from core.routing import id_range

ALIAS = "shard_2"
CLINIC = 990_001


class SectionBackfillTests(TransactionTestCase):
    databases = "__all__"

    def migrate(self, targets):
        connection = connections[ALIAS]
        executor = MigrationExecutor(connection)
        executor.migrate(targets or executor.loader.graph.leaf_nodes())

    def test_existing_clinics_get_one_section_and_every_row_its_id(self):
        self.migrate([("clinics", "0001_initial"), ("scheduling", "0001_initial")])
        try:
            with connections[ALIAS].cursor() as cursor:
                cursor.execute("BEGIN")
                cursor.execute("SELECT set_config('app.clinic_id', %s, true)", [str(CLINIC)])
                cursor.execute("""INSERT INTO clinics_clinic (id, owned, name, city, address, phone, timezone, day_ends_at,
                                  specialty_code, status, settings, created_at)
                                  VALUES (%s, true, 'Old', '', '', '', 'Africa/Cairo', '04:00', 'pediatrics', 'active',
                                  '{"print_language": "ar"}', now())""", [CLINIC])
                cursor.execute("""INSERT INTO clinics_membership (clinic_id, user_id, role, display_name, title, is_active,
                                  granted, revoked, created_at) VALUES (%s, 1, 'owner', 'O', '', true, '{}', '{}', now())
                                  RETURNING id""", [CLINIC])
                doctor = cursor.fetchone()[0]
                cursor.execute("""INSERT INTO clinics_service (clinic_id, name, kind, price, duration_min, is_active, sort)
                                  VALUES (%s, 'كشف', 'consultation', 100, 15, true, 0) RETURNING id""", [CLINIC])
                service = cursor.fetchone()[0]
                cursor.execute("""INSERT INTO clinics_schedule (clinic_id, doctor_id, weekday, starts, ends, mode)
                                  VALUES (%s, %s, 1, '17:00', '22:00', 'queue')""", [CLINIC, doctor])
                cursor.execute("""INSERT INTO patients_patient (clinic_id, file_no, full_name, name_norm, phone, phone_norm,
                                  sex, birth_date_estimated, address, notes, allergies, chronic_conditions, created_at,
                                  updated_at) VALUES (%s, 1, 'P', 'p', '', '', '', false, '', '', '', '', now(), now())
                                  RETURNING id""", [CLINIC])
                patient = cursor.fetchone()[0]
                cursor.execute("""INSERT INTO scheduling_appointment (clinic_id, patient_id, doctor_id, service_id, day,
                                  queue_no, is_extra, status, source, fee, is_follow_up, is_new, auto_closed, note,
                                  cancel_reason, created_at, updated_at) VALUES (%s, %s, %s, %s, '2026-11-10', 1, false,
                                  'booked', 'clinic', 100, false, true, false, '', '', now(), now())""",
                               [CLINIC, patient, doctor, service])
                cursor.execute("COMMIT")
        finally:
            self.migrate(None)

        low, high = id_range(ALIAS)
        with connections[ALIAS].cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("SELECT set_config('app.clinic_id', %s, true)", [str(CLINIC)])
            cursor.execute("SELECT id, name, specialty_code, is_active FROM clinics_section WHERE clinic_id = %s", [CLINIC])
            [(section, name, specialty, active)] = cursor.fetchall()
            cursor.execute("SELECT kind FROM clinics_clinic WHERE id = %s", [CLINIC])
            kind = cursor.fetchone()[0]
            found = {}
            for table in ("clinics_service", "clinics_schedule", "scheduling_appointment"):
                cursor.execute(f"SELECT DISTINCT section_id FROM {table} WHERE clinic_id = %s", [CLINIC])
                found[table] = [r[0] for r in cursor.fetchall()]
            cursor.execute("COMMIT")
            cursor.execute("""SELECT relname FROM pg_class WHERE relname IN ('clinics_clinic', 'clinics_section',
                              'clinics_service', 'clinics_schedule', 'scheduling_appointment') AND relforcerowsecurity""")
            forced = sorted(r[0] for r in cursor.fetchall())
        self.assertEqual((name, specialty, active, kind), ("أطفال", "pediatrics", True, "clinic"))
        self.assertTrue(low <= section <= high, section)  # the new table's ids come from the shard's range
        self.assertEqual(found, {"clinics_service": [section], "clinics_schedule": [section],
                                 "scheduling_appointment": [section]})
        self.assertEqual(forced, ["clinics_clinic", "clinics_schedule", "clinics_section", "clinics_service",
                                  "scheduling_appointment"])
