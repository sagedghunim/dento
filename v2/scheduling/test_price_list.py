"""Saged's fixes, part 7 (2026-11-22).

1. The desk books from the doctor's price list: every procedure has a booking service that follows its name, price
   and status. A visit booked on one is charged once even when the doctor records that same procedure.
2. No "Follow-up" in Book or Walk-in: a visit that goes on with a treatment takes its service at the full price.
3. Today's rows have no file number and no counts line under the doctor; Walk-in says "Center doctor".
4. Past shifts are the owner's; the desk keeps today's drawer.
5. Money paid over a bill: the patient file links to the drawer, which asks once and gives it back.
6. The owner writes each member's working hours, shown on Team only.
"""

import re

from clinics.models import Service
from core.routing import clinic_transaction
from core.testing import call, new_patient
from web.test_finances import fragment

from clinical.testing import M2TestCase


class PriceListCase(M2TestCase):
    def mirrors(self, fixture=None):
        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            return {s.procedure_id: s for s in Service.objects.filter(clinic_id=fixture.id, procedure_id__isnull=False)}

    def walk_in(self, service_id, patient_id=None, **extra):
        body = {"service_id": service_id, "doctor_id": self.a.membership("doctor").id, **extra}
        body.update({"patient_id": patient_id} if patient_id else {"new_patient": new_patient()})
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["appointment"]

    def call_in(self, visit_id):
        r = self.act(visit_id, "call", "waiting")
        if r.status_code == 409 and r.json()["error"]["code"] == "doctor_busy":
            self.assertEqual(self.act(r.json()["error"]["appointment_id"], "finish", "in_room").status_code, 200)
            r = self.act(visit_id, "call", "waiting")
        self.assertEqual(r.status_code, 200, r.content)

    def bill(self, visit_id):
        visit = call(self.owner, "GET", f"/api/v1/appointments/{visit_id}").json()["appointment"]
        return call(self.owner, "GET", f"/api/v1/invoices/{visit['invoice']['id']}").json()["invoice"]


class BookingFromThePriceListTests(PriceListCase):
    def test_every_active_procedure_has_a_booking_service_with_its_name_and_price(self):
        catalogue = [p for p in self.catalogue() if p["is_active"]]
        mirrors = self.mirrors()
        self.assertEqual(set(mirrors), {p["id"] for p in catalogue})
        for p in catalogue:
            s = mirrors[p["id"]]
            self.assertEqual((s.name, s.price, s.kind, s.is_active), (p["name_ar"], p["price"], "procedure", True))
            self.assertGreaterEqual(s.sort, 1000)  # after the clinic's own services

    def test_a_price_change_a_rename_and_taking_it_off_follow(self):
        filling = self.procedure("Composite filling, two surfaces")
        url = f"/api/v1/procedures/{filling['id']}"
        r = call(self.owner, "PATCH", url, {"changes": {"price": 95000, "name_ar": "حشو سطحين"}})
        self.assertEqual(r.status_code, 200, r.content)
        s = self.mirrors()[filling["id"]]
        self.assertEqual((s.name, s.price, s.is_active), ("حشو سطحين", 95000, True))
        self.assertEqual(call(self.owner, "PATCH", url, {"changes": {"is_active": False}}).status_code, 200)
        self.assertFalse(self.mirrors()[filling["id"]].is_active)
        r = call(self.owner, "POST", "/api/v1/procedures", {"name_ar": "تلميع", "price": 12000, "category": "preventive"})
        self.assertEqual(r.status_code, 201, r.content)
        added = self.mirrors()[r.json()["procedure"]["id"]]
        self.assertEqual((added.name, added.price), ("تلميع", 12000))

    def test_a_visit_booked_on_a_procedure_is_charged_once_when_the_doctor_records_it(self):
        self.sees_money()
        filling = self.procedure("Composite filling, two surfaces")
        service = self.mirrors()[filling["id"]]
        visit = self.walk_in(service.id)
        self.assertEqual(visit["fee"], filling["price"])
        self.call_in(visit["id"])
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        r = call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 36, "surfaces": ["O", "M"]})
        self.assertEqual(r.status_code, 201, r.content)
        record = r.json()["procedure"]
        bill = self.bill(visit["id"])
        self.assertEqual(bill["total"], filling["price"])
        self.assertEqual(sorted(line["amount"] for line in bill["lines"]), [0, filling["price"]])
        # Another procedure is charged on its own; the booking line stays at 0 while the filling is recorded.
        xray = self.procedure("Periapical X-ray")
        self.assertEqual(call(self.doc, "POST", url, {"procedure_id": xray["id"]}).status_code, 201)
        self.assertEqual(self.bill(visit["id"])["total"], filling["price"] + xray["price"])
        # Removing the doctor's record brings the booking's price back.
        self.assertEqual(call(self.doc, "DELETE", f"{url}/{record['id']}").status_code, 200)
        self.assertEqual(self.bill(visit["id"])["total"], filling["price"] + xray["price"])

    def test_book_and_walk_in_offer_the_consultation_and_the_price_list_but_no_follow_up(self):
        html = self.desk.get("/today").content.decode()
        follow_up = self.services_a["follow_up"]
        self.assertIn('name="service_list"', html)
        self.assertIn("طبيب المركز", html)
        self.assertNotIn("أول طبيب يفضى", html)
        book = re.search(r'<select name="service_list">(.*?)</select>', html, re.S).group(1)
        walk_in = re.search(r'<select name="service">(.*?)</select>', html, re.S).group(1)
        filling = self.procedure("Composite filling, two surfaces")
        exam = self.procedure("Full examination")  # the visit fee covers it, as on the visit screen
        mirrors = self.mirrors()
        for options in (book, walk_in):
            self.assertIn(f'<option value="{mirrors[filling["id"]].id}"', options)
            self.assertIn("<optgroup", options)
            self.assertNotIn(f'value="{mirrors[exam["id"]].id}"', options)
            self.assertNotIn(f'value="{follow_up.id}"', options)
        self.assertIn(f'value="{self.services_a["consultation"].id}"', walk_in)
        self.assertNotIn(f'name="service" value="{follow_up.id}"', html)
        self.assertIn(f'name="service" value="{self.services_a["consultation"].id}"', html)


