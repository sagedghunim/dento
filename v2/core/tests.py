import datetime as dt
import types

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.db.utils import ProgrammingError
from django.test import SimpleTestCase

from core.days import business_day, next_rollover
from core.errors import ApiError
from core.numbers import parse_int, parse_money
from core.routing import NoClinicContext, clinic_scope, clinic_transaction, id_range, set_clinic_setting
from core.testing import ShardTestCase
from core.text import ascii_digits, is_valid_phone, looks_like_phone, normalize_name, normalize_phone


class NormalizeNameTests(SimpleTestCase):
    def test_alef_forms_taa_marbuta_and_alef_maqsura_match(self):
        self.assertEqual(normalize_name("أحمد"), normalize_name("احمد"))
        self.assertEqual(normalize_name("إيمان"), normalize_name("ايمان"))
        self.assertEqual(normalize_name("آمال"), normalize_name("امال"))
        self.assertEqual(normalize_name("فاطمة"), normalize_name("فاطمه"))
        self.assertEqual(normalize_name("مصطفى"), normalize_name("مصطفي"))

    def test_diacritics_tatweel_and_spaces_are_removed(self):
        self.assertEqual(normalize_name("  مُحَمَّد   عــلي "), "محمد علي")

    def test_latin_names_fold_case(self):
        self.assertEqual(normalize_name("  Omar   KHALIL "), "omar khalil")

    def test_arabic_digits(self):
        self.assertEqual(ascii_digits("٠١٢٣٤٥٦٧٨٩"), "0123456789")


class PhoneTests(SimpleTestCase):
    def test_egyptian_forms_become_national(self):
        for raw in ["01012345678", "+201012345678", "00201012345678", "201012345678", "1012345678", "٠١٠١٢٣٤٥٦٧٨", "010 1234 5678"]:
            self.assertEqual(normalize_phone(raw), "01012345678", raw)

    def test_validity(self):
        self.assertTrue(is_valid_phone("01012345678"))
        self.assertFalse(is_valid_phone("01312345678"))
        self.assertFalse(is_valid_phone("0101234567"))
        self.assertTrue(is_valid_phone("966501234567"))
        self.assertFalse(is_valid_phone(""))

    def test_looks_like_phone(self):
        self.assertTrue(looks_like_phone("010 123"))
        self.assertTrue(looks_like_phone("٠١٠"))
        self.assertFalse(looks_like_phone("محمد"))
        self.assertFalse(looks_like_phone("  "))


class NumberParserTests(SimpleTestCase):
    def test_whole_numbers(self):
        self.assertEqual(parse_int("١٥", "qty", 1, 100), 15)
        self.assertEqual(parse_int(7, "qty", 1, 100), 7)
        for bad in [True, "1.5", "abc", None, 0, 101]:
            with self.assertRaises(ApiError, msg=repr(bad)):
                parse_int(bad, "qty", 1, 100)

    def test_money_by_string_arithmetic(self):
        self.assertEqual(parse_money("150", "fee"), 15000)
        self.assertEqual(parse_money("150.5", "fee"), 15050)
        self.assertEqual(parse_money("١٥٠٫٧٥", "fee"), 15075)
        self.assertEqual(parse_money("0.1", "fee"), 10)
        self.assertEqual(parse_money(30000, "fee"), 30000)

    def test_money_refuses_ambiguous_and_bad_input(self):
        for bad in ["1,500", "1.234", "-5", "", "12a"]:
            with self.assertRaises(ApiError, msg=bad):
                parse_money(bad, "fee")


