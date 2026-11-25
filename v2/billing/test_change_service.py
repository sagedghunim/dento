"""The visit turned out to be something else (Saged, 2026-11-25): the doctor changes the booked service, the bill
follows, and what was paid over the new total is the patient's credit."""

from clinical.testing import M2TestCase
from clinics.models import Service
from core.routing import clinic_transaction
from core.testing import call


class ChangeServiceTests(M2TestCase):
    def test_a_cheaper_service_leaves_the_patient_a_credit(self):
        visit = self.book(arrive=True).json()["appointment"]
        with clinic_transaction(self.a.id, self.a.shard):
            booked = Service.objects.get(clinic_id=self.a.id, id=visit["service"]["id"])
            cheaper = Service.objects.create(clinic_id=self.a.id, section_id=booked.section_id, name="أرخص",
                                             kind="procedure", price=max(booked.price - 20000, 0))
        fee = booked.price
        r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": fee, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.act(visit["id"], "call", "waiting", client=self.desk).status_code, 200)
        r = call(self.owner, "POST", f"/api/v1/appointments/{visit['id']}/service", {"service_id": cheaper.id})
        self.assertEqual(r.status_code, 200, r.content)
        invoice = r.json()["invoice"]
        self.assertEqual((invoice["total"], invoice["paid"]), (cheaper.price, fee))
        self.assertEqual(invoice["lines"][0]["description"], "أرخص")
        # Not once the visit is over.
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=self.desk).status_code, 200)
        r = call(self.owner, "POST", f"/api/v1/appointments/{visit['id']}/service", {"service_id": booked.id})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))
