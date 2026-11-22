"""The doctor's visit screen (Saged, 2026-11-13): it opens on call-in, Today comes back to it, only the visit's own
doctor sees it, the notes save with a version, one patient in the room per doctor, and the notes in the history."""

from audit.models import AuditAccess
from clinical.testing import M2TestCase, put
from core.testing import call, new_patient
from web.tests import access_count


class VisitScreenTests(M2TestCase):
    def test_today_and_home_go_back_to_the_open_visit_until_it_is_finished(self):
        visit = self.in_room()
        for url in ("/", "/today", "/today?day=2026-01-01"):
            r = self.doc.get(url)
            self.assertEqual((r.status_code, r["Location"]), (302, f"/visits/{visit['id']}"), url)
        self.assertEqual(self.desk.get("/").status_code, 200)  # the desk who called them in stays on Today
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=self.doc).status_code, 200)
        self.assertEqual(self.doc.get("/").status_code, 200)
        r = self.doc.get(f"/visits/{visit['id']}")
        self.assertEqual((r.status_code, r["Location"]), (302, "/"))

    def test_the_screen_shows_the_patient_band_in_arabic_right_to_left(self):
        visit = self.in_room()
        p = visit["patient"]["id"]
        r = call(self.desk, "PATCH", f"/api/v1/patients/{p}", {"changes": {"allergies": "Penicillin", "chronic_conditions": "Diabetes"},
                                                                  "expected": {"allergies": "", "chronic_conditions": ""}})
        self.assertIn(r.status_code, (200, 204), r.content)
        before = access_count(self.a, AuditAccess.Action.NOTE)
        r = self.doc.get(f"/visits/{visit['id']}")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("Mohamed Ahmed", html)
        self.assertNotIn('href="tel:', html)  # no phone on the visit screen (Saged, 2026-11-19)
        # The file opens in a window over the visit, not a new tab (Saged, 2026-11-22).
        self.assertIn(f'href="/patients/{visit["patient"]["id"]}" data-open-file', html)
        self.assertIn('id="file-dialog"', html)
        embedded = self.doc.get(f'/patients/{visit["patient"]["id"]}?embed=1')
        self.assertEqual(embedded["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", embedded["Content-Security-Policy"])
        self.assertIn('<base target="_top">', embedded.content.decode())
        self.assertNotIn('class="side"', embedded.content.decode())
        self.assertIn("Penicillin", html)
        self.assertIn("Diabetes", html)
        self.assertIn("خلص الكشف", html)
        self.assertIn("ملاحظات الزيارة", html)
        self.assertIn("مريض جديد", html)
        self.assertIn("أول زيارة", html)
        self.assertIn('data-chart-url="/visits/%d/chart"' % visit["id"], html)  # a dental place
        self.assertIn('"called_at": "', html)
        self.assertNotIn("Mohamed", html.split("<title>")[1].split("</title>")[0])
        self.assertEqual(access_count(self.a, AuditAccess.Action.NOTE), before + 1)

    def test_only_the_visits_own_doctor_sees_it(self):
        visit = self.in_room()
        self.assertEqual(self.a.client("owner").get(f"/visits/{visit['id']}").status_code, 404)  # another doctor
        self.assertEqual(self.b.client("doctor").get(f"/visits/{visit['id']}").status_code, 404)  # another place
        self.assertEqual(self.desk.get(f"/visits/{visit['id']}").status_code, 403)  # no visits.notes
        self.assertEqual(self.a.client("owner").get(f"/visits/{visit['id']}/chart",
                                                    headers={"X-Membership": str(self.a.membership("owner").id)}).status_code, 404)
        self.assertEqual(self.doc.get("/visits/999999").status_code, 404)

    def test_the_chart_part_loads_inside_the_visit(self):
        visit = self.in_room()
        r = self.doc.get(f"/visits/{visit['id']}/chart", headers={"X-Membership": str(self.doc.membership_id)})
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-chart-layout', html)
        self.assertIn('id="chart-data"', html)
        self.assertNotIn("<html", html)

    def test_notes_save_with_a_version_and_another_doctors_visit_is_404(self):
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}/note"
        r = put(self.doc, url, {"notes": "حشو 36، تنظيف"}, 0)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["note"]["version"], 1)
        r = put(self.doc, url, {"notes": "حشو 36، تنظيف، أشعة"}, 0)  # a stale version
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))
        self.assertEqual(r.json()["error"]["note"]["notes"], "حشو 36، تنظيف")
        self.assertEqual(put(self.doc, url, {"notes": "حشو 36، تنظيف، أشعة"}, 1).status_code, 200)
        owner = self.a.client("owner")
        owner_visit = self.book(client=self.desk).json()["appointment"]  # the owner's own queue
        self.act(owner_visit["id"], "arrive", "booked")
        self.assertEqual(put(self.doc, f"/api/v1/appointments/{owner_visit['id']}/note", {"notes": "x"}, 0).status_code, 404)
        self.assertEqual(put(self.b.client("doctor"), url, {"notes": "x"}, 0).status_code, 404)
        self.assertEqual(owner.get(f"/visits/{owner_visit['id']}").status_code, 302)  # not in the room yet: Today

    def test_the_notes_show_in_the_patients_history(self):
        visit = self.in_room()
        self.assertEqual(put(self.doc, f"/api/v1/appointments/{visit['id']}/note", {"notes": "حشو 36، تنظيف"}, 0).status_code, 200)
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=self.doc).status_code, 200)
        html = self.doc.get(f"/patients/{visit['patient']['id']}").content.decode()
        self.assertIn("حشو 36، تنظيف", html)
        self.assertIn("حشو 36", self.desk.get(f"/patients/{visit['patient']['id']}").content.decode()) # the desk reads the file (Saged, 2026-11-19)
        # The next visit's band shows what was done last time.
        again = self.in_room(patient_id=visit["patient"]["id"])
        html = self.doc.get(f"/visits/{again['id']}").content.decode()
        self.assertIn("حشو 36، تنظيف", html)
        self.assertIn("متابع", html)

    def test_a_doctor_never_has_two_patients_in_the_room(self):
        first = self.in_room()
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in",
                 {"service_id": self.services_a["procedure"].id, "doctor_id": self.a.membership("doctor").id,
                  "new_patient": new_patient(full_name="Second Patient", phone="01011111111")})
        second = r.json()["appointment"]
        r = self.act(second["id"], "call", "waiting", client=self.desk)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "doctor_busy"))
        self.assertEqual(r.json()["error"]["appointment_id"], first["id"])
        self.assertEqual(self.appointment(second["id"]).status, "waiting")
        self.assertEqual(self.act(first["id"], "finish", "in_room").status_code, 200)
        self.assertEqual(self.act(second["id"], "call", "waiting", client=self.desk).status_code, 200)
        self.assertEqual(self.act(second["id"], "call", "waiting", client=self.desk).status_code, 200)  # a repeat

    def test_today_tells_the_script_who_treats(self):
        self.assertIn('"treats": true', self.doc.get("/").content.decode())
        self.assertIn('"treats": false', self.desk.get("/").content.decode())
