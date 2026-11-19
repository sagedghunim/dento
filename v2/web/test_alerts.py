"""The owner's alerts (Saged, 2026-11-19): what the team did with money and records, from the change log."""

from core.testing import call

from clinical.testing import M2TestCase


class AlertTests(M2TestCase):
    def remove_a_procedure(self):
        visit = self.in_room()
        exam = self.procedure("Full examination")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": exam["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        record = r.json()["procedure"]
        r = call(self.doc, "DELETE", f"/api/v1/appointments/{visit['id']}/procedures/{record['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        return visit, exam

    def test_the_owner_only(self):
        self.assertEqual(self.owner.get("/alerts").status_code, 200)
        self.assertEqual(self.doc.get("/alerts").status_code, 403)
        self.assertEqual(self.desk.get("/alerts").status_code, 403)
        self.assertNotIn('href="/alerts"', self.desk.get("/patients").content.decode())

    def test_a_removed_procedure_shows_with_who_and_the_patient_and_is_counted_until_seen(self):
        visit, exam = self.remove_a_procedure()
        side = self.owner.get("/patients").content.decode()
        self.assertIn('class="nav-count', side)
        page = self.owner.get("/alerts").content.decode()
        self.assertIn(exam["name_ar"], page)
        self.assertIn(f'href="/patients/{visit["patient"]["id"]}"', page)
        self.assertIn("is-new", page)
        # Opened: no count any more, and the row is no longer new.
        self.assertNotIn('class="nav-count', self.owner.get("/patients").content.decode())
        self.assertNotIn("is-new", self.owner.get("/alerts").content.decode())
