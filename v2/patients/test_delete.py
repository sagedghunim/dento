"""Deleting a patient's file (Saged, 2026-11-19): the owner only. No history: gone for good. With visits or money:
hidden everywhere (search, the file, booking) while its money stays counted, and the owner can bring it back."""

from core.routing import clinic_transaction
from core.testing import call, new_patient

from clinical.testing import M2TestCase
from patients.models import Patient


class DeletePatientTests(M2TestCase):
    def fresh(self, name="Mistake Typed Twice"):
        r = call(self.desk, "POST", "/api/v1/patients", new_patient(full_name=name))
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["patient"]["id"]

    def rows(self, **where):
        with clinic_transaction(self.a.id, self.a.shard):
            return list(Patient.objects.filter(clinic_id=self.a.id, **where).values("id", "archived_at"))

    def test_the_owner_only(self):
        patient = self.fresh()
        self.assertEqual(call(self.desk, "POST", f"/api/v1/patients/{patient}/delete").status_code, 403)
        self.assertEqual(call(self.doc, "POST", f"/api/v1/patients/{patient}/delete").status_code, 403)
        self.assertNotIn("data-delete-patient", self.desk.get(f"/patients/{patient}").content.decode())

    def test_a_file_without_history_goes_for_good(self):
        patient = self.fresh()
        r = call(self.owner, "POST", f"/api/v1/patients/{patient}/delete")
        self.assertEqual((r.status_code, r.json()["result"]), (200, "deleted"))
        self.assertEqual(self.rows(id=patient), [])
        self.assertIn("Mistake Typed Twice", self.owner.get("/alerts").content.decode())

    def test_a_file_with_visits_is_hidden_and_comes_back(self):
        visit = self.in_room()
        patient = visit["patient"]["id"]
        r = call(self.owner, "POST", f"/api/v1/patients/{patient}/delete")
        self.assertEqual((r.status_code, r.json()["result"]), (200, "hidden"))
        self.assertIsNotNone(self.rows(id=patient)[0]["archived_at"])
        self.assertEqual(self.desk.get(f"/patients/{patient}").status_code, 404)
        name = visit["patient"]["name"]
        self.assertNotIn(name, self.desk.get(f"/patients?q={name[:6]}").content.decode())
        self.assertIn(name, self.owner.get("/patients").content.decode())  # in "Deleted patients"
        r = call(self.owner, "POST", f"/api/v1/patients/{patient}/restore")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.desk.get(f"/patients/{patient}").status_code, 200)


class TagFilterTests(M2TestCase):
    def test_a_tag_on_the_patients_page_shows_who_carries_it(self):
        from patients import tags

        patient = call(self.desk, "POST", "/api/v1/patients", new_patient(full_name="Tagged Person One")).json()["patient"]["id"]
        call(self.desk, "POST", "/api/v1/patients", new_patient(full_name="Untagged Person Two"))
        with clinic_transaction(self.a.id, self.a.shard):
            tags.add_defaults(self.a.id, self.a.shard)
        page = self.desk.get("/patients").content.decode()
        self.assertIn("قايمة سودا", page)
        block = next(t for t in call(self.desk, "GET", "/api/v1/patient-tags").json()["items"] if t["name"] == "قايمة سودا")
        r = call(self.desk, "PUT", f"/api/v1/patients/{patient}/tags", {"tag_ids": [block["id"]]})
        self.assertEqual(r.status_code, 200, r.content)  # the desk tags a patient
        page = self.desk.get(f"/patients?tag={block['id']}").content.decode()
        self.assertIn("Tagged Person One", page)
        self.assertNotIn("Untagged Person Two", page)
        made = call(self.desk, "POST", "/api/v1/patient-tags", {"name": "بييجي مع والدته"})
        self.assertEqual(made.status_code, 201, made.content)  # and adds a tag of their own
