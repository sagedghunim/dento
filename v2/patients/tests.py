import json

from django.db import connections
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from audit.models import AuditAccess, AuditChange
from clinics.models import Clinic
from core import sql
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.jobs import system_ctx
from scheduling.testing import M1TestCase


def access_events(fixture, patient_id=None):
    with clinic_transaction(fixture.id, fixture.shard):
        rows = AuditAccess.objects.filter(clinic_id=fixture.id)
        if patient_id:
            rows = rows.filter(patient_id=patient_id)
        return list(rows.order_by("at", "id").values_list("patient_id", "action"))


def bulk_patients(fixture, count, name="Bulk Person"):
    with clinic_transaction(fixture.id, fixture.shard):
        ctx = system_ctx(Clinic.objects.get(id=fixture.id), fixture.shard)
        start = sql.row(ctx, 'SELECT coalesce(max(file_no), 0) FROM "patients_patient" WHERE clinic_id = %s', [fixture.id])[0]
        sql.run(ctx, """
            INSERT INTO "patients_patient" (clinic_id, file_no, full_name, name_norm, phone, phone_norm, sex,
                birth_date_estimated, address, notes, allergies, chronic_conditions, created_at, updated_at)
            SELECT %s, g, %s || ' ' || g, lower(%s) || ' ' || md5(g::text), '', '', '', false, '', '', '', '', now(), now()
              FROM generate_series(%s, %s) g""", [fixture.id, name, name, start + 1, start + count])
        sql.run(ctx, "UPDATE \"clinics_counter\" SET value = value + %s WHERE clinic_id = %s AND name = 'patient_file'",
                [count, fixture.id])


