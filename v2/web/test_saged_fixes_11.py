"""Saged's fixes, part 11 (2026-11-23): the holes a deliberate wrong-input round found.

1. Cash never leaves the drawer beyond what is in it (cash out, a drawer expense, a cash payout, a cash refund).
2. A payout only up to what the month still owes, never for a month to come.
3. Money given back lowers the bill, so the patient doesn't owe it (unless the desk says otherwise).
4. A patient being seen, owing money or on installments can't be deleted.
5. Teeth: nothing that needs the tooth on one taken out; a molar procedure only on a molar; counts by the unit.
6. Installments paid in full close by themselves.
7. Small ones: a name in letters, an expense not years back, a main service never an inner key, a price at least
   10 EGP, a too-long request answered in JSON.
8. No follow-up window: a return visit costs its own price (Saged: there is no free or cheaper follow-up).
"""

import datetime as dt

from core.testing import call

from clinical.testing import M2TestCase
from scheduling.testing import today


class DrawerAndPayTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.sees_money()
        cats = call(self.owner, "GET", "/api/v1/expense-categories").json()["items"]
        self.category = next(c["id"] for c in cats if c.get("code") != "other")

    def test_cash_never_beyond_the_drawer(self):
        r = call(self.owner, "POST", "/api/v1/register/movements", {"kind": "out", "amount": 100000000, "reason": "x"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_enough_cash"))
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": self.category, "amount": 100000000,
                                                          "from_shift": True, "note": "x"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_enough_cash"))
        # Money in, then out up to it, is fine.
        self.assertEqual(call(self.owner, "POST", "/api/v1/register/movements",
                              {"kind": "in", "amount": 50000, "reason": "change"}).status_code, 201)
        self.assertEqual(call(self.owner, "POST", "/api/v1/register/movements",
                              {"kind": "out", "amount": 50000, "reason": "bank"}).status_code, 201)

    def test_a_payout_up_to_what_the_month_owes_and_not_ahead(self):
        doctor = self.a.membership("doctor").id
        month = today(self.a).strftime("%Y-%m")
        r = call(self.owner, "POST", f"/api/v1/finances/pay/{doctor}/payouts",
                 {"month": month, "amount": 100000000, "method": "transfer"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))
        ahead = (today(self.a).replace(day=1) + dt.timedelta(days=40)).strftime("%Y-%m")
        r = call(self.owner, "POST", f"/api/v1/finances/pay/{doctor}/payouts",
                 {"month": ahead, "amount": 100, "method": "transfer"})
        self.assertEqual(r.status_code, 400)

    def test_old_expense_dates_are_refused(self):
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": self.category, "amount": 100, "day": "1990-01-01"})
        self.assertEqual(r.status_code, 400)


class RefundTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.sees_money()
        self.visit = self.owed_visit()
        self.due = call(self.desk, "GET", f"/api/v1/appointments/{self.visit['id']}").json()["appointment"]["due"]
        r = call(self.desk, "POST", f"/api/v1/appointments/{self.visit['id']}/payments", {"amount": self.due, "method": "card"})
        self.assertEqual(r.status_code, 201, r.content)
        self.payment = r.json()["payment"]["id"]

    def owed(self):
        return call(self.desk, "GET", f"/api/v1/appointments/{self.visit['id']}").json()["appointment"]["due"]

    def test_money_given_back_lowers_the_bill(self):
        r = call(self.owner, "POST", f"/api/v1/payments/{self.payment}/refund", {"amount": self.due // 2})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.owed(), 0)
        self.assertEqual(r.json()["invoice"]["total"], self.due - self.due // 2)

    def test_or_the_patient_still_owes_it_when_the_desk_says_so(self):
        r = call(self.owner, "POST", f"/api/v1/payments/{self.payment}/refund", {"amount": 1000, "lower_bill": False})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.owed(), 1000)


class TeethTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.url = f"/api/v1/appointments/{self.visit['id']}/procedures"

    def record(self, name, **body):
        return call(self.doc, "POST", self.url, {"procedure_id": self.procedure(name)["id"], **body})

    def test_nothing_on_a_tooth_taken_out_but_an_implant_is_fine(self):
        self.assertEqual(self.record("Simple extraction", tooth=36).status_code, 201)
        r = self.record("Composite filling, one surface", tooth=36)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "tooth_gone"))
        self.assertEqual(self.record("Root canal treatment, molar", tooth=36).status_code, 409)
        self.assertEqual(self.record("Implant fixture", tooth=36).status_code, 201)
        self.assertEqual(self.record("Composite filling, one surface", tooth=37).status_code, 201)

    def test_a_molar_procedure_on_a_molar_only(self):
        r = self.record("Root canal treatment, molar", tooth=11)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "wrong_tooth"))
        self.assertEqual(self.record("Root canal treatment, molar", tooth=46).status_code, 201)
        self.assertEqual(self.record("Root canal treatment, molar", tooth=85).status_code, 201)  # a primary molar

    def test_counts_by_the_unit(self):
        quadrant = "Deep scaling and root planing, per quadrant"
        self.assertEqual(self.record(quadrant, qty=5).status_code, 400)
        self.assertEqual(self.record(quadrant, qty=4).status_code, 201)
        self.assertEqual(self.record("Composite filling, one surface", tooth=16, qty=3).status_code, 400)


