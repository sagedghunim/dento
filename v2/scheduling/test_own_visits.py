"""A doctor's own patients and visits (Saged, 2026-11-13) and the walk-in (design G5)."""

import uuid

from core.testing import call

from .testing import M1TestCase


class OwnPatientsTests(M1TestCase):
    """A doctor never sees or acts on another doctor's patients or visits: they are 404, as another place's are
    (Saged, 2026-11-13). The owner works a queue, the doctor works slots."""

    def setUp(self):
        super().setUp()
        self.doctor = self.a.client("doctor")
        self.doctor_id = self.a.membership("doctor").id
        other = self.book().json()["appointment"]  # the owner's visit and patient, "Mohamed Ahmed"
        self.owner_visit, self.owner_patient = other["id"], other["patient"]["id"]
        self.act(self.owner_visit, "arrive", "booked")
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in",
                 {"doctor_id": self.doctor_id, "new_patient": {"full_name": "Own Patient", "phone": "01099999999"}})
        self.assertEqual(r.status_code, 201, r.content)
        self.own_visit, self.own_patient = r.json()["appointment"]["id"], r.json()["appointment"]["patient"]["id"]

    def get(self, url, client=None):
        return call(client or self.doctor, "GET", url)

    def test_another_doctors_visit_is_404_for_every_action(self):
        for action, status in (("call", "waiting"), ("cancel", "waiting"), ("undo-arrive", "waiting")):
            r = self.act(self.owner_visit, action, status, client=self.doctor)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"), action)
        self.assertEqual(self.appointment(self.owner_visit).status, "waiting")
        for url in (f"/api/v1/appointments/{self.owner_visit}", f"/api/v1/appointments/{self.owner_visit}/record"):
            self.assertEqual(self.get(url).status_code, 404, url)
        r = self.doctor.generic("PUT", f"/api/v1/appointments/{self.owner_visit}/note",
                                '{"complaint": "x"}', content_type="application/json",
                                headers={"X-Membership": str(self.doctor.membership_id),
                                         "Idempotency-Key": str(uuid.uuid4()), "If-Match": "0"})
        self.assertEqual(r.status_code, 404)
        r = call(self.doctor, "POST", f"/api/v1/appointments/{self.owner_visit}/payments", {"amount": "10", "method": "cash"})
        self.assertEqual(r.status_code, 404)
        # Their own visit works as before.
        self.assertEqual(self.act(self.own_visit, "call", "waiting", client=self.doctor).status_code, 200)

    def test_today_shows_only_their_own_visits(self):
        body = self.get("/api/v1/today").json()
        self.assertTrue(body["own_only"])
        self.assertEqual([q["doctor"]["id"] for q in body["queues"]], [self.doctor_id])
        self.assertEqual([a["id"] for q in body["queues"] for a in q["appointments"]], [self.own_visit])
        self.assertEqual({e["appointment_id"] for e in body["recent"]}, {self.own_visit})
        desk = self.get("/api/v1/today", self.desk).json()
        self.assertFalse(desk["own_only"])
        self.assertEqual({a["id"] for q in desk["queues"] for a in q["appointments"]}, {self.owner_visit, self.own_visit})

    def test_patients_search_finds_only_their_own(self):
        self.assertEqual(self.get("/api/v1/patients?q=Mohamed").json()["items"], [])
        self.assertEqual(self.get("/api/v1/patients?q=0101234").json()["items"], [])
        self.assertEqual(self.get("/api/v1/patients/by-phone?phone=01012345678").json()["items"], [])
        self.assertEqual([i["id"] for i in self.get("/api/v1/patients?q=Own").json()["items"]], [self.own_patient])
        self.assertEqual([i["id"] for i in self.get("/api/v1/patients?q=Mohamed", self.desk).json()["items"]],
                         [self.owner_patient])

    def test_another_doctors_patient_is_404_everywhere(self):
        p = self.owner_patient
        for url in (f"/api/v1/patients/{p}", f"/api/v1/patients/{p}/visits", f"/api/v1/patients/{p}/payments",
                    f"/api/v1/patients/{p}/timeline", f"/api/v1/patients/{p}/prescriptions",
                    f"/api/v1/patients/{p}/attachments", f"/api/v1/patients/{p}/teeth", f"/api/v1/patients/{p}/plans",
                    f"/api/v1/patients/{p}/balance"):
            self.assertEqual(self.get(url).status_code, 404, url)
            self.assertEqual(self.get(url, self.owner).status_code, 200, url)
        r = call(self.doctor, "PATCH", f"/api/v1/patients/{p}", {"changes": {"notes": "x"}, "expected": {}})
        self.assertEqual(r.status_code, 404)
        r = call(self.doctor, "PUT", f"/api/v1/patients/{p}/teeth/16", {"condition": "caries", "surfaces": ["O"]})
        self.assertEqual(r.status_code, 404)
        for url in (f"/patients/{p}", f"/patients/{p}/teeth", f"/visits/{self.owner_visit}/teeth"):
            self.assertEqual(self.doctor.get(url).status_code, 404, url)
        self.assertEqual(self.doctor.get(f"/patients/{self.own_patient}").status_code, 200)

    def test_a_shared_patient_shows_only_their_own_visits(self):
        day = self.appointment(self.owner_visit).day
        visit = self.book(patient_id=self.owner_patient, doctor_id=self.doctor_id,
                          starts_at=self.slot_iso(self.a, day, "13:40")).json()["appointment"]
        self.assertEqual(self.get(f"/api/v1/patients/{self.owner_patient}").status_code, 200)
        visits = self.get(f"/api/v1/patients/{self.owner_patient}/visits").json()["items"]
        self.assertEqual([v["id"] for v in visits], [visit["id"]])

    def test_a_file_they_created_is_theirs(self):
        r = call(self.doctor, "POST", "/api/v1/patients", {"full_name": "Created Here", "phone": "01055555555"})
        self.assertEqual(r.status_code, 201, r.content)
        created = r.json()["patient"]["id"]
        self.assertEqual([i["id"] for i in self.get("/api/v1/patients?q=Created").json()["items"]], [created])
        self.assertEqual(self.get(f"/api/v1/patients/{created}").status_code, 200)

    def test_a_doctor_books_only_for_themselves_and_their_patients(self):
        r = self.book(client=self.doctor, expect=404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        day = self.appointment(self.owner_visit).day
        r = self.book(client=self.doctor, expect=404, doctor_id=self.doctor_id, patient_id=self.owner_patient,
                      starts_at=self.slot_iso(self.a, day, "13:40"))
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_the_owner_and_the_desk_are_unchanged(self):
        self.assertEqual(self.get(f"/api/v1/patients/{self.own_patient}", self.owner).status_code, 200)
        self.assertEqual(self.act(self.own_visit, "call", "waiting", client=self.owner).status_code, 200)
        self.assertEqual(self.act(self.owner_visit, "call", "waiting").status_code, 200)  # the assistant

    def test_a_stored_retired_grant_changes_nothing(self):
        from clinics.models import Membership
        from core.routing import clinic_transaction

        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.doctor_id).update(granted=["appointments.others"])
        doctor = self.a.client("doctor")
        self.assertEqual(self.act(self.owner_visit, "call", "waiting", client=doctor).status_code, 404)
        team = call(self.owner, "GET", "/api/v1/team").json()
        member = next(m for m in team["members"] if m["id"] == self.doctor_id)
        self.assertNotIn("appointments.others", member["codes"])
        self.assertTrue(member["editable"])
        r = call(self.owner, "PUT", f"/api/v1/team/members/{self.doctor_id}/permissions",
                 {"version": member["version"], "codes": {"appointments.others": True}})
        self.assertEqual(r.status_code, 400)
        r = call(self.owner, "PUT", f"/api/v1/team/members/{self.doctor_id}/permissions",
                 {"version": member["version"], "codes": {"billing.discount": True}})
        self.assertEqual(r.status_code, 200, r.content)


