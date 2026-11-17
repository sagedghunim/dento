"""M2 concurrency on real PostgreSQL (design 10, T5): threads with their own connections, released together."""

import json
import threading
import uuid

from django.db import connections
from django.test import TransactionTestCase

from billing.models import Invoice, Payment
from clinical.models import PlanItem, VisitNote
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.testing import add_schedule, services_of


class ClinicalConcurrencyTests(TransactionTestCase):
    databases = "__all__"

    def setUp(self):
        helper = ShardTestCase()
        self.a = helper.clinic("mc", "shard_1")
        add_schedule(self.a, "owner")
        add_schedule(self.a, "doctor")  # the visits are the doctor's: a doctor acts only on their own
        self.services = services_of(self.a)
        self.owner_id = self.a.membership("owner").id
        self.desk = self.a.client("assistant")

    def run_together(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait()
                results[index] = job()
            except Exception as error:  # surfaced by the assertions
                results[index] = error
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def visit(self, patient_id=None, finish=False):
        body = {"doctor_id": self.a.membership("doctor").id, "service_id": self.services["procedure"].id}
        body.update({"patient_id": patient_id} if patient_id else {"new_patient": new_patient(full_name="Race Patient")})
        visit = call(self.desk, "POST", "/api/v1/appointments", body).json()["appointment"]
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/arrive", {"status": "booked"})
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/call", {"status": "waiting"})
        if finish:
            call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/finish", {"status": "in_room"})
        return visit

    def test_two_finishes_paying_one_patients_earlier_balance_never_overpay_or_deadlock(self):
        fee = self.services["procedure"].price
        first = self.visit(finish=True)
        patient = first["patient"]["id"]
        self.visit(patient, finish=True)
        # One open visit a day per patient (Saged, 2026-11-17): the second open one is tomorrow's, put in the room
        # directly, as a visit left open past its day would be.
        import datetime as dt

        from scheduling.models import Appointment
        from scheduling.testing import today

        one = self.visit(patient)
        two = call(self.desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.a.membership("doctor").id, "service_id": self.services["procedure"].id,
            "patient_id": patient, "day": (today(self.a) + dt.timedelta(days=1)).isoformat()}).json()["appointment"]
        now = dt.datetime.now(dt.UTC)
        with clinic_transaction(self.a.id, self.a.shard):
            Appointment.objects.filter(clinic_id=self.a.id, id=two["id"]).update(status="in_room", arrived_at=now,
                                                                                  called_at=now)
        body = {"status": "in_room", "earlier": {"amount": 2 * fee, "method": "cash"}}
        results = self.run_together([
            lambda: call(self.a.client("assistant"), "POST", f"/api/v1/appointments/{one['id']}/finish", body),
            lambda: call(self.a.client("owner"), "POST", f"/api/v1/appointments/{two['id']}/finish", body)])
        codes = sorted(r.status_code if hasattr(r, "status_code") else repr(r) for r in results)
        # The second sees the balance already paid by the first (each sees the other's open visit as owed too).
        self.assertEqual(codes[0], 200, codes)
        with clinic_transaction(self.a.id, self.a.shard):
            for invoice in Invoice.objects.filter(clinic_id=self.a.id, patient_id=patient):
                self.assertLessEqual(invoice.paid, invoice.total)
                paid = sum(p.amount for p in Payment.objects.filter(clinic_id=self.a.id, invoice_id=invoice.id))
                self.assertEqual(paid, invoice.paid)

    def test_two_saves_of_one_note_version_take_effect_once(self):
        visit = self.visit()
        url = f"/api/v1/appointments/{visit['id']}/note"

        def save(text, role):
            client = self.a.client(role)
            headers = {"X-Membership": str(client.membership_id), "Idempotency-Key": str(uuid.uuid4()), "If-Match": "0"}
            return client.generic("PUT", url, json.dumps({"complaint": text}), content_type="application/json",
                                  headers=headers)

        results = self.run_together([lambda: save("one", "owner"), lambda: save("two", "doctor")])
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(VisitNote.objects.filter(clinic_id=self.a.id).count(), 1)

    def test_two_procedures_on_one_visit_both_reach_the_invoice(self):
        visit = self.visit()
        procedures = call(self.a.client("owner"), "GET", "/api/v1/procedures").json()["items"]  # with list prices
        filling = next(p for p in procedures if p["name_en"] == "Composite filling, one surface")
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        results = self.run_together([
            lambda: call(self.a.client("doctor"), "POST", url, {"procedure_id": filling["id"], "tooth": 11}),
            lambda: call(self.a.client("owner"), "POST", url, {"procedure_id": filling["id"], "tooth": 21})])
        self.assertEqual([r.status_code for r in results], [201, 201])
        with clinic_transaction(self.a.id, self.a.shard):
            invoice = Invoice.objects.get(clinic_id=self.a.id, appointment_id=visit["id"], status="open")
        self.assertEqual(invoice.total, self.services["procedure"].price + 2 * filling["price"])

    def test_a_double_click_on_a_plan_item_moves_it_once(self):
        visit = self.visit()
        doc = self.a.client("doctor")
        crown = next(p for p in call(doc, "GET", "/api/v1/procedures").json()["items"] if p["name_en"] == "Zirconia crown")
        item = call(doc, "POST", f"/api/v1/patients/{visit['patient']['id']}/plans",
                    {"items": [{"procedure_id": crown["id"], "tooth": 11}]}).json()["plan"]["items"][0]
        url = f"/api/v1/plan-items/{item['id']}/status"
        results = self.run_together([
            lambda: call(self.a.client("doctor"), "POST", url, {"status": "done", "expected": "planned"}),
            lambda: call(self.a.client("owner"), "POST", url, {"status": "dropped", "expected": "planned"})])
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertIn(PlanItem.objects.get(clinic_id=self.a.id, id=item["id"]).status, ("done", "dropped"))
