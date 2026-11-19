"""The section libraries (medicines, prescription templates, procedures), prescriptions with the allergy warning,
attachments, and the specialty data (design E3, E4)."""

import shutil
import tempfile

from django.test import override_settings

from audit.models import AuditAccess
from clinical.models import Medicine, Procedure
from clinics.models import Section
from clinics.specialties import CARDS, SPECIALTIES
from core.routing import clinic_transaction
from core.testing import call
from patients.models import Patient

from .testing import PDF, PNG, M2TestCase, put, upload


class MedicineTests(M2TestCase):
    def test_the_section_starts_with_its_specialty_library_and_doctors_edit_it(self):
        items = call(self.doc, "GET", "/api/v1/medicines").json()["items"]
        self.assertGreaterEqual(len(items), 25)
        r = call(self.doc, "POST", "/api/v1/medicines", {"name": "Solpadeine", "strength": "500 mg", "form": "أقراص",
                                                         "default_dose": "قرص عند اللزوم"})
        self.assertEqual(r.status_code, 201, r.content)
        medicine = r.json()["medicine"]
        dup = call(self.doc, "POST", "/api/v1/medicines", {"name": "Solpadeine", "strength": "500 mg"})
        self.assertEqual((dup.status_code, dup.json()["error"]["code"]), (409, "duplicate"))
        r = call(self.doc, "PATCH", f"/api/v1/medicines/{medicine['id']}", {"changes": {"default_note": "بعد الأكل"}})
        self.assertEqual(r.json()["medicine"]["default_note"], "بعد الأكل")
        self.assertEqual(call(self.doc, "DELETE", f"/api/v1/medicines/{medicine['id']}").json()["medicine"]["is_active"],
                         False)
        names = {m["name"] for m in call(self.doc, "GET", "/api/v1/medicines?q=solpa").json()["items"]}
        self.assertEqual(names, set())  # removed from the list
        again = call(self.doc, "POST", "/api/v1/medicines", {"name": "Solpadeine", "strength": "500 mg"})
        self.assertEqual(again.status_code, 201)  # a removed name may be added again

    def test_editing_needs_settings_medicines(self):
        self.assertEqual(call(self.desk, "POST", "/api/v1/medicines", {"name": "X"}).status_code, 403)
        self.assertEqual(call(self.desk, "GET", "/api/v1/medicines").status_code, 200) # the desk reads the file (Saged, 2026-11-19)
        self.set_codes("doctor", revoked=["settings.medicines"])
        self.assertEqual(call(self.doc, "POST", "/api/v1/medicines", {"name": "X"}).status_code, 403)
        self.assertEqual(call(self.doc, "GET", "/api/v1/medicines").status_code, 200)  # prescriptions.write reads it

    def test_bad_input_is_400_with_the_field(self):
        for body, field in (({}, "name"), ({"name": "x" * 121}, "name"), ({"name": 5}, "name"),
                            ({"name": "A", "section_id": "abc"}, "section_id")):
            r = call(self.doc, "POST", "/api/v1/medicines", body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(field, r.json()["error"]["fields"])
        r = call(self.doc, "PATCH", f"/api/v1/medicines/{call(self.doc, 'GET', '/api/v1/medicines').json()['items'][0]['id']}",
                 {"changes": {"price": 1}})
        self.assertEqual(r.status_code, 400)


class TemplateTests(M2TestCase):
    def test_templates_are_editable_with_if_match(self):
        items = call(self.doc, "GET", "/api/v1/prescription-templates").json()["items"]
        self.assertGreaterEqual(len(items), 5)
        self.assertTrue(all(t["items"] for t in items))
        medicine = call(self.doc, "GET", "/api/v1/medicines").json()["items"][0]
        r = call(self.doc, "POST", "/api/v1/prescription-templates",
                 {"name": "بعد الحشو", "items": [{"medicine_id": medicine["id"]}, {"name": "Panadol", "dose": "قرص"}]})
        self.assertEqual(r.status_code, 201, r.content)
        template = r.json()["template"]
        self.assertEqual(template["items"][0]["name"], medicine["name"])  # filled from the library
        url = f"/api/v1/prescription-templates/{template['id']}"
        self.assertEqual(put(self.doc, url, {"name": "بعد الحشو", "items": [{"name": "Brufen"}]}, 1).status_code, 200)
        stale = put(self.doc, url, {"name": "x", "items": [{"name": "Brufen"}]}, 1)
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))
        self.assertEqual(call(self.doc, "DELETE", url).json()["template"]["is_active"], False)


class PrescriptionTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.patient = self.visit["patient"]["id"]
        with clinic_transaction(self.a.id, self.a.shard):
            Patient.objects.filter(clinic_id=self.a.id, id=self.patient).update(allergies="بنسلين، Amoxicillin")
        self.augmentin = next(m for m in call(self.doc, "GET", "/api/v1/medicines").json()["items"]
                              if m["name"] == "Augmentin")

    def rx(self, client=None, **body):
        body.setdefault("patient_id", self.patient)
        body.setdefault("appointment_id", self.visit["id"])
        return call(client or self.doc, "POST", "/api/v1/prescriptions", body)

    def test_a_prescription_copies_the_lines_numbers_in_order_and_warns_on_allergies(self):
        r = self.rx(items=[{"medicine_id": self.augmentin["id"]}, {"name": "Panadol Extra", "dose": "قرص كل 8 ساعات"}])
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual([w["position"] for w in body["prescription"]["warnings"]], [1])  # Augmentin holds amoxicillin
        rx = body["prescription"]
        self.assertEqual((rx["items"][0]["dose"], rx["doctor"]["id"]),
                         (self.augmentin["default_dose"], self.a.membership("doctor").id))  # the visit's doctor
        second = self.rx(items=[{"name": "Panadol"}]).json()["prescription"]
        self.assertEqual(second["number"], rx["number"] + 1)
        with clinic_transaction(self.a.id, self.a.shard):
            before = AuditAccess.objects.filter(clinic_id=self.a.id, action=AuditAccess.Action.PRESCRIPTION).count()
        self.assertEqual(call(self.doc, "GET", f"/api/v1/prescriptions/{rx['id']}").json()["prescription"]["warnings"][0]
                         ["position"], 1)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id,
                                                        action=AuditAccess.Action.PRESCRIPTION).count(), before + 1)
        check = call(self.doc, "GET", f"/api/v1/patients/{self.patient}/allergy-check?medicine={self.augmentin['id']}"
                                      "&name=Brufen")
        self.assertEqual([w["name"] for w in check.json()["warnings"]], ["Augmentin"])
        listed = call(self.doc, "GET", f"/api/v1/patients/{self.patient}/prescriptions?limit=1").json()
        self.assertEqual((len(listed["items"]), bool(listed["next"])), (1, True))

    def test_a_typed_medicine_can_join_the_library(self):
        r = self.rx(items=[{"name": "Voltaren", "strength": "75 mg", "form": "أمبول", "add_to_library": True}])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNotNone(r.json()["prescription"]["items"][0]["medicine_id"])
        self.rx(items=[{"name": "Voltaren", "strength": "75 mg", "add_to_library": True}])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Medicine.objects.filter(clinic_id=self.a.id, name="Voltaren", is_active=True).count(), 1)
        self.set_codes("doctor", revoked=["settings.medicines"])
        self.assertEqual(self.rx(items=[{"name": "Other", "add_to_library": True}]).status_code, 403)

    def test_writing_needs_prescriptions_write_and_1_to_30_lines(self):
        self.assertEqual(self.rx(client=self.desk, items=[{"name": "Panadol"}]).status_code, 403)
        for items in ([], [{"name": "x"}] * 31, [{"dose": "no name"}], ["text"]):
            self.assertEqual(self.rx(items=items).status_code, 400, items)
        other = self.owed_visit()  # another patient's visit
        self.assertEqual(self.rx(appointment_id=other["id"], items=[{"name": "x"}]).status_code, 404)


class ProcedureCatalogueTests(M2TestCase):
    def test_the_owner_edits_the_catalogue_and_a_doctor_reads_it(self):
        self.assertGreaterEqual(len(self.catalogue()), 55)
        body = {"name_ar": "تركيب دعامة", "name_en": "Post", "unit": "tooth", "price": "350.50", "typical_min": 300,
                "typical_max": 500, "category": "restorative"}
        self.assertEqual(call(self.doc, "POST", "/api/v1/procedures", body).status_code, 403)  # settings.manage
        r = call(self.owner, "POST", "/api/v1/procedures", body)
        self.assertEqual((r.status_code, r.json()["procedure"]["price"]), (201, 350_50), r.content)
        bad = call(self.owner, "POST", "/api/v1/procedures", {**body, "name_ar": "y", "typical_min": 900})
        self.assertEqual((bad.status_code, bad.json()["error"]["fields"]), (400, {"typical_max": "range"}))
        self.assertEqual(call(self.owner, "POST", "/api/v1/procedures", {**body, "unit": "box"}).status_code, 400)
        self.assertEqual(call(self.owner, "POST", "/api/v1/procedures", body).status_code, 409)
        pid = r.json()["procedure"]["id"]
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/procedures/{pid}").json()["procedure"]["is_active"], False)
        self.assertNotIn(pid, [p["id"] for p in self.catalogue()])


