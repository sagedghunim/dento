"""The visit screen's finish and "Back to the visit" (Saged, 2026-11-24).

- The visit screen's finish (check_record) needs at least one thing done today, and the visit's notes or the
  dental chart worked on in this visit.
- Within five minutes of a finish the visit goes back to the room, its counters taken back; later it is refused.
"""

import datetime as dt
from unittest import mock

from django.utils import timezone

from clinical.testing import M2TestCase, put
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import summaries, today


class FinishRuleTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.filling = self.procedure("Composite filling, two surfaces")

    def finish(self, **body):
        return call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/finish",
                    {"status": "in_room", "check_record": True, **body})

    def record(self, tooth=None):
        body = {"procedure_id": self.filling["id"]}
        if tooth:
            body["tooth"] = tooth
        r = call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/procedures", body)
        self.assertEqual(r.status_code, 201, r.content)

    def test_nothing_done_is_refused(self):
        r = self.finish()
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (409, {"procedures": "required"}))
        self.assertEqual(self.appointment(self.visit["id"]).status, "in_room")

    def test_done_without_notes_or_chart_is_refused_then_notes_are_enough(self):
        self.record()
        r = self.finish()
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (409, {"notes": "required"}))
        r = put(self.doc, f"/api/v1/appointments/{self.visit['id']}/note", {"notes": "Pain on 36"}, 0)
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertEqual(self.finish().status_code, 200)

    def test_the_chart_is_enough_without_notes(self):
        self.record()
        r = call(self.doc, "PUT", f"/api/v1/patients/{self.visit['patient']['id']}/teeth",
                 {"appointment_id": self.visit["id"], "teeth": [{"tooth": 36, "condition": "caries", "surfaces": ["O"]}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.finish().status_code, 200)

    def test_the_desk_finish_without_the_check_is_as_before(self):
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room").status_code, 200)


class ReopenTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.doctor = self.a.membership("doctor").id
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room", client=self.doc).status_code, 200)

    def reopen(self):
        return call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/reopen", {"status": "done"})

    def test_back_to_the_room_takes_the_counters_back(self):
        day = today(self.a)
        clinic, doctors = summaries(self.a, day)
        self.assertEqual((clinic["done"], doctors[self.doctor]["done"]), (1, 1))
        r = self.reopen()
        self.assertEqual(r.status_code, 200, r.content)
        row = self.appointment(self.visit["id"])
        self.assertEqual((row.status, row.finished_at), ("in_room", None))
        self.assertIsNotNone(row.called_at)
        clinic, doctors = summaries(self.a, day)
        self.assertEqual((clinic["done"], doctors[self.doctor]["done"]), (0, 0))
        from summaries.models import PatientStats

        with clinic_transaction(self.a.id, self.a.shard):
            self.assertFalse(PatientStats.objects.filter(clinic_id=self.a.id,
                                                         patient_id=self.visit["patient"]["id"]).exists())
        self.assertEqual(self.reopen().status_code, 200)  # a repeat is harmless
        # Finished again: counted once.
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room", client=self.doc).status_code, 200)
        clinic, doctors = summaries(self.a, day)
        self.assertEqual((clinic["done"], doctors[self.doctor]["done"]), (1, 1))

    def test_after_five_minutes_it_is_refused(self):
        later = timezone.now() + dt.timedelta(minutes=6)
        with mock.patch("django.utils.timezone.now", return_value=later):
            r = self.reopen()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "too_late"))
        self.assertEqual(self.appointment(self.visit["id"]).status, "done")

    def test_not_while_the_doctor_has_the_next_patient_in(self):
        self.in_room()
        r = self.reopen()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "doctor_busy"))


class OneAskTests(M2TestCase):
    def test_the_desk_asks_the_doctor_about_one_patient_at_a_time(self):
        first, second = self.book(arrive=True).json()["appointment"], self.book(arrive=True).json()["appointment"]
        self.assertEqual(self.act(first["id"], "ready", "waiting").status_code, 200)
        r = self.act(second["id"], "ready", "waiting")
        error = r.json()["error"]
        self.assertEqual((r.status_code, error["code"], error["appointment_id"]), (409, "doctor_asked", first["id"]))
        self.assertEqual(self.appointment(second["id"]).status, "waiting")
        self.assertEqual(self.act(first["id"], "undo-ready", "ready").status_code, 200)
        self.assertEqual(self.act(second["id"], "ready", "waiting").status_code, 200)


class ConsultOnlyTests(M2TestCase):
    def test_consultation_only_is_enough_with_notes_and_charges_nothing(self):
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}"
        r = call(self.doc, "POST", f"{url}/consult-only", {"on": True})
        self.assertEqual((r.status_code, r.json()), (200, {"consult_only": True}))
        put(self.doc, f"{url}/note", {"notes": "Asked the price of a crown, will think"}, 0)
        self.assertEqual(call(self.doc, "POST", f"{url}/finish", {"status": "in_room", "check_record": True}).status_code, 200)
        from clinical.models import VisitProcedure

        with clinic_transaction(self.a.id, self.a.shard):
            self.assertFalse(VisitProcedure.objects.filter(clinic_id=self.a.id, appointment_id=visit["id"]).exists())
        # Not after the visit is over.
        self.assertEqual(call(self.doc, "POST", f"{url}/consult-only", {"on": False}).status_code, 409)


class DeskFinishTests(M2TestCase):
    def test_the_desk_finishes_only_after_the_doctor_wrote_and_the_doctor_may_go_back(self):
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}"
        r = call(self.desk, "POST", f"{url}/finish", {"status": "in_room", "check_record": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "record_needed"))
        self.assertIn("الدكتور", r.json()["error"]["message"])
        call(self.doc, "POST", f"{url}/consult-only", {"on": True})
        put(self.doc, f"{url}/note", {"notes": "Seen"}, 0)
        self.assertEqual(call(self.desk, "POST", f"{url}/finish", {"status": "in_room", "check_record": True})
                         .status_code, 200)
        page = self.doc.get("/today").content.decode()
        self.assertIn('id="just-finished"', page)
        self.assertEqual(call(self.doc, "POST", f"{url}/reopen", {"status": "done"}).status_code, 200)
