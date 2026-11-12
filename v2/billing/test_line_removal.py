"""Who may remove an invoice line (chief judge M1, R3; design 5.3).

- The visit's own service line always needs billing.discount: removing it is a 100% discount.
- Any other line needs billing.discount, unless the same membership added it on the same business day.
Each line says whether this person may remove it (`removable`).
"""

import datetime as dt
from zoneinfo import ZoneInfo

from core.testing import call
from scheduling.testing import M1TestCase, frozen

CAIRO = ZoneInfo("Africa/Cairo")


def cairo(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=CAIRO)


class LineRemovalTests(M1TestCase):
    def invoice(self, client=None, appointment=None):
        appointment = appointment or self.book().json()["appointment"]
        return call(client or self.desk, "POST", f"/api/v1/appointments/{appointment['id']}/invoice").json()["invoice"]

    def lines(self, invoice_id, client=None):
        return call(client or self.desk, "GET", f"/api/v1/invoices/{invoice_id}").json()["invoice"]["lines"]

    def add(self, invoice_id, client=None):
        r = call(client or self.desk, "POST", f"/api/v1/invoices/{invoice_id}/lines",
                 {"kind": "procedure", "service_id": self.services_a["procedure"].id})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["invoice"]["lines"][-1]

    def remove(self, invoice_id, line_id, client=None):
        return call(client or self.desk, "DELETE", f"/api/v1/invoices/{invoice_id}/lines/{line_id}")

    def test_an_assistant_cant_remove_the_visits_service_line(self):
        invoice = self.invoice()
        [service_line] = invoice["lines"]
        self.assertEqual(service_line["kind"], "service")
        r = self.remove(invoice["id"], service_line["id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.lines(invoice["id"])[0]["id"], service_line["id"])
        self.assertFalse(self.lines(invoice["id"])[0]["removable"])
        # The owner holds billing.discount: removing it is their 100% discount.
        self.assertTrue(self.lines(invoice["id"], self.owner)[0]["removable"])
        self.assertEqual(self.remove(invoice["id"], service_line["id"], self.owner).status_code, 200)

    def test_the_desk_removes_its_own_line_the_same_day_but_not_anyone_elses(self):
        invoice = self.invoice()
        mine = self.add(invoice["id"])
        doctors = self.add(invoice["id"], self.owner)  # someone else: the visit's doctor (the owner)
        lines = {line["id"]: line for line in self.lines(invoice["id"])}
        self.assertEqual((lines[mine["id"]]["removable"], lines[doctors["id"]]["removable"]), (True, False))
        self.assertEqual(lines[mine["id"]]["created_by_id"], self.a.membership("assistant").id)
        self.assertEqual(self.remove(invoice["id"], doctors["id"]).status_code, 403)
        self.assertEqual(self.remove(invoice["id"], mine["id"]).status_code, 200)

    def test_not_after_the_business_day_ends(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            appointment = self.book(client=desk).json()["appointment"]
            invoice = self.invoice(desk, appointment)
            line = self.add(invoice["id"], desk)
        with frozen(cairo("2026-11-11T03:59")):  # still the business day of 10 November
            self.assertTrue(self.lines(invoice["id"], self.a.client("assistant"))[-1]["removable"])
        with frozen(cairo("2026-11-11T04:00")):
            desk = self.a.client("assistant")
            self.assertFalse(self.lines(invoice["id"], desk)[-1]["removable"])
            self.assertEqual(self.remove(invoice["id"], line["id"], desk).status_code, 403)