class WalkInTests(M1TestCase):
    def walk_in(self, client=None, key=None, **body):
        body.setdefault("new_patient", {"full_name": "Walk In", "phone": "01012345678", "sex": "f", "age": 40})
        return call(client or self.desk, "POST", "/api/v1/appointments/walk-in", body, key=key)

    def test_defaults_give_the_next_turn_already_waiting(self):
        first = self.walk_in()
        self.assertEqual(first.status_code, 201, first.content)
        body = first.json()
        appointment = body["appointment"]
        self.assertEqual(appointment["status"], "waiting")
        self.assertIsNone(appointment["starts_at"])
        self.assertEqual(appointment["service"]["id"], self.services_a["consultation"].id)
        self.assertEqual((body["turn"], body["ahead"]), (appointment["queue_no"], 0))
        # The owner now has one waiting, the doctor none: the next walk-in goes to the doctor, with no time,
        # although the doctor works slots.
        second = self.walk_in().json()
        self.assertEqual(second["appointment"]["doctor_id"], self.a.membership("doctor").id)
        self.assertIsNone(second["appointment"]["starts_at"])
        third = self.walk_in(doctor_id=self.a.membership("doctor").id).json()
        self.assertEqual(third["ahead"], 1)

    def test_a_repeat_with_one_key_books_once(self):
        key = uuid.uuid4()
        first = self.walk_in(key=key)
        second = self.walk_in(key=key)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()["appointment"]["id"], second.json()["appointment"]["id"])
        self.assertEqual(second.json()["turn"], first.json()["turn"])

    def test_with_a_payment_and_an_existing_patient(self):
        patient_id = self.book().json()["appointment"]["patient"]["id"]
        r = self.walk_in(new_patient=None, patient_id=patient_id, doctor_id=self.a.membership("owner").id,
                         payment={"amount": "100", "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["appointment"]["invoice"]["paid"], 10000)

    def test_it_needs_book_and_queue_and_a_patient(self):
        self.assertEqual(self.walk_in(new_patient=None).status_code, 400)
        from clinics.models import Membership
        from core.routing import clinic_transaction

        with clinic_transaction(self.a.id, self.a.shard):
            member = Membership.objects.get(clinic_id=self.a.id, id=self.a.membership("assistant").id)
        call(self.owner, "PUT", f"/api/v1/team/members/{member.id}/permissions",
             {"version": member.version, "codes": {"appointments.queue": False}})
        self.assertEqual(self.walk_in().status_code, 403)

    def test_another_places_doctor_is_404(self):
        r = self.walk_in(doctor_id=self.b.membership("owner").id)
        self.assertEqual(r.status_code, 404)