class TreatmentGoesOnTests(PriceListCase):
    def test_a_visit_that_goes_on_is_charged_in_full_and_the_courses_carry_the_service(self):
        consultation = self.services_a["consultation"]
        first = self.book(arrive=True).json()["appointment"]
        patient = first["patient"]["id"]
        courses = call(self.desk, "GET", f"/api/v1/patients/{patient}/courses").json()
        self.assertEqual(courses["items"][0]["service_id"], consultation.id)
        self.assertRegex(courses["today"], r"^\d{4}-\d{2}-\d{2}$")
        doctor = self.a.membership("owner").id
        quote = f"/api/v1/appointments/quote?doctor={doctor}&service={consultation.id}&patient={patient}"
        self.assertTrue(call(self.desk, "GET", quote).json()["is_follow_up"])  # inside the follow-up window
        r = call(self.desk, "GET", quote + "&continues=1").json()
        self.assertEqual((r["fee"], r["is_follow_up"]), (consultation.price, False))
        self.assertEqual(self.act(first["id"], "call", "waiting", client=self.owner).status_code, 200)
        self.assertEqual(self.act(first["id"], "finish", "in_room", client=self.owner).status_code, 200)
        second = self.book(patient_id=patient, continues_id=first["id"]).json()["appointment"]
        self.assertEqual((second["fee"], second["is_follow_up"], second["course_no"]), (consultation.price, False, 2))
        self.assertEqual(self.act(second["id"], "arrive", "booked").status_code, 200)  # the recheck keeps it in full
        self.assertEqual(self.appointment(second["id"]).fee, consultation.price)


class TodayRowsTests(PriceListCase):
    def test_no_file_number_and_no_counts_line(self):
        visit = self.walk_in(self.services_a["consultation"].id)
        html = fragment(self.desk, "/today/queue").content.decode()
        self.assertIn(visit["patient"]["name"], html)
        self.assertIsNone(re.search(r"ملف\s*<bdi", html))
        self.assertNotIn("حاجزين:", html)
        self.assertNotIn(">إعادة<", html)


