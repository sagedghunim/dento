"""The dental chart page (design E3): permissions, another clinic's records, right to left, and what it shows."""

import re

from accounts.models import User
from audit.models import AuditAccess
from clinical.testing import M2TestCase
from core.testing import call
from web.tests import access_count, fragment


def tooth_button(html, number):
    found = re.search(r'<button type="button" class="tooth [a-z]+" data-tooth="%d"[^>]*>' % number, html)
    assert found, f"tooth {number} not on the page"
    return found.group(0)


class TeethPageTests(M2TestCase):
    def test_the_visit_chart_is_arabic_right_to_left_with_the_teeth_left_to_right(self):
        visit = self.in_room()
        before = access_count(self.a, AuditAccess.Action.HISTORY)
        r = self.doc.get(f"/visits/{visit['id']}/teeth")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn('class="dentition" dir="ltr" data-dentition="permanent"', html)
        self.assertIn("مخطط الأسنان", html)
        self.assertIn("تسوس", html)  # the legend in Arabic
        self.assertIn("اضغط على أي سن", html)
        # Anatomical order: 18 first, 11 before 21; lower 48 first, 41 before 31.
        order = [int(n) for n in re.findall(r'data-tooth="(\d+)"', html)]
        self.assertEqual(order[:32], [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28,
                                      48, 47, 46, 45, 44, 43, 42, 41, 31, 32, 33, 34, 35, 36, 37, 38])
        self.assertEqual(order[32:], [55, 54, 53, 52, 51, 61, 62, 63, 64, 65, 85, 84, 83, 82, 81, 71, 72, 73, 74, 75])
        self.assertIn('data-shape="molar"', tooth_button(html, 16))
        self.assertIn('data-shape="incisor"', tooth_button(html, 11))
        self.assertIn('data-part="finding"', html)
        self.assertIn('data-part="procedure"', html)
        self.assertNotIn("Mohamed", re.search(r"<title>(.*?)</title>", html, re.S).group(1))
        self.assertEqual(access_count(self.a, AuditAccess.Action.HISTORY), before + 1)

    def test_english_for_a_user_who_chose_it(self):
        visit = self.in_room()
        User.objects.filter(pk=self.a.user("doctor").pk).update(language="en")
        html = self.doc.get(f"/visits/{visit['id']}/teeth").content.decode()
        self.assertIn('lang="en" dir="ltr"', html)
        self.assertIn("Pulp caries", html)
        self.assertIn("Tap a tooth to view its history and record findings.", html)

    def test_the_chart_shows_findings_procedures_and_plan_items(self):
        visit = self.in_room()
        p = visit["patient"]["id"]
        r = call(self.doc, "PUT", f"/api/v1/patients/{p}/teeth/16", {"condition": "caries", "surfaces": ["O"]})
        self.assertEqual(r.status_code, 200, r.content)
        filling = next(x for x in self.catalogue() if x["unit"] == "tooth" and x["condition"] == "filling")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                 {"procedure_id": filling["id"], "tooth": 26, "surfaces": ["M", "O"]})
        self.assertEqual(r.status_code, 201, r.content)
        crown = next(x for x in self.catalogue() if x["unit"] == "tooth" and x["condition"] == "crown")
        r = call(self.doc, "POST", f"/api/v1/patients/{p}/plans", {"items": [{"procedure_id": crown["id"], "tooth": 36}]})
        self.assertEqual(r.status_code, 201, r.content)
        html = self.doc.get(f"/visits/{visit['id']}/teeth").content.decode()
        self.assertIn('data-condition="caries"', tooth_button(html, 16))
        tooth_26 = tooth_button(html, 26)
        self.assertIn('data-condition="filling"', tooth_26)
        self.assertIn("data-done", tooth_26)
        self.assertIn("data-progress", tooth_button(html, 36))
        self.assertNotIn("data-progress", tooth_button(html, 16))
        self.assertIn('"plan_id": %d' % r.json()["plan"]["id"], html)  # "add to plan" goes to the open plan

    def test_a_booked_visit_takes_findings_but_not_procedures(self):
        visit = self.booked()  # the doctor's own
        html = self.doc.get(f"/visits/{visit['id']}/teeth").content.decode()
        self.assertIn('data-part="finding"', html)
        self.assertNotIn('data-part="procedure"', html)
        self.assertIn("بعد وصول المريض", html)

    def test_the_patient_file_chart_has_no_procedures(self):
        visit = self.in_room()
        r = self.doc.get(f"/patients/{visit['patient']['id']}/teeth")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-part="finding"', html)
        self.assertIn('data-part="plan"', html)
        self.assertNotIn('data-part="procedure"', html)


