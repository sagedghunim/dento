"""A new patient's number rules and search with a slip (Saged, 2026-11-17). The two-desk race is in
scheduling.test_concurrency."""

from clinics.models import Clinic
from core import sql
from core.routing import clinic_transaction
from core.testing import call, new_patient
from core.text import edit_distance, name_skeleton, normalize_name
from scheduling.jobs import system_ctx
from scheduling.testing import M1TestCase


class NumberRuleTests(M1TestCase):
    PHONE = "01012345678"

    def create(self, client=None, expect=201, **fields):
        r = call(client or self.desk, "POST", "/api/v1/patients", new_patient(**{"phone": self.PHONE, **fields}))
        if expect is not None:
            self.assertEqual(r.status_code, expect, r.content)
        return r

    def test_one_own_number_one_file(self):
        mona = self.create(full_name="Mona Samir Ali").json()["patient"]
        r = self.create(full_name="Sara Samir Ali", phone="+20 10 1234 5678", expect=409)
        error = r.json()["error"]
        self.assertEqual((error["code"], error["patient"]["id"]), ("phone_taken", mona["id"]))
        self.assertIn("Mona Samir Ali", error["message"])
        # Through Book and Walk-in too, with nothing written.
        r = self.book(expect=409, new_patient=new_patient("Sara Samir Ali", phone=self.PHONE))
        self.assertEqual(r.json()["error"]["code"], "phone_taken")
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", {"new_patient": new_patient("Sara Samir Ali", phone=self.PHONE)})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "phone_taken"))
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/by-phone?phone={self.PHONE}").json()["items"][0]["id"],
                         mona["id"])

    def test_a_guardians_number_is_shared_by_siblings(self):
        father = self.create(full_name="Samir Ali Hassan", age=40).json()["patient"]
        kids = [self.create(full_name=name, age=6, phone_owner="guardian", guardian_relation="father").json()["patient"]
                for name in ("Omar Samir Ali", "Laila Samir Ali")]
        answer = call(self.desk, "GET", f"/api/v1/patients/by-phone?phone={self.PHONE}").json()
        self.assertEqual([i["id"] for i in answer["items"]], [father["id"], *(k["id"] for k in kids)])
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{kids[0]['id']}").json()["patient"]["guardian_relation"],
                         "father")
        # The number stays the father's own: no second "own" file, and no second Omar on it.
        self.assertEqual(self.create(full_name="Hany Samir Ali", expect=409).json()["error"]["code"], "phone_taken")
        r = self.create(full_name="omar  samir ALI", phone_owner="guardian", guardian_relation="mother", expect=409)
        self.assertEqual((r.json()["error"]["code"], r.json()["error"]["patient"]["id"]), ("duplicate_patient", kids[0]["id"]))

    def test_the_same_name_on_the_same_number_is_always_a_duplicate(self):
        first = self.create(full_name="Nour Adel Fahmy", phone_owner="guardian", guardian_relation="mother").json()["patient"]
        # Spelling forms fold: the same person.
        r = self.create(full_name="نور عادل فهمي", phone_owner="guardian", guardian_relation="mother")
        self.assertEqual(r.status_code, 201)  # another script is another name
        r = self.create(full_name="Nour Adel Fahmy", phone_owner="guardian", guardian_relation="other", expect=409)
        self.assertEqual((r.json()["error"]["code"], r.json()["error"]["patient"]["id"]), ("duplicate_patient", first["id"]))

    def test_an_edit_follows_the_same_rules(self):
        own = self.create(full_name="Mona Samir Ali").json()["patient"]
        other = self.create(full_name="Hoda Samir Ali", phone="01099990002").json()["patient"]
        url = f"/api/v1/patients/{other['id']}"
        r = call(self.desk, "PATCH", url, {"changes": {"phone": self.PHONE}, "expected": {"phone": "01099990002"}})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "phone_taken"))
        r = call(self.desk, "PATCH", url, {"changes": {"phone": self.PHONE, "phone_owner": "guardian",
                                                       "guardian_relation": "mother"}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        r = call(self.desk, "PATCH", url, {"changes": {"phone_owner": "self"}, "expected": {}})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "phone_taken"))
        # Its own file's edit never trips on itself.
        r = call(self.desk, "PATCH", f"/api/v1/patients/{own['id']}", {"changes": {"full_name": "Mona Samir Aly"},
                                                                       "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        for bad in ({"full_name": "Mona"}, {"phone": ""}, {"sex": ""}, {"age": ""}):
            r = call(self.desk, "PATCH", url, {"changes": bad, "expected": {}})
            self.assertEqual(r.status_code, 400, bad)

    def test_a_doctor_is_refused_without_seeing_another_doctors_patient(self):
        self.create(full_name="Mona Samir Ali")
        doctor = self.a.client("doctor")
        r = self.create(client=doctor, full_name="Sara Samir Ali", expect=409)
        self.assertEqual((r.json()["error"]["code"], r.json()["error"]["patient"]), ("phone_taken", None))
        self.assertNotIn("Mona", r.json()["error"]["message"])
        answer = call(doctor, "GET", f"/api/v1/patients/by-phone?phone={self.PHONE}").json()
        self.assertEqual((answer["items"], answer["taken"]), ([], True))

    def test_another_clinics_files_never_count(self):
        self.book(fixture=self.b, new_patient=new_patient("Mona Samir Ali", phone=self.PHONE))
        self.create(full_name="Mona Samir Ali")


class SlipSearchTests(M1TestCase):
    def found(self, q):
        r = call(self.desk, "GET", f"/api/v1/patients?q={q}")
        self.assertEqual(r.status_code, 200, r.content)
        return [item["name"] for item in r.json()["items"]]

    def test_the_skeleton_in_the_database_is_pythons(self):
        names = ["إبراهيم عبد الله محمود", "Mohammed Ahmed Aly", "فاطمة الزهراء حسنى", "مصطفي  عبدالرحمن  سعد"]
        for name in names:
            self.book(new_patient=new_patient(name))
        with clinic_transaction(self.a.id, self.a.shard):
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            stored = dict(sql.rows(ctx, 'SELECT full_name, name_skel FROM "patients_patient" WHERE clinic_id = %s',
                                   [self.a.id]))
        self.assertEqual(stored, {" ".join(n.split()): name_skeleton(normalize_name(n)) for n in names})

    def test_a_missing_extra_or_wrong_letter_still_finds_the_file(self):
        for name in ("إبراهيم عبد الله محمود", "Mohammed Ahmed Aly", "يوسف كمال الدين", "Karim Adel Fahmy"):
            self.book(new_patient=new_patient(name))
        self.assertEqual(self.found("ابرهيم"), ["إبراهيم عبد الله محمود"])        # a missing alef
        self.assertEqual(self.found("ابراهيم عبدالله"), ["إبراهيم عبد الله محمود"])  # a space less
        self.assertEqual(self.found("mohamed ahmad"), ["Mohammed Ahmed Aly"])     # vowels and a doubled letter
        self.assertEqual(self.found("يوسف كمار"), ["يوسف كمال الدين"])            # a wrong letter
        self.assertEqual(self.found("karrim adl"), ["Karim Adel Fahmy"])          # an extra letter
        self.assertEqual(self.found("kxyz"), [])

    def test_edit_distance(self):
        self.assertEqual(edit_distance("abc", "abc"), 0)
        self.assertEqual(edit_distance("abc", "acb"), 1)  # a swap
        self.assertEqual(edit_distance("abc", "abxc"), 1)
        self.assertEqual(edit_distance("abcdef", "a", limit=2), 3)