class ShiftsAreTheOwnersTests(PriceListCase):
    def test_the_desk_has_today_drawer_only(self):
        self.assertEqual(call(self.desk, "GET", "/api/v1/register").status_code, 200)
        self.assertEqual(call(self.desk, "GET", "/api/v1/register/shifts").status_code, 403)
        self.assertEqual(call(self.owner, "GET", "/api/v1/register/shifts").status_code, 200)
        self.assertEqual(fragment(self.desk, "/finances/shifts").status_code, 403)
        self.assertEqual(fragment(self.owner, "/finances/shifts").status_code, 200)
        self.assertNotIn('id="shifts-title"', self.desk.get("/finances?tab=register").content.decode())
        self.assertIn('id="shifts-title"', self.owner.get("/finances?tab=register").content.decode())


class GiveBackFromThePatientFileTests(PriceListCase):
    def overpaid(self):
        """A visit paid in full, then a recorded procedure removed: the patient has money over the bill."""
        visit = self.in_room()
        xray = self.procedure("Periapical X-ray")
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        record = call(self.doc, "POST", url, {"procedure_id": xray["id"]}).json()["procedure"]
        bill = self.bill(visit["id"])
        r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": bill["total"], "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(call(self.doc, "DELETE", f"{url}/{record['id']}").status_code, 200)
        return visit, bill["id"], xray["price"]

    def test_the_file_links_to_the_drawer_which_asks_once_and_gives_it_back(self):
        visit, invoice_id, credit = self.overpaid()
        patient = visit["patient"]["id"]
        link = f"/finances?tab=register&amp;give_back={invoice_id}"
        self.assertIn(link, self.desk.get(f"/patients/{patient}").content.decode())
        page = self.desk.get(f"/finances?tab=register&give_back={invoice_id}").content.decode()
        self.assertIn('id="give-back-dialog"', page)
        self.assertIn(f'data-invoice="{invoice_id}"', page)
        self.assertNotIn('id="give-back-dialog"', self.desk.get("/finances?tab=register&give_back=abc").content.decode())
        r = call(self.desk, "POST", f"/api/v1/invoices/{invoice_id}/give-back", {})
        self.assertEqual((r.status_code, r.json()["payment"]["amount"]), (201, credit))
        after = self.desk.get(f"/finances?tab=register&give_back={invoice_id}").content.decode()
        self.assertNotIn('id="give-back-dialog"', after)  # nothing over the bill any more
        file = self.desk.get(f"/patients/{patient}").content.decode()
        self.assertNotIn(link, file)
        self.assertIn("اترجّع للمريض", file)


class WorkingHoursTests(PriceListCase):
    def put(self, client, member, body):
        version = call(self.owner, "GET", "/api/v1/team").json()
        version = next(m for m in version["members"] if m["id"] == member)["version"]
        return call(client, "PUT", f"/api/v1/team/members/{member}/hours", {"version": version, **body})

    def test_the_owner_writes_them_and_team_shows_them(self):
        sara = self.a.membership("assistant").id
        r = self.put(self.owner, sara, {"days": [5, 6, 0, 1, 2, 3], "starts": "08:00", "ends": "16:00"})
        self.assertEqual(r.status_code, 200, r.content)
        member = r.json()["member"]
        self.assertEqual(member["hours"], {"days": [0, 1, 2, 3, 5, 6], "starts": "08:00", "ends": "16:00"})
        self.assertIn("السبت – الخميس", member["hours_text"])
        self.assertIn("8:00", member["hours_text"])
        team = call(self.owner, "GET", "/api/v1/team").json()["members"]
        self.assertEqual(next(m for m in team if m["id"] == sara)["hours_text"], member["hours_text"])
        # The desk can still sign in and work at any time: the hours limit nothing.
        self.assertEqual(call(self.desk, "GET", "/api/v1/today").status_code, 200)
        cleared = self.put(self.owner, sara, {"days": [], "starts": "", "ends": ""}).json()["member"]
        self.assertEqual((cleared["hours"]["days"], cleared["hours_text"]), ([], ""))

    def test_refusals(self):
        sara = self.a.membership("assistant").id
        self.assertEqual(self.put(self.desk, sara, {"days": [1]}).status_code, 403)
        for body in ({"days": [7]}, {"days": [1], "starts": "08:00"}, {"days": [1], "starts": "8", "ends": "9"},
                     {"days": [1], "starts": "08:00", "ends": "08:00"}, {"days": "1"}):
            self.assertEqual(self.put(self.owner, sara, body).status_code, 400, body)
        page = self.owner.get("/team").content.decode()
        self.assertIn('id="hours-dialog"', page)
        self.assertIn("السبت", page)