class TeethPermissionTests(M2TestCase):
    def test_without_clinical_view_the_pages_are_refused_and_not_linked(self):
        visit = self.in_room()
        p = visit["patient"]["id"]
        self.assertEqual(self.desk.get(f"/visits/{visit['id']}/teeth").status_code, 403)
        self.assertEqual(self.desk.get(f"/patients/{p}/teeth").status_code, 403)
        self.assertEqual(fragment(self.desk, f"/patients/{p}/teeth/16/history").status_code, 403)
        self.assertNotIn("/teeth", fragment(self.desk).content.decode())
        self.assertNotIn("/teeth", self.desk.get(f"/patients/{p}").content.decode())

    def test_the_doctor_gets_the_links(self):
        visit = self.in_room()
        today = fragment(self.doc).content.decode()
        self.assertIn(f'href="/visits/{visit["id"]}/teeth"', today)
        self.assertIn(f'href="/patients/{visit["patient"]["id"]}/teeth"',
                      self.doc.get(f"/patients/{visit['patient']['id']}").content.decode())

    def test_reading_without_procedures_record_shows_no_forms(self):
        visit = self.in_room()
        self.set_codes("doctor", revoked=["procedures.record"])
        r = self.doc.get(f"/visits/{visit['id']}/teeth")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("data-part=", html)
        self.assertIn("data-history", html)

    def test_another_clinics_visit_patient_and_history_are_not_found(self):
        theirs = self.in_room(fixture=self.b)
        p = theirs["patient"]["id"]
        before = access_count(self.b, AuditAccess.Action.HISTORY)
        self.assertEqual(self.doc.get(f"/visits/{theirs['id']}/teeth").status_code, 404)
        self.assertEqual(self.doc.get(f"/patients/{p}/teeth").status_code, 404)
        self.assertEqual(fragment(self.doc, f"/patients/{p}/teeth/16/history").status_code, 404)
        self.assertEqual(access_count(self.b, AuditAccess.Action.HISTORY), before)

    def test_a_non_dental_place_has_no_chart(self):
        kids = self.clinic("k", "shard_1", specialty="pediatrics")
        doctor = kids.client("doctor")
        self.assertEqual(doctor.get(f"/patients/1/teeth").status_code, 404)


class ToothHistoryTests(M2TestCase):
    def test_history_is_newest_first_and_formatted_in_arabic(self):
        visit = self.in_room()
        p = visit["patient"]["id"]
        url = f"/api/v1/patients/{p}/teeth/16"
        self.assertEqual(call(self.doc, "PUT", url, {"condition": "caries", "surfaces": ["O"]}).status_code, 200)
        r = call(self.doc, "PUT", url, {"condition": "filling", "surfaces": ["O"], "expected_condition": "caries"})
        self.assertEqual(r.status_code, 200, r.content)
        r = fragment(self.doc, f"/patients/{p}/teeth/16/history")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(html.count('class="history-item"'), 2)
        self.assertLess(html.index("حشو"), html.index("تسوس"))
        self.assertEqual(r["X-Next"], "")

    def test_a_tooth_outside_fdi_is_not_found(self):
        visit = self.in_room()
        self.assertEqual(fragment(self.doc, f"/patients/{visit['patient']['id']}/teeth/19/history").status_code, 404)

    def test_the_fragment_needs_the_tabs_clinic(self):
        visit = self.in_room()
        r = fragment(self.doc, f"/patients/{visit['patient']['id']}/teeth/16/history", membership=False)
        self.assertEqual(r.status_code, 409)