class InstallmentCloseTests(M2TestCase):
    def test_a_plan_paid_in_full_closes_and_a_new_one_can_start(self):
        from billing import installments
        from core.routing import clinic_transaction

        self.sees_money()
        visit = self.owed_visit()
        patient = visit["patient"]["id"]
        url = f"/api/v1/patients/{patient}/installments"
        due = call(self.desk, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]["due"]
        self.assertEqual(call(self.desk, "POST", url, {"every": "week", "amount": due}).status_code, 201)
        r = call(self.desk, "POST", url + "/payments", {"amount": due, "method": "cash"})
        self.assertEqual(r.json()["plan"]["status"], "done")
        with clinic_transaction(self.a.id, self.a.shard):
            from billing.models import InstallmentPlan

            self.assertFalse(InstallmentPlan.objects.filter(clinic_id=self.a.id, patient_id=patient,
                                                            status="active").exists())
        self.assertIsNotNone(installments)


class SmallTests(M2TestCase):
    def test_a_name_in_letters(self):
        from core.testing import new_patient

        body = {"service_id": self.services_a["consultation"].id, "doctor_id": self.a.membership("doctor").id,
                "new_patient": new_patient("123 456 789")}
        self.assertEqual(call(self.desk, "POST", "/api/v1/appointments/walk-in", body).status_code, 400)

    def test_main_service_names_and_the_least_price(self):
        for family in ("c-cleaning", "+new", "CROWN"):
            r = call(self.owner, "POST", "/api/v1/procedures", {"name_ar": f"خدمة {family}", "family": family})
            self.assertEqual(r.status_code, 400, family)
        r = call(self.owner, "POST", "/api/v1/procedures", {"name_ar": "خدمة بقرش", "price": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(call(self.owner, "POST", "/api/v1/procedures", {"name_ar": "خدمة بعشرة", "price": 1000}).status_code, 201)

    def test_a_too_long_note_is_a_json_answer(self):
        visit = self.in_room()
        r = call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/note", {"notes": "x" * 100000, "version": 0})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_big"))


class NoFollowUpWindowTests(M2TestCase):
    def test_a_return_visit_costs_its_own_price(self):
        first = self.owed_visit()
        patient = first["patient"]["id"]
        price = self.services_a["consultation"].price
        body = {"service_id": self.services_a["consultation"].id, "doctor_id": self.a.membership("doctor").id,
                "patient_id": patient}
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["appointment"]["fee"], r.json()["appointment"]["is_follow_up"]), (price, False))
