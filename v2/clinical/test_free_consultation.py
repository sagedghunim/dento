"""The owner's money options (Saged, 2026-11-19).

1. `consultation_free_with_procedure`: with it on, a consultation visit that gets a procedure charges the procedure
   only; the consultation line is back at its list price when the last procedure is removed. Off (the default), the
   bill has both.
2. The deposit and this option are set by the account's owner only, even for someone given settings.manage.
"""

from core.testing import call

from .testing import M2TestCase


class FreeConsultationTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.sees_money()
        self.visit = self.in_room(service="consultation")
        self.fee = self.services_a["consultation"].price
        self.filling = self.procedure("Composite filling, two surfaces")
        self.assertGreater(self.fee, 0)

    def option(self, on=True):
        r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"consultation_free_with_procedure": on}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)

    def record(self):
        r = call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/procedures",
                 {"procedure_id": self.filling["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def lines(self, invoice):
        return sorted((line["kind"], line["amount"]) for line in invoice["lines"])

    def test_off_by_default_the_bill_has_both(self):
        invoice = self.record()["invoice"]
        self.assertEqual(self.lines(invoice), sorted([("service", self.fee), ("procedure", self.filling["price"])]))

    def test_on_the_procedure_only_and_the_fee_comes_back_when_it_is_removed(self):
        self.option()
        body = self.record()
        self.assertEqual(self.lines(body["invoice"]), sorted([("service", 0), ("procedure", self.filling["price"])]))
        self.assertEqual(body["invoice"]["total"], self.filling["price"])
        r = call(self.doc, "DELETE", f"/api/v1/appointments/{self.visit['id']}/procedures/{body['procedure']['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.lines(r.json()["invoice"]), [("service", self.fee)])
        self.assertEqual(r.json()["invoice"]["total"], self.fee)

    def test_a_procedure_visit_keeps_its_fee(self):
        self.option()
        self.visit = self.in_room(service="procedure")
        fee = self.services_a["procedure"].price
        invoice = self.record()["invoice"]
        self.assertIn(("service", fee), self.lines(invoice))


class OwnerOnlyTests(M2TestCase):
    def test_settings_manage_alone_cannot_set_the_money_options(self):
        self.set_codes("doctor", granted=["settings.manage", "money.view"])
        for changes in ({"consultation_free_with_procedure": True},
                        {"booking_deposit": True, "booking_deposit_amount": 5000}):
            r = call(self.doc, "PUT", "/api/v1/settings/clinic", {"changes": changes, "expected": {}})
            self.assertEqual(r.status_code, 403, r.content)
        # Other settings still save for them.
        r = call(self.doc, "PUT", "/api/v1/settings/clinic", {"changes": {"follow_up_days": 10}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)

    def test_the_card_is_the_owners_only(self):
        self.set_codes("doctor", granted=["settings.manage"])
        self.assertNotIn(b"data-booking-card", self.doc.get("/team").content)
        self.assertIn(b"data-booking-card", self.owner.get("/team").content)
