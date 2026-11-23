"""Saged's fixes, part 12 (2026-11-23).

1. Walk-in: the consultation is the first chip, picked by default, for someone who doesn't know what they need yet.
2. The visit screen writes the file's chronic conditions, allergies and notes; they show in the patient file.
   The desk still fills them when it opens a new file.
3. The count next to "Another procedure" says "العدد", never "جرد" (the stock count).
4. "Something else" under "What comes next" suggests from the price list as the doctor types.
"""

from django.utils import translation

from clinical.testing import M2TestCase
from core.testing import call


class WalkInConsultationTests(M2TestCase):
    def test_the_consultation_is_the_first_chip_before_the_main_services(self):
        html = self.desk.get("/today").content.decode()
        dialog = html.split('id="walkin-dialog"')[1].split("</dialog>")[0]
        consult = self.services_a["consultation"].id
        self.assertIn(f'data-consult="{consult}"', dialog)
        self.assertLess(dialog.index("data-consults"), dialog.index("data-families"))


class VisitMedicalFileTests(M2TestCase):
    def visit_html(self, visit, client=None):
        r = (client or self.doc).get(f"/visits/{visit['id']}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_the_doctor_writes_the_medical_file_from_the_visit_and_the_file_shows_it(self):
        visit = self.in_room()
        html = self.visit_html(visit)
        self.assertIn("data-medical", html)
        for name in ("chronic_conditions", "allergies", "notes"):
            self.assertIn(f'name="{name}"', html.split("data-medical>")[1].split("</section>")[0])
        patient_id = visit["patient"]["id"]
        r = call(self.doc, "PATCH", f"/api/v1/patients/{patient_id}",
                 {"changes": {"allergies": "بنسلين", "chronic_conditions": "سكر"},
                  "expected": {"allergies": "", "chronic_conditions": ""}})
        self.assertEqual(r.status_code, 200, r.content)
        file_html = self.desk.get(f"/patients/{patient_id}").content.decode()
        self.assertIn("بنسلين", file_html)
        self.assertIn("سكر", file_html)
        # The visit screen opens on what the file holds.
        again = self.visit_html(visit).split("data-medical>")[1].split("</section>")[0]
        self.assertIn("بنسلين", again)
        self.assertIn("flag-allergy", again)

    def test_a_change_made_meanwhile_is_refused_not_overwritten(self):
        visit = self.in_room()
        patient_id = visit["patient"]["id"]
        url = f"/api/v1/patients/{patient_id}"
        self.assertEqual(call(self.doc, "PATCH", url, {"changes": {"notes": "أ"}, "expected": {"notes": ""}}).status_code, 200)
        r = call(self.doc, "PATCH", url, {"changes": {"notes": "ب"}, "expected": {"notes": ""}})
        self.assertEqual(r.status_code, 409)

    def test_the_desk_still_cannot_change_them_on_an_existing_file(self):
        visit = self.in_room()
        r = call(self.desk, "PATCH", f"/api/v1/patients/{visit['patient']['id']}",
                 {"changes": {"allergies": "x"}, "expected": {"allergies": ""}})
        self.assertEqual(r.status_code, 403)


class VisitWordsTests(M2TestCase):
    def test_the_procedure_count_is_not_the_stock_count(self):
        visit = self.in_room()
        with translation.override("ar"):
            self.doc.cookies["django_language"] = "ar"
            html = self.doc.get(f"/visits/{visit['id']}", HTTP_ACCEPT_LANGUAGE="ar").content.decode()
        label = html.split('class="field proc-count">')[1].split("<input")[0]
        self.assertNotIn("جرد", label)

    def test_something_else_has_a_suggestion_row(self):
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-next-other", html)
        self.assertIn("data-next-suggest", html)