class BusinessDayTests(SimpleTestCase):
    clinic = types.SimpleNamespace(timezone="Africa/Cairo", day_ends_at=dt.time(4, 0))

    def at(self, text):
        return dt.datetime.fromisoformat(text)

    def test_after_midnight_belongs_to_the_evening_session(self):
        # 00:30 Cairo on 16 Jan is still the business day of 15 Jan.
        self.assertEqual(business_day(self.clinic, self.at("2026-01-15T22:30:00+00:00")), dt.date(2026, 1, 15))
        self.assertEqual(business_day(self.clinic, self.at("2026-01-16T01:59:00+00:00")), dt.date(2026, 1, 15))
        self.assertEqual(business_day(self.clinic, self.at("2026-01-16T02:00:00+00:00")), dt.date(2026, 1, 16))

    def test_summer_time(self):
        # Egypt is UTC+3 in summer: 03:59 local on 16 July is 00:59 UTC.
        self.assertEqual(business_day(self.clinic, self.at("2026-07-16T00:59:00+00:00")), dt.date(2026, 7, 15))
        self.assertEqual(business_day(self.clinic, self.at("2026-07-16T01:00:00+00:00")), dt.date(2026, 7, 16))

    def test_rollover(self):
        self.assertEqual(next_rollover(self.clinic, self.at("2026-07-15T18:00:00+00:00")),
                         dt.datetime(2026, 7, 16, 1, 0, tzinfo=dt.UTC))


class RoutingAndRlsTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")
        self.b = self.clinic("b", "shard_1")
        self.c = self.clinic("c", "shard_2")

    def test_tenant_query_without_context_raises(self):
        from clinics.models import Clinic

        with self.assertRaises(NoClinicContext):
            list(Clinic.objects.filter(id=self.a.id))

    def test_clinics_live_on_their_shards(self):
        from clinics.models import Clinic

        with clinic_transaction(self.c.id, "shard_2"):
            self.assertTrue(Clinic.objects.filter(id=self.c.id).exists())
        with clinic_transaction(self.c.id, "shard_1"):
            self.assertFalse(Clinic.objects.filter(id=self.c.id).exists())

    def test_rls_shows_only_the_current_clinic(self):
        from clinics.models import Membership, Service

        with clinic_transaction(self.a.id, "shard_1"):
            self.assertEqual(set(Membership.objects.filter(clinic_id__in=[self.a.id, self.b.id]).values_list("clinic_id", flat=True)), {self.a.id})
            # Another clinic's id in WHERE finds nothing.
            self.assertFalse(Service.objects.filter(clinic_id=self.b.id).exists())

    def test_no_setting_means_no_rows(self):
        from clinics.models import Service

        with clinic_transaction(self.a.id, "shard_1"):
            set_clinic_setting("shard_1", None)  # the reused-connection case: '' rather than NULL
            self.assertFalse(Service.objects.filter(clinic_id=self.a.id).exists())

    def test_insert_for_another_clinic_is_refused(self):
        from clinics.models import Service

        with clinic_transaction(self.a.id, "shard_1"):
            with self.assertRaises(ProgrammingError), transaction.atomic(using="shard_1"):
                Service.objects.create(clinic_id=self.b.id, name="x", kind="procedure", price=1)

    def test_composite_foreign_key_refuses_another_clinics_doctor(self):
        from clinics.models import Membership, Schedule

        with clinic_transaction(self.b.id, "shard_1"):
            other_doctor = Membership.objects.get(clinic_id=self.b.id, role="doctor")
        with clinic_transaction(self.a.id, "shard_1"):
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                Schedule.objects.create(clinic_id=self.a.id, doctor_id=other_doctor.id, weekday=1,
                                        starts=dt.time(17), ends=dt.time(22))

    def test_every_shard_table_with_clinic_id_is_forced_and_policied(self):
        for alias in settings.SHARDS:
            with connections[alias].cursor() as cursor:
                cursor.execute("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'clinic_isolation')
                    FROM pg_class c
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'clinic_id' AND NOT a.attisdropped
                    WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')""")
                rows = cursor.fetchall()
            self.assertTrue(rows)
            bad = [name for name, enabled, forced, policy in rows if not (enabled and forced and policy)]
            self.assertEqual(bad, [], alias)

    def test_sequences_draw_from_the_shards_range(self):
        for alias in settings.SHARDS:
            low, high = id_range(alias)
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT sequencename, min_value, max_value FROM pg_sequences WHERE schemaname = current_schema()")
                rows = cursor.fetchall()
            self.assertTrue(rows)
            self.assertEqual([r[0] for r in rows if (r[1], r[2]) != (low, high)], [], alias)
        with clinic_transaction(self.c.id, "shard_2"):
            from clinics.models import Membership

            ids = list(Membership.objects.filter(clinic_id=self.c.id).values_list("id", flat=True))
        low, high = id_range("shard_2")
        self.assertTrue(all(low <= i <= high for i in ids))
