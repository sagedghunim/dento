"""Saged's fixes, part 10 (2026-11-22).

1. Two-step sign-in: an owner who has it on shows the same code again (QR) after typing the password, to add it to
   another phone.
2. Follow-ups after a procedure: a doctor changes them for their own sections only.
3. The price list: a main service of the clinic's own, typed by name.
4. Installments: any amount toward the plan; more than is due by today splits what is left evenly over the parts
   still to come (choice "a": the same number of parts, each smaller).
5. The visit in the patient file shows what was charged: never a consultation at 0 next to a paid procedure.
"""

import datetime as dt

from django.test import Client

from accounts import totp
from billing import installments
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import today


class ShowTheCodeTests(M2TestCase):
    url = "/api/v1/me/totp/show"

    def test_the_owner_sees_the_same_code_after_the_password(self):
        r = call(self.owner, "POST", self.url, {"password": "wrong-one"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "wrong_password"))
        r = call(self.owner, "POST", self.url, {"password": "pass-word-123"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("<svg", r.json()["qr_svg"])
        # The code it gives is the one the owner signs in with.
        code = totp.code_at(r.json()["secret"], totp.current_step() + 1)
        signed = Client()
        call(signed, "POST", "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        self.assertEqual(call(signed, "POST", "/api/v1/auth/code", {"code": code}).status_code, 200)
        self.assertIn('id="totp-show"', self.owner.get("/account/security").content.decode())

    def test_not_before_the_code_and_not_without_two_step(self):
        r = call(self.a.client("owner", mfa=False), "POST", self.url, {"password": "pass-word-123"})
        self.assertEqual(r.status_code, 403)
        r = call(self.doc, "POST", self.url, {"password": "pass-word-123"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_set_up"))


class FollowUpScopeTests(M2TestCase):
    def test_a_doctor_changes_the_follow_ups_of_their_own_sections_only(self):
        from clinics.models import Section

        with clinic_transaction(self.a.id, self.a.shard):
            other = Section.objects.create(clinic_id=self.a.id, name="جلدية", specialty_code="dermatology", sort=9)
        r = call(self.owner, "POST", "/api/v1/procedures", {"name_ar": "تقشير", "section_id": other.id, "price": 10000})
        self.assertEqual(r.status_code, 201, r.content)
        theirs = r.json()["procedure"]["id"]
        mine = self.procedure("Composite filling, two surfaces")["id"]
        recipe = {"changes": {"follow_up": [{"days": 7, "label": "متابعة"}]}}
        self.assertEqual(call(self.doc, "PATCH", f"/api/v1/procedures/{mine}", recipe).status_code, 200)
        self.assertEqual(call(self.doc, "PATCH", f"/api/v1/procedures/{theirs}", recipe).status_code, 404)
        self.assertEqual(call(self.owner, "PATCH", f"/api/v1/procedures/{theirs}", recipe).status_code, 200)
        html = self.doc.get("/prescriptions").content.decode()
        self.assertIn("data-recipe-new", html)
        self.assertIn("data-sections=", html)


class OwnMainServiceTests(M2TestCase):
    def test_a_main_service_typed_by_name_groups_the_price_list_and_booking(self):
        r = call(self.owner, "POST", "/api/v1/procedures",
                 {"name_ar": "تبييض بالليزر المنزلي", "family": "تجميل الابتسامة", "unit": "case", "price": 150000})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["procedure"]["family"], "تجميل الابتسامة")
        html = self.owner.get("/prices").content.decode()
        self.assertIn('<option value="تجميل الابتسامة">', html)  # offered again for the next one
        self.assertIn('value="+new"', html)
        self.assertIn("تجميل الابتسامة", html.split("price-rows")[0] + html)


class InstallmentPaymentTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.sees_money()
        visit = self.in_room()
        filling = self.procedure("Composite filling, two surfaces")["id"]
        for tooth in (16, 26, 36):
            r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                     {"procedure_id": filling, "tooth": tooth})
            self.assertEqual(r.status_code, 201, r.content)
        self.patient = visit["patient"]["id"]
        self.owed = call(self.desk, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]["due"]
        self.day = today(self.a)
        # Saged's example, scaled: a part due today, then two more a week apart.
        self.part = (self.owed // 4) // 100 * 100
        self.first = self.owed - 2 * self.part
        parts = [{"due_on": (self.day + dt.timedelta(days=7 * n)).isoformat(), "amount": a}
                 for n, a in enumerate((self.first, self.part, self.part))]
        r = call(self.desk, "POST", f"/api/v1/patients/{self.patient}/installments", {"parts": parts})
        self.assertEqual(r.status_code, 201, r.content)
        self.url = f"/api/v1/patients/{self.patient}/installments/payments"

    def test_paying_what_is_due_keeps_the_parts(self):
        r = call(self.desk, "POST", self.url, {"amount": self.first, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        plan = r.json()["plan"]
        self.assertEqual([(p["amount"], p["state"]) for p in plan["parts"]],
                         [(self.first, "paid"), (self.part, "ahead"), (self.part, "ahead")])

    def test_paying_more_splits_the_rest_evenly_over_the_parts_to_come(self):
        extra = self.part // 2
        r = call(self.desk, "POST", self.url, {"amount": self.first + extra, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        plan = r.json()["plan"]
        rest = self.owed - self.first - extra
        self.assertEqual([p["amount"] for p in plan["parts"]], [self.first, extra, *installments.split_evenly(rest, 2)])
        self.assertEqual([p["state"] for p in plan["parts"]], ["paid", "paid", "ahead", "ahead"])
        self.assertEqual([p["due_on"] for p in plan["parts"][2:]],
                         [(self.day + dt.timedelta(days=7 * n)).isoformat() for n in (1, 2)])
        self.assertEqual(plan["left"], rest)
        self.assertEqual(sum(p["amount"] for p in plan["parts"]), self.owed)
        # The payment is on the bill like any other.
        payments = call(self.desk, "GET", f"/api/v1/patients/{self.patient}/payments").json()["items"]
        self.assertEqual(payments[0]["amount"], self.first + extra)

    def test_the_rules(self):
        r = call(self.desk, "POST", self.url, {"amount": self.owed + 100, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))
        self.set_codes("doctor")  # the money switch off
        self.assertEqual(call(self.doc, "POST", self.url, {"amount": 100, "method": "cash"}).status_code, 403)
        html = self.desk.get(f"/patients/{self.patient}").content.decode()
        self.assertIn("data-inst-pay", html)
        self.assertIn('id="inst-pay-dialog"', html)

    def test_respread_on_sageds_example(self):
        # 2,000 EGP: 500 down (paid), then 500 today and two more; she pays 700 today.
        day = dt.date(2026, 11, 22)
        before = [{"due_on": day - dt.timedelta(days=30), "amount": 50000, "paid": 50000},
                  {"due_on": day, "amount": 50000, "paid": 0},
                  {"due_on": day + dt.timedelta(days=30), "amount": 50000, "paid": 0},
                  {"due_on": day + dt.timedelta(days=60), "amount": 50000, "paid": 0}]
        parts = installments.respread(before, 50000 + 70000, 200000, day)
        self.assertEqual([a for _d, a in parts], [50000, 50000, 20000, 40000, 40000])
        self.assertEqual(installments.split_evenly(100000, 3), [33400, 33400, 33200])


class VisitBillTests(M2TestCase):
    def test_a_free_consultation_goes_and_the_procedure_shows_with_its_price(self):
        self.sees_money()
        r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"consultation_free_with_procedure": True}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        visit = self.in_room(service="consultation")
        filling = self.procedure("Composite filling, two surfaces")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": filling["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        html = self.desk.get(f"/patients/{visit['patient']['id']}").content.decode()
        card = html.split('data-visit data-id="%d"' % visit["id"])[1].split("</li>")[0]
        self.assertIn(filling["name_ar"], card)
        head = card.split('class="visit-when"')[1].split("</div>")[0]
        self.assertNotIn(self.services_a["consultation"].name, head)
        self.assertIn("visit-bill", card)