class PatientTests(M1TestCase):
    def create(self, client=None, **fields):
        body = {"full_name": "أحمد محمود علي", "phone": "+20 101 234 5678", "sex": "m", "age": 40, **fields}
        return call(client or self.desk, "POST", "/api/v1/patients", body)

    def test_create_normalises_and_numbers(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["patient"]["file_no"], 1)
        patient = call(self.desk, "GET", f"/api/v1/patients/{r.json()['patient']['id']}").json()["patient"]
        self.assertEqual((patient["file_no"], patient["phone"], patient["age"], patient["birth_date_estimated"]),
                         (1, "01012345678", 40, True))
        self.assertEqual(self.create(full_name="Mona Samir Ali", phone="01099990001").json()["patient"]["file_no"], 2)
        # A new file needs a three-part name, an Egyptian mobile, the sex and an age (Saged, 2026-11-17).
        for bad in ({"full_name": ""}, {"full_name": "Mona Ali"}, {"phone": "12345"}, {"phone": ""},
                    {"phone": "0221234567"}, {"phone": "+44 7700 900123"}, {"sex": "x"}, {"sex": ""}, {"age": 200},
                    {"age": None}, {"birth_date": "3000-01-01", "age": None}, {"notes": "x" * 2001},
                    {"phone_owner": "guardian"}, {"phone_owner": "guardian", "guardian_relation": "aunt"},
                    {"phone_owner": "friend"}):
            self.assertEqual(self.create(**bad).status_code, 400, bad)

    def test_search_is_arabic_aware_by_any_word_prefix_phone_or_file_number(self):
        ids = {}
        for name, phone in (("أحمد محمود علي", "01012345678"), ("فاطمة الزهراء حسن", "01112223333"),
                            ("محمد إبراهيم سعيد", "01234567890"), ("Omar 100%_x Zed", "01500000001")):
            ids[name] = self.create(full_name=name, phone=phone).json()["patient"]["id"]

        def found(q):
            r = call(self.desk, "GET", f"/api/v1/patients?q={q}")
            self.assertEqual(r.status_code, 200, r.content)
            return [item["id"] for item in r.json()["items"]]

        self.assertEqual(found("احمد"), [ids["أحمد محمود علي"]])  # alef forms
        self.assertEqual(found("فاطمه"), [ids["فاطمة الزهراء حسن"]])  # taa marbuta
        self.assertEqual(found("ابراه"), [ids["محمد إبراهيم سعيد"]])  # a later word
        self.assertEqual(set(found("محم")), {ids["أحمد محمود علي"], ids["محمد إبراهيم سعيد"]})
        self.assertEqual(found("٠١١١٢"), [ids["فاطمة الزهراء حسن"]])  # Arabic-Indic digits, phone prefix
        self.assertEqual(found("+2011122"), [ids["فاطمة الزهراء حسن"]])
        self.assertEqual(found("3"), [])  # too short
        self.assertEqual(found("1"), [])
        self.assertIn(ids["محمد إبراهيم سعيد"], found("03"))  # file number 3 is exact
        self.assertEqual(found("100_"), [])  # LIKE wildcards are escaped: _ is not "any character"
        self.assertEqual(found("1%25%25"), [])
        self.assertEqual(found("100%25"), [ids["Omar 100%_x Zed"]])
        self.assertEqual(found("100%25_"), [ids["Omar 100%_x Zed"]])
        item = call(self.desk, "GET", "/api/v1/patients?q=احمد").json()["items"][0]
        self.assertEqual((item["age"], item["sex"], item["today"], item["last_visit"]), (40, "m", None, None))

    def test_search_shows_todays_booking_and_never_another_clinic(self):
        appt = self.book(new_patient=new_patient(full_name="Karim Adel")).json()["appointment"]
        self.book(fixture=self.b, new_patient=new_patient(full_name="Karim Other"))
        items = call(self.desk, "GET", "/api/v1/patients?q=karim").json()["items"]
        self.assertEqual([i["id"] for i in items], [appt["patient"]["id"]])
        self.assertEqual(items[0]["today"]["queue_no"], 1)
        self.assertEqual(access_events(self.a), [])  # search writes no access event

    def test_by_phone_lists_this_clinics_files_with_that_phone(self):
        first = self.create().json()["patient"]["id"]
        second = self.create(full_name="Son Ahmed Mahmoud", phone_owner="guardian", guardian_relation="father",
                             age=8).json()["patient"]["id"]
        self.book(fixture=self.b, new_patient=new_patient(full_name="Elsewhere", phone="01012345678"))
        answer = call(self.desk, "GET", "/api/v1/patients/by-phone?phone=0020 1012345678").json()
        self.assertEqual([(i["id"], i["phone_owner"], i["guardian_relation"], i["age"]) for i in answer["items"]],
                         [(first, "self", "", 40), (second, "guardian", "father", 8)])
        self.assertTrue(answer["taken"])
        self.assertEqual(call(self.desk, "GET", "/api/v1/patients/by-phone?phone=").status_code, 400)

    def test_opening_a_file_and_its_lists_writes_one_access_event_each(self):
        appt = self.book(payment={"amount": 1000, "method": "cash"}).json()["appointment"]
        pid = appt["patient"]["id"]
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{pid}").status_code, 200)
        visits = call(self.desk, "GET", f"/api/v1/patients/{pid}/visits").json()
        payments = call(self.desk, "GET", f"/api/v1/patients/{pid}/payments").json()
        self.assertEqual([v["id"] for v in visits["items"]], [appt["id"]])
        self.assertEqual([p["amount"] for p in payments["items"]], [1000])
        self.assertEqual(access_events(self.a, pid), [(pid, 1), (pid, 7), (pid, 7)])

    def test_lists_page_with_a_cursor(self):
        import datetime as dt

        from scheduling.testing import today

        pid = self.book().json()["appointment"]["patient"]["id"]
        days = [(today(self.a) + dt.timedelta(days=i)).isoformat() for i in range(5)]
        for day in days[1:]:  # one open visit a day with a doctor (Saged, 2026-11-17)
            self.book(patient_id=pid, day=day)
        first = call(self.desk, "GET", f"/api/v1/patients/{pid}/visits?limit=3").json()
        self.assertEqual(len(first["items"]), 3)
        rest = call(self.desk, "GET", f"/api/v1/patients/{pid}/visits?limit=3&after={first['next']}").json()
        self.assertEqual((len(rest["items"]), rest["next"]), (2, None))
        self.assertEqual([v["day"] for v in first["items"] + rest["items"]], days[::-1])
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{pid}/visits?limit=101").status_code, 400)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{pid}/visits?after=junk").status_code, 400)

    def test_edits_send_the_values_they_replace_and_identity_edits_are_audited(self):
        patient = self.create().json()["patient"]
        url = f"/api/v1/patients/{patient['id']}"
        r = call(self.desk, "PATCH", url, {"changes": {"full_name": "Ahmed M. Ali"}, "expected": {"full_name": "wrong"}})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))
        r = call(self.desk, "PATCH", url, {"changes": {"full_name": "Ahmed M. Ali", "address": "Giza"},
                                           "expected": {"full_name": patient["full_name"], "address": ""}})
        self.assertEqual((r.status_code, r.json()["patient"]["full_name"]), (200, "Ahmed M. Ali"))
        with clinic_transaction(self.a.id, self.a.shard):
            change = AuditChange.objects.get(clinic_id=self.a.id, action="patient.identity")
        self.assertEqual(change.details, {"before": {"full_name": patient["full_name"]}, "after": {"full_name": "Ahmed M. Ali"}})
        self.assertEqual(call(self.desk, "PATCH", url, {"changes": {"made_up": 1}}).status_code, 400)
        # Notes, allergies and chronic conditions are the doctor's once the file exists (Saged, 2026-11-22).
        for name in ("notes", "allergies", "chronic_conditions"):
            self.assertEqual(call(self.desk, "PATCH", url, {"changes": {name: "x"}, "expected": {name: ""}}).status_code,
                             403, name)
        self.assertEqual(call(self.desk, "GET", "/api/v1/patients?q=ahmed").json()["items"][0]["name"], "Ahmed M. Ali")

    def test_another_clinics_file_is_404_everywhere(self):
        foreign = self.book(fixture=self.b).json()["appointment"]["patient"]["id"]
        for method, path in (("GET", ""), ("GET", "/visits"), ("GET", "/payments"), ("PATCH", "")):
            r = call(self.desk, method, f"/api/v1/patients/{foreign}{path}",
                     {"changes": {"address": "x"}} if method == "PATCH" else None)
            self.assertEqual(r.status_code, 404, (method, path))
        self.assertEqual(access_events(self.a), [])
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{2**64}").status_code, 404)

    def test_permissions(self):
        from clinics.models import Membership

        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["patients.view", "patients.edit"])
        pid = self.create(client=self.owner).json()["patient"]["id"]
        for method, url in (("GET", "/api/v1/patients?q=ahmed"), ("GET", "/api/v1/patients/by-phone?phone=01012345678"),
                            ("GET", f"/api/v1/patients/{pid}"), ("POST", "/api/v1/patients")):
            self.assertEqual(call(self.desk, method, url, {"full_name": "x"}).status_code, 403, url)

    def test_search_runs_the_same_statements_for_10_or_10000_files(self):
        def count(q):
            with CaptureQueriesContext(connections[self.a.shard]) as shard:
                self.assertEqual(call(self.desk, "GET", f"/api/v1/patients?q={q}").status_code, 200)
            return len([x for x in shard.captured_queries if "SAVEPOINT" not in x["sql"]])

        def counts():
            # A full prefix page, a word match, nothing at all, a phone prefix.
            return count("bulk"), count("person"), count("zzz"), count("0109999")

        bulk_patients(self.a, 25)
        small = counts()
        bulk_patients(self.a, 10000)
        self.assertEqual(counts(), small)
        # set_config, membership, SET LOCAL; then 1-2 searches; then the details of what was found, if anything;
        # then the timeout put back, so it doesn't outlive the search (chief judge M1, R1).
        self.assertEqual(small, (6, 7, 6, 5))  # the day is the clock's: no read of the register (Saged, 2026-11-24)


