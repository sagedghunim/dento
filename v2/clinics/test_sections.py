"""Centers with sections (design D1): the table, its isolation, the composite keys and the section endpoints."""

import datetime as dt

from django.db import IntegrityError, connections, transaction
from django.db.utils import ProgrammingError

from clinics.models import Clinic, Membership, Schedule, Section, Service
from core.errors import ApiError
from core.routing import clinic_transaction
from core.testing import ShardSQLCheck, ShardTestCase, call
from directory.flows import clean_sections
from directory.models import DirectoryClinic
from scheduling.testing import add_schedule

CENTER = [{"specialty": "dental"}, {"specialty": "pediatrics", "name": "Kids"}, {"specialty": "dermatology"}]


class SectionModelTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")
        self.c = self.clinic("c", "shard_2", sections=CENTER)

    def sections(self, fixture):
        with clinic_transaction(fixture.id, fixture.shard):
            return list(Section.objects.filter(clinic_id=fixture.id).order_by("sort", "id"))

    def test_a_clinic_has_one_section_named_after_its_specialty(self):
        [section] = self.sections(self.a)
        self.assertEqual((section.name, section.specialty_code, section.is_active), ("أسنان", "dental", True))
        with clinic_transaction(self.a.id, self.a.shard):
            clinic = Clinic.objects.get(id=self.a.id)
            self.assertEqual((clinic.kind, clinic.specialty_code), ("clinic", "dental"))
            self.assertEqual(set(Service.objects.filter(clinic_id=self.a.id).values_list("section_id", flat=True)),
                             {section.id})
        self.assertEqual(DirectoryClinic.objects.get(id=self.a.id).kind, "clinic")

    def test_a_center_copies_each_sections_template_and_links_follow_ups_inside_the_section(self):
        sections = self.sections(self.c)
        self.assertEqual([(s.name, s.specialty_code) for s in sections],
                         [("أسنان", "dental"), ("Kids", "pediatrics"), ("جلدية وتجميل", "dermatology")])
        with clinic_transaction(self.c.id, self.c.shard):
            clinic = Clinic.objects.get(id=self.c.id)
            self.assertEqual((clinic.kind, clinic.specialty_code), ("center", "dental"))
            services = list(Service.objects.filter(clinic_id=self.c.id))
        by_id = {s.id: s for s in services}
        for section in sections:
            mine = [s for s in services if s.section_id == section.id]
            self.assertTrue(mine)
            self.assertEqual(sum(s.kind == "consultation" for s in mine), 1)
            consult = next(s for s in mine if s.kind == "consultation")
            self.assertEqual(by_id[consult.follow_up_service_id].section_id, section.id)
        # Every section has its own "Consultation": names are unique per section, not per place.
        self.assertEqual(sum(s.name == "كشف" for s in services), 3)
        self.assertTrue(any(s.name == "جلسة ليزر" for s in services))
        self.assertEqual(DirectoryClinic.objects.get(id=self.c.id).kind, "center")

    def test_kind_and_section_count_rules(self):
        cases = [("clinic", [{"specialty": "dental"}, {"specialty": "pediatrics"}], "sections"),
                 ("center", [{"specialty": "dental"}], "sections"),
                 ("center", [{"specialty": "dental", "name": f"S{i}"} for i in range(21)], "sections"),
                 ("center", [{"specialty": "dental"}, {"specialty": "made-up"}], "sections.1.specialty"),
                 ("center", [{"specialty": "dental"}, {"specialty": "dental"}], "sections.1.name"),
                 ("center", [{"specialty": "dental", "name": "A"}, {"specialty": "pediatrics", "name": " a "}],
                  "sections.1.name"),
                 ("hospital", [{"specialty": "dental"}], "kind"),
                 ("clinic", "dental", "sections")]
        for kind, sections, field in cases:
            with self.subTest(kind=kind, field=field), self.assertRaises(ApiError) as caught:
                clean_sections(kind, sections)
            self.assertIn(field, caught.exception.fields)
        twenty = clean_sections("center", [{"specialty": "dental", "name": f"S{i}"} for i in range(20)])
        self.assertEqual(len(twenty), 20)
        self.assertEqual(clean_sections("clinic", [{"specialty": "dental"}], "en"), [{"specialty": "dental", "name": "Dental",
                                                                                      "name_en": "Dental"}])

    def test_one_active_section_per_name(self):
        with clinic_transaction(self.c.id, self.c.shard):
            with self.assertRaises(IntegrityError), transaction.atomic(using=self.c.shard):
                Section.objects.create(clinic_id=self.c.id, name="Kids", specialty_code="pediatrics")
            Section.objects.create(clinic_id=self.c.id, name="Kids", specialty_code="pediatrics", is_active=False)

    def test_rls_hides_and_refuses_another_places_sections(self):
        with clinic_transaction(self.a.id, "shard_1"):
            self.assertFalse(Section.objects.filter(clinic_id=self.c.id).exists())
            with self.assertRaises(ProgrammingError), transaction.atomic(using="shard_1"):
                Section.objects.create(clinic_id=self.c.id, name="x", specialty_code="dental")
        other = self.clinic("b", "shard_1")
        with clinic_transaction(self.a.id, "shard_1"):
            self.assertEqual(Section.objects.filter(clinic_id__in=[self.a.id, other.id]).count(), 1)

    def test_composite_keys_refuse_another_places_section(self):
        b = self.clinic("b", "shard_1")
        [b_section] = self.sections(b)
        [a_section] = self.sections(self.a)
        with clinic_transaction(self.a.id, "shard_1"):
            doctor = Membership.objects.get(clinic_id=self.a.id, role="doctor")
            service = Service.objects.filter(clinic_id=self.a.id).first()
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                Service.objects.create(clinic_id=self.a.id, section_id=b_section.id, name="x", kind="procedure", price=1)
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                Schedule.objects.create(clinic_id=self.a.id, doctor=doctor, section_id=b_section.id, weekday=1,
                                        starts=dt.time(17), ends=dt.time(22))
            from core import sql
            from scheduling.jobs import system_ctx

            ctx = system_ctx(Clinic.objects.get(id=self.a.id), "shard_1")
            patient = sql.row(ctx, 'INSERT INTO "patients_patient" (clinic_id, file_no, full_name, name_norm, phone, '
                                   "phone_norm, sex, birth_date_estimated, address, notes, allergies, chronic_conditions, "
                                   "created_at, updated_at) VALUES (%s, 1, 'P', 'p', '', '', '', false, '', '', '', '', "
                                   "now(), now()) RETURNING id", [self.a.id])[0]
            insert = ('INSERT INTO "scheduling_appointment" (clinic_id, patient_id, doctor_id, service_id, section_id, '
                      "day, queue_no, is_extra, status, source, fee, is_follow_up, is_new, auto_closed, note, "
                      "cancel_reason, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, '2026-11-10', 1, false, "
                      "'booked', 'clinic', 0, false, true, false, '', '', now(), now())")
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                sql.run(ctx, insert, [self.a.id, patient, doctor.id, service.id, b_section.id])
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                sql.run(ctx, insert.replace("%s, '2026", "NULL, '2026"), [self.a.id, patient, doctor.id, service.id])
            sql.run(ctx, insert, [self.a.id, patient, doctor.id, service.id, a_section.id])

    def test_section_columns_are_not_null_with_composite_keys_on_every_shard(self):
        for alias in ("shard_1", "shard_2"):
            with connections[alias].cursor() as cursor:
                cursor.execute("""
                    SELECT table_name, is_nullable FROM information_schema.columns
                     WHERE column_name = 'section_id' AND table_schema = current_schema()
                       AND table_name NOT LIKE 'clinical%%' ORDER BY table_name""")
                # M3 adds section_id where it may be empty by design: a payment with no visit, a stock item shared
                # by the place (F2), and the money-day's 0 for "no section" (F5; a plain key, no foreign key).
                self.assertEqual(cursor.fetchall(), [("billing_package", "NO"), ("billing_payment", "YES"),
                                                     ("clinics_schedule", "NO"), ("clinics_service", "NO"),
                                                     ("inventory_item", "YES"), ("scheduling_appointment", "NO"),
                                                     ("summaries_money_day", "NO")], alias)
                cursor.execute("""
                    SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint
                     WHERE contype = 'f' AND confrelid = 'clinics_section'::regclass
                       AND conrelid::regclass::text NOT LIKE 'clinical%%' ORDER BY 1""")
                self.assertEqual(cursor.fetchall(), [
                    ("billing_package", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                    ("billing_payment", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                    ("clinics_schedule", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                    ("clinics_service", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                    ("inventory_item", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                    ("scheduling_appointment", "FOREIGN KEY (clinic_id, section_id) REFERENCES clinics_section(clinic_id, id) ON DELETE RESTRICT"),
                ], alias)
                cursor.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'clinics_section'")
                self.assertEqual(cursor.fetchone(), (True,))
                cursor.execute("""SELECT pg_get_indexdef(indexrelid) FROM pg_index
                                   WHERE indrelid = 'clinics_section'::regclass AND indisunique ORDER BY 1""")
                indexes = [r[0] for r in cursor.fetchall()]
                self.assertTrue(any("(clinic_id, id)" in i for i in indexes), indexes)
                self.assertTrue(any("(clinic_id, name) WHERE is_active" in i for i in indexes), indexes)

    def test_the_sql_shape_check_covers_the_section_table_and_columns(self):
        check = ShardSQLCheck()
        run = lambda sql: check(lambda *args: None, sql, [], False, {})  # noqa: E731
        run('SELECT id FROM "clinics_section" WHERE is_active')
        run('UPDATE "scheduling_appointment" SET section_id = 1 WHERE id = 2')
        self.assertEqual(len(check.violations), 2)
        run('SELECT id FROM "clinics_section" WHERE clinic_id = 1 AND is_active')
        run('UPDATE "scheduling_appointment" SET section_id = 1 WHERE clinic_id = 1 AND id = 2')
        self.assertEqual(len(check.violations), 2)


class SectionEndpointTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")
        self.c = self.clinic("c", "shard_2", sections=CENTER)
        with clinic_transaction(self.c.id, self.c.shard):
            self.dental, self.kids, self.skin = Section.objects.filter(clinic_id=self.c.id).order_by("sort", "id")
        add_schedule(self.c, "owner", section=self.dental.id, starts="17:00", ends="22:00")
        add_schedule(self.c, "doctor", section=self.kids.id, starts="10:00", ends="14:00", mode="slots", slot_minutes=20,
                     weekdays=[0, 1, 2])
        add_schedule(self.c, "doctor", section=self.skin.id, starts="16:00", ends="20:00", weekdays=[3, 4])
        self.desk = self.c.client("assistant")

    def test_sections_for_anyone_who_can_book(self):
        body = call(self.desk, "GET", "/api/v1/sections").json()
        self.assertEqual(body["kind"], "center")
        self.assertEqual([(s["id"], s["name"], s["specialty"]) for s in body["items"]],
                         [(self.dental.id, "أسنان", "dental"), (self.kids.id, "Kids", "pediatrics"),
                          (self.skin.id, "جلدية وتجميل", "dermatology")])
        self.assertEqual(body["items"][1]["specialty_name"], "أطفال")
        clinic = call(self.a.client("assistant"), "GET", "/api/v1/sections").json()
        self.assertEqual((clinic["kind"], len(clinic["items"])), ("clinic", 1))

    def test_services_and_doctors_of_a_section(self):
        services = call(self.desk, "GET", f"/api/v1/sections/{self.skin.id}/services").json()
        self.assertEqual(services["section"]["id"], self.skin.id)
        self.assertTrue(services["items"])
        self.assertEqual({s["section_id"] for s in services["items"]}, {self.skin.id})
        self.assertIn("جلسة ليزر", [s["name"] for s in services["items"]])
        doctors = call(self.desk, "GET", f"/api/v1/sections/{self.kids.id}/doctors").json()
        self.assertEqual([d["id"] for d in doctors["items"]], [self.c.membership("doctor").id])
        self.assertEqual([(s["weekday"], s["mode"], s["slot_minutes"]) for s in doctors["items"][0]["sessions"]],
                         [(0, "slots", 20), (1, "slots", 20), (2, "slots", 20)])
        dental = call(self.desk, "GET", f"/api/v1/sections/{self.dental.id}/doctors").json()
        self.assertEqual([d["id"] for d in dental["items"]], [self.c.membership("owner").id])

    def test_another_places_or_an_inactive_section_is_404_and_a_booking_code_is_needed(self):
        with clinic_transaction(self.a.id, self.a.shard):
            a_section = Section.objects.get(clinic_id=self.a.id)
        for path in (f"/api/v1/sections/{a_section.id}/services", f"/api/v1/sections/{a_section.id}/doctors",
                     "/api/v1/sections/999/services", f"/api/v1/sections/{2**63}/doctors"):
            self.assertEqual(call(self.desk, "GET", path).status_code, 404, path)
        with clinic_transaction(self.c.id, self.c.shard):
            Section.objects.filter(clinic_id=self.c.id, id=self.skin.id).update(is_active=False)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/sections/{self.skin.id}/services").status_code, 404)
        self.assertEqual(len(call(self.desk, "GET", "/api/v1/sections").json()["items"]), 2)
        with clinic_transaction(self.c.id, self.c.shard):
            Membership.objects.filter(clinic_id=self.c.id, id=self.c.membership("assistant").id).update(
                revoked=["appointments.book"])
        for path in ("/api/v1/sections", f"/api/v1/sections/{self.kids.id}/services",
                     f"/api/v1/sections/{self.kids.id}/doctors"):
            self.assertEqual(call(self.desk, "GET", path).status_code, 403, path)

    def test_the_clinic_configuration_carries_the_kind_and_sections(self):
        body = call(self.desk, "GET", "/api/v1/clinic").json()
        self.assertEqual(body["clinic"]["kind"], "center")
        self.assertEqual([s["id"] for s in body["sections"]], [self.dental.id, self.kids.id, self.skin.id])
        self.assertEqual({s["section_id"] for s in body["services"]}, {self.dental.id, self.kids.id, self.skin.id})
        owner = next(d for d in body["doctors"] if d["id"] == self.c.membership("owner").id)
        self.assertEqual({s["section_id"] for s in owner["today"]}, {self.dental.id})
