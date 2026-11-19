"""How many of a procedure, and one consultation a visit (Saged, 2026-11-19).

- The desk books 3 fillings once: the fee is 3 times the price, and the bill has one line of qty 3.
- A consultation or follow-up is always 1, whatever is sent; outside 1 to 20 is refused.
- The doctor records a procedure with a count when no tooth is named.
- A bill never has two consultation lines.
"""

from core.testing import call, new_patient

from clinical.testing import M2TestCase


class BookingQtyTests(M2TestCase):
    def walk_in(self, service, **extra):
        body = {"service_id": self.services_a[service].id, "doctor_id": self.a.membership("doctor").id,
                "new_patient": new_patient(), **extra}
        return call(self.desk, "POST", "/api/v1/appointments/walk-in", body)

    def bill(self, visit):
        return call(self.owner, "GET", f"/api/v1/invoices/{visit['invoice']['id']}").json()["invoice"]

    def test_three_of_a_procedure_cost_three_times_on_one_line(self):
        price = self.services_a["procedure"].price
        r = self.walk_in("procedure", qty=3, payment={"amount": price, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        visit = r.json()["appointment"]
        self.assertEqual((visit["qty"], visit["fee"]), (3, 3 * price))
        [line] = self.bill(visit)["lines"]
        self.assertEqual((line["qty"], line["unit_price"], line["amount"]), (3, price, 3 * price))

    def test_a_consultation_is_always_one_and_the_count_has_limits(self):
        r = self.walk_in("consultation", qty=3)
        self.assertEqual(r.status_code, 201, r.content)
        visit = r.json()["appointment"]
        self.assertEqual((visit["qty"], visit["fee"]), (1, self.services_a["consultation"].price))
        for bad in (0, 21, "3", True):
            self.assertEqual(self.walk_in("procedure", qty=bad).status_code, 400, bad)

    def test_the_doctor_records_a_count(self):
        self.sees_money()
        visit = self.in_room()
        exam = self.procedure("Full examination")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": exam["id"], "qty": 3})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["procedure"]["qty"], r.json()["procedure"]["amount"]), (3, 3 * exam["price"]))

    def test_one_consultation_a_bill(self):
        visit = self.in_room(service="consultation")
        exam = self.procedure("Full examination")  # the bill comes with the first line
        self.assertEqual(call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                              {"procedure_id": exam["id"]}).status_code, 201)
        invoice = call(self.owner, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]["invoice"]
        r = call(self.owner, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                 {"kind": "service", "service_id": self.services_a["consultation"].id, "qty": 1})
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["error"]["code"], "already_added")
