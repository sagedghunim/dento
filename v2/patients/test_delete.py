"""A patient is never deleted (Saged, 2026-11-26): one nobody wants goes on the blacklist. A file hidden before that
stays out of search and booking, its money stays counted, and the owner can bring it back."""

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

    def test_no_way_to_delete_a_patient(self):
        patient = self.fresh()
        for client in (self.owner, self.desk):
            self.assertEqual(call(client, "POST", f"/api/v1/patients/{patient}/delete").status_code, 404)
        self.assertNotIn("data-delete-patient", self.owner.get(f"/patients/{patient}").content.decode())
        self.assertEqual(len(self.rows(id=patient, archived_at__isnull=True)), 1)
        page = self.owner.get("/alerts").content.decode()
        self.assertNotIn('name="kind" value="patient"', page)
        self.assertNotIn('data-gate="patient"', page)

    def test_a_file_hidden_before_comes_back_for_the_owner_only(self):
        from django.utils import timezone

        patient = self.fresh("Hidden Long Ago")
        with clinic_transaction(self.a.id, self.a.shard):
            Patient.objects.filter(clinic_id=self.a.id, id=patient).update(archived_at=timezone.now())
        self.assertEqual(self.desk.get(f"/patients/{patient}").status_code, 404)
        self.assertNotIn("Hidden Long Ago", self.desk.get("/patients?q=Hidden").content.decode())
        self.assertIn("Hidden Long Ago", self.owner.get("/patients").content.decode())
        self.assertEqual(call(self.desk, "POST", f"/api/v1/patients/{patient}/restore").status_code, 403)
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