class SearchPlanTests(TransactionTestCase):
    """Design 5.7 and 10: both search queries keep to the clinic's part of the index under RLS, 100,000 files."""

    databases = "__all__"

    def test_plans_for_a_100000_file_clinic(self):
        helper = ShardTestCase()
        a = helper.clinic("plan", "shard_1")
        bulk_patients(a, 100_000)
        with connections[a.shard].cursor() as cursor:
            cursor.execute('VACUUM ANALYZE "patients_patient"')
        with clinic_transaction(a.id, a.shard):
            ctx = system_ctx(Clinic.objects.get(id=a.id), a.shard)

            def plan(statement, params):
                found = sql.row(ctx, "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)[0]
                return found[0] if isinstance(found, list) else json.loads(found)[0]

            prefix = plan('SELECT id, full_name, phone, file_no FROM "patients_patient" '
                          "WHERE clinic_id = %s AND name_norm >= %s AND name_norm < %s ORDER BY name_norm LIMIT 20",
                          [a.id, "bulk person 1", "bulk person 2"])
            word = plan('SELECT id, full_name, phone, file_no FROM "patients_patient" WHERE clinic_id = %s '
                        "AND name_norm LIKE %s ESCAPE '\\' AND NOT (id = ANY(%s::bigint[])) ORDER BY name_norm LIMIT 20",
                        [a.id, "% zzz%", []])
        for found, name in ((prefix, "prefix"), (word, "word")):
            text = json.dumps(found["Plan"])
            self.assertIn("Index Only Scan", text, name)
            self.assertIn("patient_name_search", text, name)
        self.assertIn("name_norm >=", json.dumps(prefix["Plan"]))  # the range is an index condition, not a filter
        self.assertLess(word["Execution Time"], 300, "the word search must fit its 300 ms timeout")
