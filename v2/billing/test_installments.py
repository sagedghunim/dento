"""Installments (Saged, 2026-11-19): what a patient owes, split into parts on set days.

The parts add up to what is owed; payments on the plan's bills cover them in order; a part past its day and not
covered is late; one running plan per patient; the desk with money makes and cancels them, the doctor without
money cannot.
"""

import datetime as dt

from core.testing import call

from billing import installments
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from scheduling.testing import today


class ScheduleTests(M2TestCase):
    def test_equal_parts_the_last_smaller_and_months_keep_the_day(self):
        parts = installments.schedule(300000, 0, "week", 100000, dt.date(2026, 1, 1))
        self.assertEqual([a for _d, a in parts], [100000, 100000, 100000])
        self.assertEqual([d.isoformat() for d, _a in parts], ["2026-01-01", "2026-01-08", "2026-01-15"])
        parts = installments.schedule(250000, 50000, "month", 70000, dt.date(2026, 1, 31))
        self.assertEqual([a for _d, a in parts], [50000, 70000, 70000, 60000])
        self.assertEqual([d.isoformat() for d, _a in parts[1:]], ["2026-01-31", "2026-02-28", "2026-03-31"])


class InstallmentTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.sees_money()
        self.visit = self.in_room()
        exam = self.procedure("Full examination")
        self.assertEqual(call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/procedures",
                              {"procedure_id": exam["id"]}).status_code, 201)
        self.patient = self.visit["patient"]["id"]
        self.url = f"/api/v1/patients/{self.patient}/installments"
        visit = call(self.desk, "GET", f"/api/v1/appointments/{self.visit['id']}").json()["appointment"]
        self.owed = visit["due"]
        self.assertGreater(self.owed, 0)

    def make(self, client=None, **body):
        return call(client or self.desk, "POST", self.url, {"every": "week", "amount": 10000, **body})

    def test_the_parts_cover_what_is_owed_and_payments_cover_them_in_order(self):
        r = self.make(down=0, amount=self.owed // 2 + 100)
        self.assertEqual(r.status_code, 201, r.content)
        plan = r.json()["plan"]
        self.assertEqual(sum(p["amount"] for p in plan["parts"]), self.owed)
        self.assertEqual(plan["total"], self.owed)
        self.assertEqual([p["state"] for p in plan["parts"]], ["ahead", "ahead"])
        first = plan["parts"][0]["amount"]
        pay = call(self.desk, "POST", f"/api/v1/appointments/{self.visit['id']}/payments", {"amount": first, "method": "cash"})
        self.assertEqual(pay.status_code, 201, pay.content)
        plan = call(self.desk, "GET", self.url).json()["plan"]
        self.assertEqual([p["state"] for p in plan["parts"]], ["paid", "ahead"])
        self.assertEqual(plan["left"], self.owed - first)

    def test_a_part_past_its_day_is_late_and_shows_on_today(self):
        self.assertEqual(self.make(down=0, amount=self.owed).status_code, 201)
        with clinic_transaction(self.a.id, self.a.shard):
            from billing.models import Installment

            Installment.objects.filter(clinic_id=self.a.id, patient_id=self.patient).update(
                due_on=today(self.a) - dt.timedelta(days=2))
        plan = call(self.desk, "GET", self.url).json()["plan"]
        self.assertEqual((plan["parts"][0]["state"], plan["late"]), ("late", self.owed))
        self.assertIn(f'href="/patients/{self.patient}"', self.desk.get("/").content.decode())

    def test_one_plan_cancel_and_the_rules(self):
        self.assertEqual(self.make(down=self.owed).status_code, 400)  # the down payment is all of it
        self.assertEqual(self.make(amount=1).json()["error"]["code"], "invalid")  # too many parts
        self.assertEqual(self.make().status_code, 201)
        self.assertEqual(self.make().json()["error"]["code"], "plan_exists")
        r = call(self.desk, "DELETE", self.url)
        self.assertEqual((r.status_code, r.json()["plan"]["status"]), (200, "cancelled"))
        self.assertEqual(self.make().status_code, 201)

    def test_the_doctor_without_money_cannot(self):
        self.set_codes("doctor")  # the money switch off again
        self.assertEqual(self.make(client=self.doc).status_code, 403)
        self.assertEqual(call(self.doc, "GET", self.url).status_code, 403)
