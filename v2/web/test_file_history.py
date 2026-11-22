"""The file's history (Saged, 2026-11-22).

- Everything done in a patient's file is listed for the owner, newest first, with who did it.
- A doctor may write in the file with the patient outside: that change is marked "outside the visit"; one made after
  the visit had finished is marked "after the visit ended" and reaches the owner's alerts.
- The owner only: the button and the page.
"""

from audit.models import AuditChange
from core.routing import clinic_transaction
from core.testing import call, new_patient

from clinical.testing import M2TestCase, put


class FileHistoryTests(M2TestCase):
    def history(self, patient_id):
        r = self.owner.get(f"/patients/{patient_id}/history")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_what_the_doctor_does_in_the_room_and_after_it(self):
        visit = self.in_room()
        filling = self.procedure("Composite filling, two surfaces")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                 {"procedure_id": filling["id"], "tooth": 16})
        self.assertEqual(r.status_code, 201, r.content)
        note = f"/api/v1/appointments/{visit['id']}/note"
        self.assertEqual(put(self.doc, note, {"complaint": "ألم", "diagnosis": "", "notes": ""}, 0).status_code, 200)
        self.assertEqual(self.act(visit["id"], "finish", "in_room").status_code, 200)
        # Back at the file later: the doctor fixes the diagnosis after the visit ended.
        r = put(self.doc, note, {"complaint": "ألم", "diagnosis": "تسوس عميق", "notes": ""}, 1)
        self.assertEqual(r.status_code, 200, r.content)

        html = self.history(visit["patient"]["id"])
        self.assertIn("سجّل شغل", html)
        self.assertIn(filling["name_ar"], html)
        self.assertIn("سن 16", html)
        self.assertIn("تسوس عميق", html)
        self.assertEqual(html.count("بعد ما الزيارة خلصت"), 1)
        self.assertIn("فتح الملف", html)  # the walk-in opened it
        self.assertLess(html.index("تسوس عميق"), html.index("سجّل شغل"))  # newest first

        alerts = self.owner.get("/alerts").content.decode()
        self.assertIn("بعد ما الزيارة خلصت", alerts)
        self.assertIn(f'/patients/{visit["patient"]["id"]}', alerts)

    def test_a_change_with_the_patient_outside_is_marked(self):
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", {
            "service_id": self.services_a["procedure"].id, "doctor_id": self.a.membership("doctor").id,
            "new_patient": new_patient()})
        self.assertEqual(r.status_code, 201, r.content)
        visit = r.json()["appointment"]  # waiting outside, not called in
        r = put(self.doc, f"/api/v1/appointments/{visit['id']}/note", {"complaint": "متابعة", "diagnosis": "", "notes": ""}, 0)
        self.assertEqual(r.status_code, 200, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            row = AuditChange.objects.filter(clinic_id=self.a.id, action="visit_note.save").latest("id")
        self.assertEqual((row.patient_id, row.details.get("outside"), row.details.get("late")),
                         (visit["patient"]["id"], True, None))
        self.assertIn("من بره الكشف", self.history(visit["patient"]["id"]))

    def test_the_patients_details_and_money_show(self):
        visit = self.in_room()
        url = f"/api/v1/patients/{visit["patient"]["id"]}"
        r = call(self.desk, "PATCH", url, {"changes": {"address": "المعادي"}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        html = self.history(visit["patient"]["id"])
        self.assertIn("عدّل بيانات المريض", html)
        self.assertIn("المعادي", html)

    def test_the_owner_only(self):
        visit = self.in_room()
        url = f"/patients/{visit["patient"]["id"]}/history"
        for client in (self.desk, self.doc):
            self.assertEqual(client.get(url).status_code, 403)
        self.assertIn(url, self.owner.get(f"/patients/{visit["patient"]["id"]}").content.decode())
        self.assertNotIn(url, self.desk.get(f"/patients/{visit["patient"]["id"]}").content.decode())
        self.assertEqual(self.owner.get("/patients/999999999/history").status_code, 404)