@override_settings(ATTACHMENTS_ROOT=tempfile.mkdtemp(prefix="hamada-attachments-"))
class AttachmentTests(M2TestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.ATTACHMENTS_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.url = f"/api/v1/patients/{self.visit['patient']['id']}/attachments"

    def test_an_image_or_pdf_is_stored_and_opening_it_is_logged(self):
        r = upload(self.desk, self.url, PNG, title="أشعة", appointment_id=str(self.visit["id"]))
        self.assertEqual(r.status_code, 201, r.content)
        attachment = r.json()["attachment"]
        self.assertEqual((attachment["content_type"], attachment["size"]), ("image/png", len(PNG)))
        pdf = upload(self.desk, self.url, PDF, name="report.pdf", content_type="application/pdf")
        self.assertEqual(pdf.json()["attachment"]["content_type"], "application/pdf")
        self.assertEqual(call(self.desk, "GET", attachment["url"]).status_code, 200) # the desk reads the file (Saged, 2026-11-19)
        with clinic_transaction(self.a.id, self.a.shard):
            before = AuditAccess.objects.filter(clinic_id=self.a.id, action=AuditAccess.Action.ATTACHMENT).count()
        opened = self.doc.get(attachment["url"], headers={"X-Membership": str(self.doc.membership_id)})
        self.assertEqual((opened.status_code, b"".join(opened.streaming_content)), (200, PNG))
        self.assertEqual(opened["X-Content-Type-Options"], "nosniff")
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id,
                                                        action=AuditAccess.Action.ATTACHMENT).count(), before + 1)
        listed = call(self.doc, "GET", self.url).json()["items"]
        self.assertEqual(len(listed), 2)
        record = call(self.doc, "GET", f"/api/v1/appointments/{self.visit['id']}/record").json()
        self.assertEqual([a["id"] for a in record["attachments"]], [attachment["id"]])

    def test_other_files_and_files_over_10_mb_are_refused(self):
        r = upload(self.desk, self.url, b"MZ\x90\x00 an executable", name="x.png")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (415, "file_type"))
        big = upload(self.desk, self.url, PNG + b"\x00" * (10 * 1024 * 1024), name="big.png")
        self.assertEqual((big.status_code, big.json()["error"]["code"]), (413, "too_large"))
        json_body = call(self.desk, "POST", self.url, {"file": "x"})
        self.assertEqual(json_body.status_code, 415)

    def test_a_repeat_with_the_same_key_stores_once(self):
        import uuid

        key = uuid.uuid4()
        first = upload(self.desk, self.url, PNG, key=key)
        again = upload(self.desk, self.url, PNG, key=key)
        self.assertEqual((again.status_code, again.json()), (first.status_code, first.json()))
        self.assertEqual(len(call(self.doc, "GET", self.url).json()["items"]), 1)


class SpecialtyDataTests(M2TestCase):
    def test_ten_cards_in_the_sign_up_order_then_internal(self):
        self.assertEqual(CARDS, ["dental", "pediatrics", "dermatology", "obgyn", "ent", "ophthalmology", "orthopedics",
                                 "cardiology", "physiotherapy", "veterinary"])
        self.assertEqual(list(SPECIALTIES), [*CARDS, "internal"])

    def test_each_specialty_ships_a_reviewable_catalogue(self):
        from clinical.models import CONDITIONS, UNITS

        dental = SPECIALTIES["dental"]
        self.assertTrue(55 <= len(dental["procedures"]) <= 70, len(dental["procedures"]))
        self.assertTrue(25 <= len(dental["medicines"]) <= 35, len(dental["medicines"]))
        self.assertTrue(all(p["description"] for p in dental["procedures"]))
        for code, spec in SPECIALTIES.items():
            procedures, medicines = spec["procedures"], spec["medicines"]
            if code != "dental":
                self.assertTrue(8 <= len(procedures) and 8 <= len(medicines), code)
            names = [p["name_ar"] for p in procedures]
            self.assertEqual(len(names), len(set(names)), code)
            meds = [(m["name"], m["strength"]) for m in medicines]
            self.assertEqual(len(meds), len(set(meds)), code)
            for p in procedures:
                self.assertIn(p["unit"], UNITS, (code, p["name_en"]))
                self.assertTrue(p["condition"] == "" or p["condition"] in CONDITIONS, (code, p["name_en"]))
                self.assertTrue(0 <= p["typical_min"] <= p["price"] <= p["typical_max"] or p["price"] == 0,
                                (code, p["name_en"]))
                self.assertTrue(p["name_ar"] and p["name_en"], code)
            self.assertTrue(all(m["name"] and m["form"] for m in medicines), code)

    def test_a_new_center_copies_each_sections_catalogue_once(self):
        center = self.clinic("cc", "shard_1", sections=[{"specialty": "pediatrics", "name": "Kids"},
                                                        {"specialty": "veterinary", "name": "Vet"}])
        from directory.flows import ensure_clinic_on_shard

        with clinic_transaction(center.id, center.shard):
            sections = {s.specialty_code: s.id for s in Section.objects.filter(clinic_id=center.id)}
            counts = {code: Procedure.objects.filter(clinic_id=center.id, section_id=sid).count()
                      for code, sid in sections.items()}
        self.assertEqual(counts, {code: len(SPECIALTIES[code]["procedures"]) for code in sections})
        ensure_clinic_on_shard(center.entry, [], center.user("owner"), kind="center")  # a second run adds nothing
        with clinic_transaction(center.id, center.shard):
            self.assertEqual(Procedure.objects.filter(clinic_id=center.id).count(), sum(counts.values()))
