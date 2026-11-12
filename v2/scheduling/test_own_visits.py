"""A doctor's own visits (design G4) and the walk-in (design G5)."""

import uuid

from core.testing import call

from .testing import M1TestCase


class OwnVisitTests(M1TestCase):
    """The owner works a queue, the doctor works slots; the doctor holds no appointments.others."""

    def setUp(self):
        super().setUp()
        from clinics.models import Membership
        from core.routing import clinic_transaction

        with clinic_transaction(self.a.id, self.a.shard):  # the fixture gives it; this rule's tests take it away
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("doctor").id).update(granted=[])
        self.doctor = self.a.client("doctor")
        self.owner_visit = self.book().json()["appointment"]["id"]
        self.act(self.owner_visit, "arrive", "booked")

    def test_a_doctor_cant_call_or_finish_another_doctors_visit(self):
        r = self.act(self.owner_visit, "call", "waiting", client=self.doctor)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_your_patient"))
        self.assertEqual(self.appointment(self.owner_visit).status, "waiting")

    def test_the_refusal_is_in_the_users_language(self):
        user = self.a.user("doctor")
        user.language = "en"
        user.save(update_fields=["language"])
        r = self.act(self.owner_visit, "call", "waiting", client=self.a.client("doctor"))
        self.assertIn("another doctor", r.json()["error"]["message"])
        user.language = "ar"
        user.save(update_fields=["language"])
        r = self.act(self.owner_visit, "call", "waiting", client=self.a.client("doctor"))
        self.assertTrue(any("؀" <= ch <= "ۿ" for ch in r.json()["error"]["message"]))

    def test_a_doctor_cant_write_the_note_of_another_doctors_visit(self):
        r = self.doctor.generic("PUT", f"/api/v1/appointments/{self.owner_visit}/note",
                                '{"complaint": "x"}', content_type="application/json",
                                headers={"X-Membership": str(self.doctor.membership_id),
                                         "Idempotency-Key": str(uuid.uuid4()), "If-Match": "0"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_your_patient"))

    def test_the_owner_acts_on_anyones_visit_and_a_grant_lifts_the_rule(self):
        self.assertEqual(self.act(self.owner_visit, "call", "waiting", client=self.owner).status_code, 200)
        self.assertEqual(self.act(self.owner_visit, "undo-call", "in_room", client=self.owner).status_code, 200)
        member = self.a.membership("doctor")
        from clinics.models import Membership
        from core.routing import clinic_transaction

        with clinic_transaction(self.a.id, self.a.shard):
            version = Membership.objects.get(clinic_id=self.a.id, id=member.id).version
        r = call(self.owner, "PUT", f"/api/v1/team/members/{member.id}/permissions",
                 {"version": version, "codes": {"appointments.others": True}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.act(self.owner_visit, "call", "waiting", client=self.doctor).status_code, 200)

    def test_the_desk_and_arrive_are_unaffected(self):
        visit = self.book().json()["appointment"]["id"]
        self.assertEqual(self.act(visit, "arrive", "booked", client=self.doctor).status_code, 200)
        self.assertEqual(self.act(self.owner_visit, "call", "waiting").status_code, 200)  # the assistant

    def test_today_marks_what_the_doctor_can_act_on(self):
        body = call(self.doctor, "GET", "/api/v1/today").json()
        self.assertTrue(body["own_only"])
        self.assertEqual(body["my_doctor_id"], self.a.membership("doctor").id)
        visit = next(a for q in body["queues"] for a in q["appointments"] if a["id"] == self.owner_visit)
        self.assertFalse(visit["can_act"])
        owner_view = call(self.owner, "GET", "/api/v1/today").json()
        self.assertTrue(all(a["can_act"] for q in owner_view["queues"] for a in q["appointments"]))


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
