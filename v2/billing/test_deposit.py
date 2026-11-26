"""The booking deposit and a plan session's visit price (Saged, 2026-11-16): the money, to the piastre.

- A deposit is a payment on the visit's bill, taken with the booking through the booking's own `payment`, counted on
  the day it was taken, in that day's drawer. The bill may hold more than its total only up to the deposit.
- What is owed is the total minus what was paid, never negative; what was paid over the total is a credit the desk
  refunds by hand. A no-show keeps the deposit and owes nothing. A cancel is refused until the deposit is refunded.
- A plan session's visit fee follows its step (none, follow-up, consultation) and survives the arrival recompute.

The clock is frozen: day 0 is Tuesday 2026-11-10. Consultation EGP 300, follow-up EGP 150, braces adjustment EGP 500,
scaling EGP 200, the deposit EGP 100.
"""

import contextlib
import datetime as dt
import random
from zoneinfo import ZoneInfo

from audit.models import AuditChange
from billing.models import Invoice, InvoiceLine, Payment
from billing.tests import MoneyInvariantTests
from clinical.models import PlanItem
from clinical.testing import M2TestCase
from clinics.models import Clinic, Membership, Service
from core.routing import clinic_transaction
from core.testing import call, new_patient
from scheduling.jobs import sweep_clinic, system_ctx
from scheduling.models import Appointment
from scheduling.testing import frozen, summaries

CAIRO = ZoneInfo("Africa/Cairo")
DAY = dt.date(2026, 11, 10)
CONSULT, FOLLOW, BRACES, SCALING, DEPOSIT = 300_00, 150_00, 500_00, 200_00, 100_00


class DepositTestCase(M2TestCase):
    def setUp(self):
        super().setUp()
        self.doctor_id = self.a.membership("doctor").id
        self.owner_id = self.a.membership("owner").id
        self.sees_money()  # the doctor records and the tests read the amounts (Saged, 2026-11-16)
        with clinic_transaction(self.a.id, self.a.shard):
            Service.objects.filter(clinic_id=self.a.id, kind="follow_up").update(price=FOLLOW)
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                granted=["billing.refund", "billing.discount"])

    @contextlib.contextmanager
    def on(self, offset=0, hour=19):
        """The clock on day `offset` at `hour`, with clients signed in then."""
        day = DAY + dt.timedelta(days=offset)
        with frozen(dt.datetime.combine(day, dt.time(hour), tzinfo=CAIRO)):
            self.desk, self.owner, self.doc = (self.a.client(role) for role in ("assistant", "owner", "doctor"))
            yield day

    def deposit_on(self, amount=DEPOSIT, fixture=None, on=True):
        fixture = fixture or self.a
        r = call(fixture.client("owner"), "PUT", "/api/v1/settings/clinic",
                 {"changes": {"booking_deposit": on, "booking_deposit_amount": amount}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["settings"]

    def prices(self):
        """Braces adjustment at EGP 500 and scaling at EGP 200, as the owner sets them."""
        braces = self.procedure("Monthly orthodontic adjustment", client=self.owner)
        scaling = self.procedure("Scaling and polishing", client=self.owner)
        for procedure, price in ((braces, BRACES), (scaling, SCALING)):
            r = call(self.owner, "PATCH", f"/api/v1/procedures/{procedure['id']}", {"changes": {"price": price}})
            self.assertEqual(r.status_code, 200, r.content)
        return braces, scaling

    def slot(self, offset, hhmm="10:00"):
        day = DAY + dt.timedelta(days=offset)
        return {"day": day.isoformat(), "starts_at": self.slot_iso(self.a, day, hhmm), "doctor_id": self.doctor_id}

    def deposit(self, amount=DEPOSIT, method="cash"):
        return {"amount": amount, "method": method, "deposit": True}

    def bill(self, visit_id, client=None):
        """The visit's bill as the Pay dialog reads it, or None."""
        visit = call(client or self.desk, "GET", f"/api/v1/appointments/{visit_id}").json()["appointment"]
        if visit["invoice"] is None:
            return None
        return call(client or self.desk, "GET", f"/api/v1/invoices/{visit['invoice']['id']}").json()["invoice"]

    def figures(self, visit_id):
        bill = self.bill(visit_id)
        return {name: bill[name] for name in ("total", "paid", "due", "credit", "deposit")}

    def record(self, visit_id, procedure, **extra):
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit_id}/procedures", {"procedure_id": procedure["id"], **extra})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def pay(self, visit_id, amount, method="cash"):
        return call(self.desk, "POST", f"/api/v1/appointments/{visit_id}/payments", {"amount": amount, "method": method})

    def refund(self, payment_id, amount):
        return call(self.desk, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": amount})

    def to_the_room(self, visit_id):
        self.assertEqual(self.act(visit_id, "arrive", "booked").status_code, 200)
        self.assertEqual(self.act(visit_id, "call", "waiting").status_code, 200)

    def day_money(self, day):
        """(collected, refunded) of the clinic-day, 0 when the day has no row."""
        clinic, _doctors = summaries(self.a, day)
        return (clinic["collected"], clinic["refunded"]) if clinic else (0, 0)

    def drawer(self):
        """The open shift's cash: (collected, refunded)."""
        from finance.models import Shift
        from finance.services import shift_totals

        with clinic_transaction(self.a.id, self.a.shard):
            shift = Shift.objects.get(clinic_id=self.a.id, status="open")
            cash = shift_totals(system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard), shift.id)["methods"].get(
                "cash", {"collected": 0, "refunded": 0})
        return cash["collected"], cash["refunded"]

    def rows(self, model, **where):
        with clinic_transaction(self.a.id, self.a.shard):
            return list(model.objects.filter(clinic_id=self.a.id, **where).order_by("id").values())

    def plan_session(self, visit_price="none", first_service="consultation", procedure=True, days=3):
        """Day 0: the doctor's visit (paid in full), braces recorded with its one follow-up session in `days` days.
        Returns (patient id, the session's follow-up, the braces procedure, the scaling procedure)."""
        braces, scaling = self.prices()
        step = {"days": days, "label": "شد تقويم", "label_en": "Braces adjustment", "visit_price": visit_price}
        if procedure:
            step["procedure_id"] = braces["id"]
        r = call(self.owner, "PATCH", f"/api/v1/procedures/{braces['id']}", {"changes": {"follow_up": [step]}})
        self.assertEqual(r.status_code, 200, r.content)
        visit = self.in_room(service=first_service)
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/follow-ups", {"recipe_of": braces["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        session = r.json()["upcoming"][0]
        fee = self.services_a[first_service].price
        r = self.act(visit["id"], "finish", "in_room", payment={"amount": fee, "method": "card"})
        self.assertEqual(r.status_code, 200, r.content)
        return visit["patient"]["id"], session, braces, scaling


class SettingTests(DepositTestCase):
    def test_it_is_off_by_default_and_only_settings_manage_turns_it_on(self):
        with self.on():
            settings = call(self.owner, "GET", "/api/v1/clinic").json()["settings"]
            self.assertEqual((settings["booking_deposit"], settings["booking_deposit_amount"]), (False, 0))
            body = {"changes": {"booking_deposit": True, "booking_deposit_amount": DEPOSIT}, "expected": {}}
            self.assertEqual(call(self.desk, "PUT", "/api/v1/settings/clinic", body).status_code, 403)
            self.assertEqual(call(self.doc, "PUT", "/api/v1/settings/clinic", body).status_code, 403)
            settings = self.deposit_on()
            self.assertEqual((settings["booking_deposit"], settings["booking_deposit_amount"]), (True, DEPOSIT))

    def test_bad_values_are_a_400(self):
        with self.on():
            for changes in ({"booking_deposit": "yes"}, {"booking_deposit_amount": -1}, {"booking_deposit_amount": 10.5},
                            {"booking_deposit_amount": True}, {"booking_deposit_amount": 10**12},
                            {"booking_deposit": True},  # on with no amount
                            {"booking_deposit": True, "booking_deposit_amount": 0}):
                r = call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": changes, "expected": {}})
                self.assertEqual(r.status_code, 400, (changes, r.content))
            self.deposit_on()
            r = call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"booking_deposit_amount": 0}, "expected": {}})
            self.assertEqual(r.status_code, 400, r.content)  # still on: the amount can't go to zero


class RehearsalTests(DepositTestCase):
    """Deposit, then charges: the visit fee follows the step; due is the total less the deposit, to the piastre."""

    def rehearse(self, visit_price, fee, first_service="consultation"):
        with self.on(0):
            self.deposit_on()
            patient, session, braces, scaling = self.plan_session(visit_price, first_service)
            first_fee = self.services_a[first_service].price
            visit = self.book(patient_id=patient, follow_up_id=session["id"], payment=self.deposit(), **self.slot(3)).json()["appointment"]
            self.assertEqual((visit["fee"], visit["due"], visit["invoice"]["paid"]), (fee, max(0, fee - DEPOSIT), DEPOSIT))
            self.assertEqual(self.figures(visit["id"]), {"total": fee, "paid": DEPOSIT, "due": max(0, fee - DEPOSIT),
                                                         "credit": max(0, DEPOSIT - fee), "deposit": DEPOSIT})
            self.assertEqual(self.day_money(DAY), (first_fee + DEPOSIT, 0))
            self.assertEqual(self.drawer(), (DEPOSIT, 0))  # the first visit was paid by card
        with self.on(3, hour=10) as day:
            self.to_the_room(visit["id"])
            self.assertEqual(self.appointment(visit["id"]).fee, fee)  # the arrival recompute keeps the step's fee
            done = self.record(visit["id"], braces, plan_item_id=session["id"])
            self.assertEqual(done["procedure"]["amount"], BRACES)
            self.record(visit["id"], scaling)
            total = fee + BRACES + SCALING
            self.assertEqual(self.figures(visit["id"]), {"total": total, "paid": DEPOSIT, "due": total - DEPOSIT,
                                                         "credit": 0, "deposit": DEPOSIT})
            over = self.pay(visit["id"], total - DEPOSIT + 1)
            self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "over_balance"))
            r = self.act(visit["id"], "finish", "in_room", payment={"amount": total - DEPOSIT, "method": "cash"})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual((r.json()["invoice"]["remaining"], r.json()["appointment"]["due"]), (0, 0))
            self.assertEqual(self.figures(visit["id"]), {"total": total, "paid": total, "due": 0, "credit": 0, "deposit": DEPOSIT})
            again = self.pay(visit["id"], 1)
            self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "over_balance"))
            bill = self.bill(visit["id"])
            self.assertEqual([(p["amount"], p["deposit"], p["day"]) for p in bill["payments"]],
                             [(DEPOSIT, True, DAY.isoformat()), (total - DEPOSIT, False, day.isoformat())])
            self.assertEqual([line["amount"] for line in bill["lines"]], [fee, BRACES, SCALING])
            # Each day counts what it took, once: the deposit on day 0, the rest on the visit's day.
            self.assertEqual(self.day_money(DAY), (first_fee + DEPOSIT, 0))
            self.assertEqual(self.day_money(day), (total - DEPOSIT, 0))
            self.assertEqual(self.drawer(), (total, 0))
            item = self.rows(PlanItem, id=session["id"])[0]
            self.assertEqual((item["status"], item["done_appointment_id"]), ("done", visit["id"]))
        MoneyInvariantTests.check_invariants(self, [DAY, day])
        return visit, total

    def test_no_visit_fee(self):
        _visit, total = self.rehearse("none", 0)
        self.assertEqual(total, 700_00)  # 500 + 200, and EGP 600 was due after the EGP 100 deposit

    def test_the_follow_up_price(self):
        _visit, total = self.rehearse("follow_up", FOLLOW)
        self.assertEqual(total, 850_00)

    def test_the_follow_up_price_for_a_patient_the_rule_would_charge_in_full(self):
        self.rehearse("follow_up", FOLLOW, first_service="procedure")

    def test_the_full_consultation_for_a_patient_the_rule_would_charge_less(self):
        _visit, total = self.rehearse("consultation", CONSULT)
        self.assertEqual(total, 1000_00)

    def test_the_receipt_shows_the_deposit_with_its_date(self):
        visit, _total = self.rehearse("none", 0)
        with self.on(3, hour=11):
            html = self.desk.get(f"/print/receipts/{self.bill(visit['id'])['id']}").content.decode()
        self.assertIn("عربون", html)
        self.assertIn('data-deposit', html)
        self.assertEqual(html.count("data-payment"), 2)


class DiscountTests(DepositTestCase):
    def kind(self, name, kind, value):
        r = call(self.owner, "POST", "/api/v1/discount-types", {"name": name, "kind": kind, "value": value})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["discount_type"]

    def apply(self, bill, kind):
        return call(self.desk, "PUT", f"/api/v1/invoices/{bill['id']}/discount-type",
                    {"type_id": kind["id"] if kind else None, "expected_discount": bill["discount"]})

    def test_a_percent_recomputes_on_the_lines_and_due_stays_exact(self):
        with self.on(0):
            self.deposit_on()
            braces, scaling = self.prices()
            call(self.owner, "PATCH", f"/api/v1/procedures/{scaling['id']}", {"changes": {"price": 333_33}})
            ten = self.kind("Friends", "percent", 10)
            visit = self.book(payment=self.deposit(), **self.slot(3)).json()["appointment"]
            self.assertEqual(self.figures(visit["id"]), {"total": CONSULT, "paid": DEPOSIT, "due": 200_00, "credit": 0,
                                                         "deposit": DEPOSIT})
        with self.on(3, hour=10):
            self.to_the_room(visit["id"])
            self.record(visit["id"], braces)
            r = self.apply(self.bill(visit["id"]), ten)
            self.assertEqual(r.status_code, 200, r.content)
            bill = self.bill(visit["id"])  # 10% of 800.00 = 80.00
            self.assertEqual((bill["subtotal"], bill["discount"], bill["total"], bill["paid"], bill["due"]),
                             (800_00, 80_00, 720_00, DEPOSIT, 620_00))
            self.record(visit["id"], scaling)
            bill = self.bill(visit["id"])  # 10% of 1133.33 = 113.333 -> 113.33
            self.assertEqual((bill["subtotal"], bill["discount"], bill["total"], bill["paid"], bill["due"]),
                             (1133_33, 113_33, 1020_00, DEPOSIT, 920_00))
            self.assertEqual(self.pay(visit["id"], 920_01).status_code, 409)
            self.assertEqual(self.pay(visit["id"], 920_00).status_code, 201)
            self.assertEqual(self.figures(visit["id"]), {"total": 1020_00, "paid": 1020_00, "due": 0, "credit": 0,
                                                         "deposit": DEPOSIT})

    def test_a_fixed_discount_with_a_deposit(self):
        with self.on(0):
            self.deposit_on()
            braces, _scaling = self.prices()
            fifty = self.kind("Staff", "fixed", 50_00)
            visit = self.book(payment=self.deposit(), **self.slot(3)).json()["appointment"]
        with self.on(3, hour=10):
            self.to_the_room(visit["id"])
            self.record(visit["id"], braces)
            self.assertEqual(self.apply(self.bill(visit["id"]), fifty).status_code, 200)
            bill = self.bill(visit["id"])
            self.assertEqual((bill["subtotal"], bill["discount"], bill["total"], bill["paid"], bill["due"]),
                             (800_00, 50_00, 750_00, DEPOSIT, 650_00))
            self.assertEqual(self.pay(visit["id"], 650_00).status_code, 201)
            self.assertEqual(self.figures(visit["id"])["due"], 0)

    def test_a_bill_under_the_deposit_gives_back_only_what_is_over_the_deposit(self):
        # Saged, 2026-11-26: the file shows what give-back returns, and nothing is left after it.
        with self.on(0):
            self.deposit_on()
            most = self.kind("Most", "fixed", CONSULT - 50_00)
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            self.assertEqual(self.pay(visit["id"], CONSULT - DEPOSIT).status_code, 201)
            self.assertEqual(self.apply(self.bill(visit["id"]), most).status_code, 200)
            bill = self.bill(visit["id"])
            page = self.owner.get(f"/patients/{visit['patient']['id']}").content.decode()
            self.assertIn(f"give_back={bill['id']}", page)
            r = call(self.owner, "POST", f"/api/v1/invoices/{bill['id']}/give-back", {"method": "card"})
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(self.figures(visit["id"])["paid"], DEPOSIT)
            page = self.owner.get(f"/patients/{visit['patient']['id']}").content.decode()
            self.assertNotIn(f"give_back={bill['id']}", page)

    def test_a_discount_may_take_the_bill_under_the_deposit_and_under_other_payments(self):
        with self.on(0):
            self.deposit_on()
            all_of_it = self.kind("Free", "percent", 100)
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            self.assertEqual(self.apply(self.bill(visit["id"]), all_of_it).status_code, 200)
            self.assertEqual(self.figures(visit["id"]), {"total": 0, "paid": DEPOSIT, "due": 0, "credit": DEPOSIT,
                                                         "deposit": DEPOSIT})
            self.assertEqual(self.apply(self.bill(visit["id"]), None).status_code, 200)
            self.assertEqual(self.pay(visit["id"], 200_00).status_code, 201)  # the rest of the EGP 300
            # Under other payments too now (Saged, 2026-11-26): what was paid over waits to be given back.
            r = self.apply(self.bill(visit["id"]), all_of_it)
            self.assertEqual(r.status_code, 200, r.content)
            figures = self.figures(visit["id"])
            self.assertEqual((figures["total"], figures["paid"], figures["due"]), (0, CONSULT, 0))


class CreditTests(DepositTestCase):
    """The deposit is more than the final total: a credit, never a negative due, and no piastre lost."""

    def test_the_bill_shows_a_credit_and_the_desk_refunds_the_difference(self):
        with self.on(0):
            self.deposit_on()
            patient, session, braces, scaling = self.plan_session("none", procedure=False)
            call(self.owner, "PATCH", f"/api/v1/procedures/{scaling['id']}", {"changes": {"price": 60_00}})
            visit = self.book(patient_id=patient, follow_up_id=session["id"], payment=self.deposit(), **self.slot(3)).json()["appointment"]
            self.assertEqual((visit["due"], visit["invoice"]["credit"]), (0, DEPOSIT))
        with self.on(3, hour=10) as day:
            self.to_the_room(visit["id"])
            self.record(visit["id"], scaling)
            self.assertEqual(self.figures(visit["id"]), {"total": 60_00, "paid": DEPOSIT, "due": 0, "credit": 40_00,
                                                         "deposit": DEPOSIT})
            r = self.pay(visit["id"], 1)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))
            r = self.act(visit["id"], "finish", "in_room")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual((r.json()["invoice"]["remaining"], r.json()["invoice"]["credit"], r.json()["appointment"]["due"]),
                             (0, 40_00, 0))
            self.assertEqual(r.json()["balance"]["owed"], 0)
            deposit = self.bill(visit["id"])["payments"][0]
            over = self.refund(deposit["id"], DEPOSIT + 1)
            self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "over_refund"))
            r = self.refund(deposit["id"], 40_00)
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(self.figures(visit["id"]), {"total": 60_00, "paid": 60_00, "due": 0, "credit": 0, "deposit": 60_00})
            self.assertEqual(self.day_money(day), (0, 40_00))
            self.assertEqual(self.drawer(), (DEPOSIT, 40_00))  # EGP 60 left in the drawer: what the visit cost
        MoneyInvariantTests.check_invariants(self, [DAY, day])

    def test_a_line_may_be_removed_under_the_deposit_but_not_under_other_payments(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            bill = self.bill(visit["id"])
            r = call(self.desk, "DELETE", f"/api/v1/invoices/{bill['id']}/lines/{bill['lines'][0]['id']}")
            self.assertEqual(r.status_code, 200, r.content)  # only the deposit is on it: a credit of EGP 100
            self.assertEqual(self.figures(visit["id"]), {"total": 0, "paid": DEPOSIT, "due": 0, "credit": DEPOSIT,
                                                         "deposit": DEPOSIT})
            other = self.book(payment=self.deposit(), doctor_id=self.owner_id,
                              new_patient=new_patient(full_name="Someone Else", phone="01055555555")).json()["appointment"]
            self.assertEqual(self.pay(other["id"], 200_00).status_code, 201)
            bill = self.bill(other["id"])
            r = call(self.desk, "DELETE", f"/api/v1/invoices/{bill['id']}/lines/{bill['lines'][0]['id']}")
            self.assertEqual(r.status_code, 200, r.content)  # what was paid stays as the patient's credit (Saged, 2026-11-22)
            self.assertEqual(r.json()["invoice"]["credit"], CONSULT)

    def test_the_database_refuses_more_than_the_total_without_a_deposit(self):
        from django.db import IntegrityError, transaction

        with self.on(0):
            visit = self.book(payment={"amount": CONSULT, "method": "cash"}, doctor_id=self.owner_id).json()["appointment"]
        # More paid than the total is allowed (a credit, Saged 2026-11-22); a negative paid or deposit never is.
        for change in ({"paid": -1}, {"deposit": 1, "paid": 0}, {"deposit": -1}):
            with self.assertRaises(IntegrityError), clinic_transaction(self.a.id, self.a.shard), \
                    transaction.atomic(using=self.a.shard):
                Invoice.objects.filter(clinic_id=self.a.id, id=visit["invoice"]["id"]).update(**change)


class NoShowTests(DepositTestCase):
    def test_a_no_show_keeps_the_deposit_owes_nothing_and_the_desk_refunds_it_by_hand(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            self.assertEqual(self.figures(visit["id"])["due"], 200_00)
            r = self.act(visit["id"], "no-show", "booked")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.json()["appointment"]["due"], 0)
            self.assertEqual(self.figures(visit["id"]), {"total": 0, "paid": DEPOSIT, "due": 0, "credit": DEPOSIT,
                                                         "deposit": DEPOSIT})
            self.assertEqual(self.appointment(visit["id"]).fee, CONSULT)  # what the visit would have cost stays on it
            balance = call(self.owner, "GET", f"/api/v1/patients/{visit['patient']['id']}/balance")
            self.assertEqual(balance.status_code, 200, balance.content)
            self.assertEqual(balance.json()["balance"]["owed"], 0)
            self.assertEqual(self.receivables(), 0)
            self.assertEqual((self.day_money(DAY), self.drawer()), ((DEPOSIT, 0), (DEPOSIT, 0)))  # kept
            # Undo puts the fee back; marking it again takes it off again.
            self.assertEqual(self.act(visit["id"], "undo-no-show", "no_show").status_code, 200)
            self.assertEqual(self.figures(visit["id"]), {"total": CONSULT, "paid": DEPOSIT, "due": 200_00, "credit": 0,
                                                         "deposit": DEPOSIT})
            self.assertEqual(self.act(visit["id"], "no-show", "booked").status_code, 200)
            deposit = self.bill(visit["id"])["payments"][0]
            # The clinic keeps a no-show's deposit unless the owner's switch lets it go back (Saged, 2026-11-26).
            r = self.refund(deposit["id"], DEPOSIT)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_refundable"))
            self.assertIn("ما جاش", r.json()["error"]["message"])
            self.assertEqual(call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"deposit_back_no_show": True},
                                                                                  "expected": {}}).status_code, 200)
            self.assertEqual(self.refund(deposit["id"], DEPOSIT).status_code, 201)
            self.assertEqual(self.figures(visit["id"]), {"total": 0, "paid": 0, "due": 0, "credit": 0, "deposit": 0})
            self.assertEqual((self.day_money(DAY), self.drawer()), ((DEPOSIT, DEPOSIT), (DEPOSIT, DEPOSIT)))
            self.assertEqual(self.refund(deposit["id"], 1).status_code, 409)  # nothing is refunded twice
        MoneyInvariantTests.check_invariants(self, [DAY])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditChange.objects.filter(clinic_id=self.a.id, action="invoice.visit_fee").count(), 3)

    def receivables(self):
        from finance.selectors import receivables

        with clinic_transaction(self.a.id, self.a.shard):
            return receivables(system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard), 10, None)[1]["owed"]

    def test_the_nightly_sweep_keeps_the_deposit_too(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            unpaid = self.book(doctor_id=self.owner_id, new_patient=new_patient(full_name="No Deposit", phone="01055555555")).json()["appointment"]
        counts = sweep_clinic(self.a.id, self.a.shard, dt.datetime(2026, 11, 11, 4, 30, tzinfo=CAIRO))
        self.assertEqual(counts["no_show"], 2)
        with self.on(1):
            self.assertEqual(self.appointment(visit["id"]).status, "no_show")
            self.assertEqual(self.figures(visit["id"]), {"total": 0, "paid": DEPOSIT, "due": 0, "credit": DEPOSIT,
                                                         "deposit": DEPOSIT})
            self.assertIsNone(self.bill(unpaid["id"]))
            self.assertEqual(self.receivables(), 0)
            self.assertEqual((self.day_money(DAY), self.day_money(DAY + dt.timedelta(days=1))), ((DEPOSIT, 0), (0, 0)))
        MoneyInvariantTests.check_invariants(self, [DAY, DAY + dt.timedelta(days=1)])

    def test_a_no_show_that_paid_more_than_the_deposit_keeps_its_bill(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            self.assertEqual(self.pay(visit["id"], 200_00).status_code, 201)
            self.assertEqual(self.act(visit["id"], "no-show", "booked").status_code, 200)
            self.assertEqual(self.figures(visit["id"]), {"total": CONSULT, "paid": CONSULT, "due": 0, "credit": 0,
                                                         "deposit": DEPOSIT})
            # With the owner's switch on, only the deposit goes back; the other payment stays (Saged, 2026-11-26).
            self.assertEqual(call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"deposit_back_no_show": True},
                                                                                  "expected": {}}).status_code, 200)
            deposit, other = self.bill(visit["id"])["payments"][:2]
            self.assertTrue(deposit["deposit"])
            r = self.refund(other["id"], 100)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_refundable"))
            self.assertEqual(self.refund(deposit["id"], DEPOSIT).status_code, 201)


class MoveAndCancelTests(DepositTestCase):
    def test_a_reschedule_keeps_the_deposit_on_the_bill_and_on_the_day_it_was_taken(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id, day=(DAY + dt.timedelta(days=3)).isoformat()).json()["appointment"]
        with self.on(1):
            r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/reschedule",
                     {"status": "booked", "doctor_id": self.owner_id, "day": (DAY + dt.timedelta(days=5)).isoformat()})
            self.assertEqual(r.status_code, 200, r.content)
            moved = r.json()["appointment"]
            self.assertEqual((moved["day"], moved["invoice"]["id"], moved["invoice"]["paid"], moved["due"]),
                             ((DAY + dt.timedelta(days=5)).isoformat(), visit["invoice"]["id"], DEPOSIT, 200_00))
            payments = self.rows(Payment)
            self.assertEqual([(p["amount"], p["day"], p["is_deposit"]) for p in payments], [(DEPOSIT, DAY, True)])
            for offset, money in ((0, (DEPOSIT, 0)), (1, (0, 0)), (3, (0, 0)), (5, (0, 0))):
                self.assertEqual(self.day_money(DAY + dt.timedelta(days=offset)), money, offset)
            self.assertEqual(self.drawer(), (DEPOSIT, 0))
        with self.on(5, hour=10):
            self.assertEqual(self.act(visit["id"], "arrive", "booked").status_code, 200)
            self.assertEqual(self.figures(visit["id"]), {"total": CONSULT, "paid": DEPOSIT, "due": 200_00, "credit": 0,
                                                         "deposit": DEPOSIT})
        MoneyInvariantTests.check_invariants(self, [DAY + dt.timedelta(days=d) for d in (0, 1, 3, 5)])

    def test_a_cancel_is_refused_until_the_deposit_is_refunded_by_hand(self):
        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id, day=(DAY + dt.timedelta(days=3)).isoformat()).json()["appointment"]
        with self.on(1) as day:
            r = self.act(visit["id"], "cancel", "booked", reason="patient called")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "refund_first"))
            self.assertEqual(self.appointment(visit["id"]).status, "booked")
            self.assertEqual(self.figures(visit["id"])["paid"], DEPOSIT)  # nothing lost
            deposit = self.bill(visit["id"])["payments"][0]
            self.assertEqual(self.refund(deposit["id"], DEPOSIT).status_code, 201)
            self.assertEqual(self.act(visit["id"], "cancel", "booked", reason="patient called").status_code, 200)
            self.assertEqual(self.act(visit["id"], "cancel", "booked").status_code, 200)  # a repeat
            invoice = self.rows(Invoice, id=visit["invoice"]["id"])[0]
            self.assertEqual((invoice["status"], invoice["paid"], invoice["deposit"]), ("void", 0, 0))
            self.assertEqual([(p["kind"], p["amount"], p["day"]) for p in self.rows(Payment)],
                             [("payment", DEPOSIT, DAY), ("refund", DEPOSIT, day)])  # nothing duplicated
            self.assertEqual((self.day_money(DAY), self.day_money(day)), ((DEPOSIT, 0), (0, DEPOSIT)))
            self.assertEqual(self.drawer(), (DEPOSIT, DEPOSIT))
        MoneyInvariantTests.check_invariants(self, [DAY, day])


class ArrivalTests(DepositTestCase):
    """The follow-up rule is checked again at arrival; a plan session keeps its step's fee."""

    def test_a_plan_session_keeps_its_fee_at_arrival_with_or_without_a_deposit(self):
        for visit_price, fee, is_follow_up in (("none", 0, True), ("follow_up", FOLLOW, True), ("consultation", CONSULT, False)):
            for deposit in (False, True):
                with self.subTest(visit_price=visit_price, deposit=deposit):
                    with self.on(0):
                        self.deposit_on()
                        patient, session, _braces, _scaling = self.plan_session(visit_price)
                        body = {"patient_id": patient, "follow_up_id": session["id"], **self.slot(3)}
                        if deposit:
                            body["payment"] = self.deposit()
                        visit = self.book(**body).json()["appointment"]
                        self.assertEqual((visit["fee"], visit["is_follow_up"]), (fee, is_follow_up))
                    with self.on(3, hour=10):
                        self.assertEqual(self.act(visit["id"], "arrive", "booked").status_code, 200)
                        row = self.appointment(visit["id"])
                        self.assertEqual((row.fee, row.is_follow_up, row.visit_price), (fee, is_follow_up, visit_price))
                        if deposit:
                            self.assertEqual(self.figures(visit["id"])["total"], fee)
                        # Back to booked and in again: still the step's fee.
                        self.assertEqual(self.act(visit["id"], "undo-arrive", "waiting").status_code, 200)
                        self.assertEqual(self.act(visit["id"], "arrive", "booked").status_code, 200)
                        self.assertEqual(self.appointment(visit["id"]).fee, fee)
                        self.assertEqual(self.act(visit["id"], "undo-arrive", "waiting").status_code, 200)
                        self.assertEqual(self.act(visit["id"], "no-show", "booked").status_code, 200)  # frees the doctor's slot

    def test_a_follow_up_price_changed_meanwhile_is_the_one_charged(self):
        with self.on(0):
            patient, session, _braces, _scaling = self.plan_session("follow_up")
            visit = self.book(patient_id=patient, follow_up_id=session["id"], **self.slot(3)).json()["appointment"]
            with clinic_transaction(self.a.id, self.a.shard):
                Service.objects.filter(clinic_id=self.a.id, kind="follow_up").update(price=175_00)
        with self.on(3, hour=10):
            self.assertEqual(self.act(visit["id"], "arrive", "booked").status_code, 200)
            self.assertEqual(self.appointment(visit["id"]).fee, 175_00)

    def test_a_deposit_booking_that_became_a_follow_up_is_charged_as_one(self):
        with self.on(0):
            self.deposit_on()
            first = self.book(doctor_id=self.owner_id).json()["appointment"]  # not arrived yet: no window
            patient = first["patient"]["id"]
            later = self.book(patient_id=patient, doctor_id=self.owner_id, day=(DAY + dt.timedelta(days=3)).isoformat(),
                              payment=self.deposit()).json()["appointment"]
            self.assertEqual((later["fee"], later["is_follow_up"], later["due"]), (CONSULT, False, 200_00))
            self.assertEqual(self.act(first["id"], "arrive", "booked").status_code, 200)  # no window any more
        with self.on(3, hour=10):
            self.assertEqual(self.act(later["id"], "arrive", "booked").status_code, 200)
            row = self.appointment(later["id"])
            # No follow-up window (Saged, 2026-11-23): the visit keeps its own price.
            self.assertEqual((row.fee, row.is_follow_up), (CONSULT, False))
            self.assertEqual(self.figures(later["id"]), {"total": CONSULT, "paid": DEPOSIT, "due": CONSULT - DEPOSIT,
                                                         "credit": 0, "deposit": DEPOSIT})
            self.assertEqual([line["amount"] for line in self.bill(later["id"])["lines"]], [CONSULT])

    def test_a_fee_paid_beyond_the_deposit_is_not_recomputed(self):
        with self.on(0):
            self.deposit_on()
            first = self.book(doctor_id=self.owner_id).json()["appointment"]
            later = self.book(patient_id=first["patient"]["id"], doctor_id=self.owner_id,
                              day=(DAY + dt.timedelta(days=3)).isoformat(), payment=self.deposit()).json()["appointment"]
            self.assertEqual(self.pay(later["id"], 200_00).status_code, 201)
            self.assertEqual(self.act(first["id"], "arrive", "booked").status_code, 200)
        with self.on(3, hour=10):
            self.assertEqual(self.act(later["id"], "arrive", "booked").status_code, 200)
            self.assertEqual(self.appointment(later["id"]).fee, CONSULT)
            self.assertEqual(self.figures(later["id"]), {"total": CONSULT, "paid": CONSULT, "due": 0, "credit": 0,
                                                         "deposit": DEPOSIT})


class FigureTests(DepositTestCase):
    """The drawer, Overview and Finances count the deposit once, on the day it was taken."""

    def test_every_figure_counts_it_once_on_its_own_day(self):
        from finance.selectors import revenue
        from reports.selectors import performance
        from summaries.models import MoneyDay

        with self.on(0):
            self.deposit_on()
            visit = self.book(payment=self.deposit(method="card"), **self.slot(3)).json()["appointment"]
            cash = self.book(payment=self.deposit(), doctor_id=self.owner_id, day=(DAY + dt.timedelta(days=3)).isoformat(),
                             new_patient=new_patient(full_name="Cash Payer", phone="01055555555")).json()["appointment"]
            self.assertEqual(self.drawer(), (DEPOSIT, 0))  # the card deposit isn't cash
        with self.on(3, hour=10) as day:
            self.to_the_room(visit["id"])
            r = self.act(visit["id"], "finish", "in_room", payment={"amount": 200_00, "method": "cash"})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(self.act(cash["id"], "arrive", "booked").status_code, 200)
        clinic0, doctors0 = summaries(self.a, DAY)
        clinic3, doctors3 = summaries(self.a, day)
        self.assertEqual((clinic0["collected"], clinic3["collected"]), (2 * DEPOSIT, 200_00))
        self.assertEqual((doctors0[self.doctor_id]["collected"], doctors0[self.owner_id]["collected"]), (DEPOSIT, DEPOSIT))
        self.assertEqual(doctors3[self.doctor_id]["collected"], 200_00)
        with clinic_transaction(self.a.id, self.a.shard):
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            by_day = {i["key"]: i["net"] for i in revenue(ctx, DAY, day, "day")["items"]}
            by_method = {i["key"]: i["net"] for i in revenue(ctx, DAY, day, "method")["items"]}
            month = performance(ctx, DAY, day, "month")
            money = {(m["day"], m["method"]): m["collected"] for m in MoneyDay.objects.filter(clinic_id=self.a.id).values()}
        self.assertEqual(by_day, {DAY.isoformat(): 2 * DEPOSIT, day.isoformat(): 200_00})
        self.assertEqual(by_method, {"card": DEPOSIT, "cash": DEPOSIT + 200_00})
        self.assertEqual(money, {(DAY, "card"): DEPOSIT, (DAY, "cash"): DEPOSIT, (day, "cash"): 200_00})
        self.assertEqual([(m["collected"], m["revenue"]) for m in month], [(400_00, 400_00)])
        self.assertEqual(self.drawer(), (DEPOSIT + 200_00, 0))
        MoneyInvariantTests.check_invariants(self, [DAY, day])

    def test_a_deposit_with_no_open_shift_opens_one_for_cash(self):
        from finance.models import Shift

        from django.db import connections

        with clinic_transaction(self.a.id, self.a.shard), connections[self.a.shard].cursor() as cursor:
            cursor.execute('DELETE FROM "finance_shift" WHERE clinic_id = %s', [self.a.id])
        self.assertEqual(self.rows(Shift), [])
        with self.on(0):
            self.deposit_on()
            self.book(payment=self.deposit(), **self.slot(3))
            self.assertEqual(self.drawer(), (DEPOSIT, 0))
            self.assertIsNotNone(self.rows(Payment)[0]["shift_id"])


class OnceTests(DepositTestCase):
    def test_a_double_clicked_book_takes_the_deposit_once(self):
        with self.on(0):
            self.deposit_on()
            key = "7d0c6f0e-3c58-4c4b-9d5e-2f6f1c1d7a55"
            body = {"service_id": self.services_a["consultation"].id, "payment": self.deposit(), **self.slot(3),
                    "new_patient": new_patient()}
            first = call(self.desk, "POST", "/api/v1/appointments", body, key=key)
            second = call(self.desk, "POST", "/api/v1/appointments", body, key=key)
            self.assertEqual((first.status_code, second.status_code), (201, 201))
            self.assertEqual(first.json()["appointment"]["id"], second.json()["appointment"]["id"])
            self.assertEqual(second.json()["appointment"]["invoice"]["paid"], DEPOSIT)
            self.assertEqual(len(self.rows(Appointment)), 1)
            self.assertEqual([(p["amount"], p["is_deposit"]) for p in self.rows(Payment)], [(DEPOSIT, True)])
            self.assertEqual((self.day_money(DAY), self.drawer()), ((DEPOSIT, 0), (DEPOSIT, 0)))
            # A new key for the same body is a second booking: the same new patient on the same number is refused as
            # a duplicate file (Saged, 2026-11-17) before the slot is even tried, and no money moves.
            third = call(self.desk, "POST", "/api/v1/appointments", body)
            self.assertEqual((third.status_code, third.json()["error"]["code"]), (409, "duplicate_patient"))
            self.assertEqual(len(self.rows(Payment)), 1)
            self.assertEqual(self.day_money(DAY), (DEPOSIT, 0))

    def test_a_plan_session_booked_twice_takes_one_deposit(self):
        with self.on(0):
            self.deposit_on()
            patient, session, _braces, _scaling = self.plan_session("none")
            body = {"patient_id": patient, "follow_up_id": session["id"], "payment": self.deposit()}
            self.assertEqual(self.book(**body, **self.slot(3)).status_code, 201)
            before = len(self.rows(Payment))
            r = self.book(expect=None, **body, **self.slot(4, "10:20"))  # another day: the session itself refuses
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "follow_up_booked"))
            self.assertEqual(len(self.rows(Payment)), before)  # refused whole: no second deposit
            self.assertEqual(len(self.rows(Payment, is_deposit=True)), 1)


class RefusalTests(DepositTestCase):
    def test_without_billing_collect_there_is_no_deposit(self):
        with self.on(0):
            self.deposit_on()
            self.set_codes("assistant", revoked=["billing.collect"])
            self.desk = self.a.client("assistant")
            r = self.book(expect=None, payment=self.deposit(), **self.slot(3))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"))
            self.assertEqual((self.rows(Appointment), self.rows(Payment), self.rows(Invoice)), ([], [], []))
            self.assertEqual(self.book(**self.slot(3)).status_code, 201)  # the booking itself still works
            self.assertNotIn('"deposit": 10000', self.desk.get("/").content.decode())

    def test_the_server_decides_whether_and_how_much(self):
        with self.on(0):
            off = self.book(expect=None, payment=self.deposit(), **self.slot(3))
            self.assertEqual((off.status_code, off.json()["error"]["code"]), (409, "deposit_off"))
            self.deposit_on()
            for amount in (DEPOSIT - 1, DEPOSIT + 1, 1):
                r = self.book(expect=None, payment=self.deposit(amount), **self.slot(3))
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "deposit_changed"), amount)
                self.assertEqual(r.json()["error"]["amount"], DEPOSIT)
            for bad in ({"amount": DEPOSIT, "method": "cash", "deposit": "yes"}, {"amount": 0, "method": "cash", "deposit": True},
                        {"amount": DEPOSIT, "method": "gold", "deposit": True}, {"method": "cash", "deposit": True}):
                self.assertEqual(self.book(expect=None, payment=bad, **self.slot(3)).status_code, 400, bad)
            self.assertEqual((self.rows(Appointment), self.rows(Payment)), ([], []))
            # An ordinary upfront payment keeps its rules: today only, and never more than the fee.
            r = self.book(expect=None, payment={"amount": DEPOSIT, "method": "cash"}, **self.slot(3))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "payment_not_today"))
            r = self.book(expect=None, payment={"amount": CONSULT + 1, "method": "cash"}, doctor_id=self.owner_id)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))

    def test_a_walk_in_and_a_finish_take_no_deposit(self):
        with self.on(0):
            self.deposit_on()
            body = {"new_patient": new_patient(full_name="Walk In", phone="01055555555"), "doctor_id": self.owner_id,
                    "payment": self.deposit()}
            r = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
            self.assertEqual(r.status_code, 400, r.content)
            visit = self.in_room()
            r = self.act(visit["id"], "finish", "in_room", payment=self.deposit())
            self.assertEqual(r.status_code, 400, r.content)
            r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", self.deposit())
            self.assertEqual(r.status_code, 201, r.content)  # the Pay endpoint knows no deposit: an ordinary payment
            self.assertEqual([p["is_deposit"] for p in self.rows(Payment)], [False])

    def test_a_patient_here_now_takes_no_deposit_but_may_pay_as_before(self):
        # "Here now" in Book is a walk-in: the patient pays at the visit. A deposit is only money taken before it.
        with self.on(0):
            self.deposit_on()
            r = self.book(expect=None, payment=self.deposit(), doctor_id=self.owner_id, arrive=True)
            self.assertEqual((r.status_code, r.json()["error"]["fields"]), (400, {"payment.deposit": "here_now"}))
            self.assertEqual((self.rows(Appointment), self.rows(Payment)), ([], []))
            r = self.book(expect=None, payment={"amount": DEPOSIT, "method": "cash"}, doctor_id=self.owner_id, arrive=True)
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(self.figures(r.json()["appointment"]["id"]),
                             {"total": CONSULT, "paid": DEPOSIT, "due": 200_00, "credit": 0, "deposit": 0})
            self.assertEqual([p["is_deposit"] for p in self.rows(Payment)], [False])


class IsolationTests(DepositTestCase):
    def test_one_clinics_setting_and_money_never_reach_another(self):
        with self.on(0):
            self.deposit_on(fixture=self.b)  # B asks for deposits; A doesn't
            r = self.book(expect=None, payment=self.deposit(), **self.slot(3))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "deposit_off"))
            b_desk = self.b.client("assistant")
            theirs = self.book(client=b_desk, fixture=self.b, payment=self.deposit(),
                               day=(DAY + dt.timedelta(days=3)).isoformat()).json()["appointment"]
            self.assertEqual(theirs["invoice"]["paid"], DEPOSIT)
            payment = call(b_desk, "GET", f"/api/v1/invoices/{theirs['invoice']['id']}").json()["invoice"]["payments"][0]
            # A's desk (with billing.refund) can't see, refund, cancel or mark B's visit.
            self.assertEqual(self.refund(payment["id"], DEPOSIT).status_code, 404)
            self.assertEqual(call(self.desk, "GET", f"/api/v1/invoices/{theirs['invoice']['id']}").status_code, 404)
            self.assertEqual(self.act(theirs["id"], "no-show", "booked").status_code, 404)
            self.assertEqual(self.act(theirs["id"], "cancel", "booked").status_code, 404)
            self.assertEqual((self.rows(Payment), self.day_money(DAY)), ([], (0, 0)))
            with clinic_transaction(self.b.id, self.b.shard):
                found = Payment.objects.get(clinic_id=self.b.id, id=payment["id"])
                self.assertEqual((found.amount, found.refunded, found.is_deposit), (DEPOSIT, 0, True))

    def test_another_clinics_plan_session_is_not_found_and_nothing_is_booked(self):
        with self.on(0):
            self.deposit_on()
            self.deposit_on(fixture=self.b)
            patient, session, _braces, _scaling = self.plan_session("none")
            b_desk = self.b.client("assistant")
            theirs = self.book(client=b_desk, fixture=self.b).json()["appointment"]
            r = self.book(client=b_desk, fixture=self.b, expect=None, patient_id=theirs["patient"]["id"],
                          follow_up_id=session["id"], payment=self.deposit(), day=(DAY + dt.timedelta(days=3)).isoformat())
            self.assertEqual(r.status_code, 404, r.content)
            with clinic_transaction(self.b.id, self.b.shard):
                self.assertEqual(Payment.objects.filter(clinic_id=self.b.id).count(), 0)
                self.assertEqual(Appointment.objects.filter(clinic_id=self.b.id).count(), 1)
            r = call(b_desk, "GET", f"/api/v1/appointments/quote?doctor={self.b.membership('owner').id}"
                                    f"&service={self.services_b['consultation'].id}&patient={theirs['patient']['id']}"
                                    f"&follow_up={session['id']}")
            self.assertEqual(r.status_code, 404, r.content)
            self.assertEqual(self.rows(PlanItem, id=session["id"])[0]["appointment_id"], None)


class RandomDepositTests(DepositTestCase):
    """Random sequences with deposits, plan sessions, discounts, refunds, no-shows and removals over three days:
    nothing answers 500, no bill owes a negative amount, and the money adds up everywhere."""

    def test_random_sequences(self):
        rng = random.Random(20261116)
        days = [DAY + dt.timedelta(days=d) for d in range(3)]
        with self.on(0):
            self.deposit_on()
            braces, scaling = self.prices()
            types = [call(self.owner, "POST", "/api/v1/discount-types", body).json()["discount_type"] for body in (
                {"name": "Ten", "kind": "percent", "value": 10}, {"name": "All", "kind": "percent", "value": 100},
                {"name": "Fifty", "kind": "fixed", "value": 50_00})]
        visits, done, room = [], {}, set()
        for step in range(210):
            offset = min(step // 70, 2)
            with self.on(offset):
                choice = rng.choice(["book", "book", "pay", "refund", "discount", "remove", "line", "no_show", "undo",
                                     "cancel", "arrive", "call", "record", "finish", "reschedule"])
                target = rng.choice(visits) if visits else None
                r = None
                if choice == "book" or target is None:
                    body = {"doctor_id": self.owner_id, "day": (DAY + dt.timedelta(days=offset + rng.randint(0, 1))).isoformat(),
                            "new_patient": new_patient(full_name=f"Patient {step}", phone=f"010{step:08d}")}
                    if rng.random() < 0.8:
                        body["payment"] = self.deposit(method=rng.choice(["cash", "card"]))
                    r = self.book(expect=None, **body)
                    if r.status_code == 201:
                        visits.append(r.json()["appointment"]["id"])
                elif choice == "pay":
                    r = self.pay(target, rng.randint(1, 400_00))
                elif choice in ("refund", "discount", "remove", "line"):
                    bill = self.bill(target)
                    if bill is None:
                        continue
                    if choice == "refund":
                        open_ = [p for p in bill["payments"] if p["kind"] == "payment" and p["refunded"] < p["amount"]]
                        if open_:
                            p = rng.choice(open_)
                            r = self.refund(p["id"], rng.randint(1, p["amount"] - p["refunded"]))
                    elif choice == "discount":
                        kind = rng.choice([*types, None])
                        r = call(self.desk, "PUT", f"/api/v1/invoices/{bill['id']}/discount-type",
                                 {"type_id": kind["id"] if kind else None, "expected_discount": bill["discount"]})
                    elif choice == "remove" and bill["lines"]:
                        r = call(self.desk, "DELETE", f"/api/v1/invoices/{bill['id']}/lines/{rng.choice(bill['lines'])['id']}")
                    elif choice == "line":
                        r = call(self.desk, "POST", f"/api/v1/invoices/{bill['id']}/lines",
                                 {"kind": "procedure", "service_id": self.services_a["procedure"].id})
                elif choice == "no_show":
                    r = self.act(target, "no-show", "booked")
                elif choice == "undo":
                    r = self.act(target, "undo-no-show", "no_show")
                elif choice == "cancel":
                    r = self.act(target, "cancel", "booked")
                    if r.status_code == 409 and r.json()["error"]["code"] == "refund_first":
                        # The desk refunds by hand what was paid, then cancels.
                        for p in self.bill(target)["payments"]:
                            if p["kind"] == "payment" and p["refunded"] < p["amount"]:
                                self.assertEqual(self.refund(p["id"], p["amount"] - p["refunded"]).status_code, 201)
                        r = self.act(target, "cancel", "booked")
                        self.assertEqual(r.status_code, 200, r.content)
                elif choice == "reschedule":
                    r = call(self.desk, "POST", f"/api/v1/appointments/{target}/reschedule",
                             {"status": "booked", "doctor_id": self.owner_id, "day": (DAY + dt.timedelta(days=offset + 1)).isoformat()})
                elif choice == "record":
                    if room:
                        r = call(self.owner, "POST", f"/api/v1/appointments/{min(room)}/procedures",
                                 {"procedure_id": rng.choice([braces, scaling])["id"]})
                else:
                    if choice == "finish" and room:
                        target = min(room)
                    source = {"arrive": "booked", "call": "waiting", "finish": "in_room"}[choice]
                    r = self.act(target, choice, source)
                    if r.status_code < 300:
                        (room.add if choice == "call" else room.discard)(target)
                if r is not None:
                    self.assertLess(r.status_code, 500, (choice, r.content))
                    if r.status_code < 300:
                        done[choice] = done.get(choice, 0) + 1
        for choice in ("book", "pay", "refund", "discount", "remove", "line", "no_show", "undo", "cancel", "arrive", "call",
                       "record", "finish", "reschedule"):
            self.assertGreater(done.get(choice, 0), 0, (choice, done))
        MoneyInvariantTests.check_invariants(self, days + [DAY + dt.timedelta(days=3)])
        payments = self.rows(Payment)
        for invoice in self.rows(Invoice):
            held = sum(p["amount"] - p["refunded"] for p in payments if p["invoice_id"] == invoice["id"] and p["is_deposit"])
            self.assertEqual(invoice["deposit"], held, invoice)  # the deposit held = deposit payments less their refunds
            # More paid than the total is a credit now (a line removed after paying, Saged 2026-11-22).
            self.assertGreaterEqual(invoice["paid"], invoice["deposit"], invoice)
        with self.on(2):
            for visit_id in visits:
                visit = call(self.desk, "GET", f"/api/v1/appointments/{visit_id}").json()["appointment"]
                self.assertGreaterEqual(visit["due"], 0, visit)
                if visit["invoice"]:
                    self.assertEqual(visit["due"] - visit["invoice"]["credit"],
                                     (visit["invoice"]["total"] - visit["invoice"]["paid"]) if visit["status"] != "cancelled" else 0, visit)


class ScreenTests(DepositTestCase):
    """What the screens are handed: the deposit to offer in Book, and the plan session's procedure in the visit."""

    def test_book_offers_the_clinics_deposit_only_to_who_can_collect(self):
        with self.on(0):
            self.assertIn('"deposit": 0', self.desk.get("/").content.decode())  # off
            self.deposit_on()
            self.assertIn('"deposit": 10000', self.a.client("assistant").get("/").content.decode())

    def test_the_visit_offers_the_sessions_procedure_in_one_tap_and_records_it_against_the_plan(self):
        with self.on(0):
            patient, session, braces, _scaling = self.plan_session("none")
            visit = self.book(patient_id=patient, follow_up_id=session["id"], **self.slot(3)).json()["appointment"]
        with self.on(3, hour=10):
            self.to_the_room(visit["id"])
            html = self.doc.get(f"/visits/{visit['id']}").content.decode()
            self.assertIn(f'data-plan-item="{session["id"]}"', html)
            self.assertIn(f'"plan_offer": [{{"plan_item_id": {session["id"]}, "procedure_id": {braces["id"]}}}]', html)
            self.record(visit["id"], braces, plan_item_id=session["id"])
            html = self.doc.get(f"/visits/{visit['id']}").content.decode()
            self.assertNotIn("data-plan-item=", html)  # done: no longer offered
            self.assertEqual(self.figures(visit["id"])["total"], BRACES)  # no visit fee, the braces at its price

    def test_a_step_without_a_procedure_offers_nothing(self):
        with self.on(0):
            patient, session, _braces, _scaling = self.plan_session("follow_up", procedure=False)
            visit = self.book(patient_id=patient, follow_up_id=session["id"], **self.slot(3)).json()["appointment"]
        with self.on(3, hour=10):
            self.to_the_room(visit["id"])
            self.assertNotIn("data-plan-item=", self.doc.get(f"/visits/{visit['id']}").content.decode())


class RecipeTests(DepositTestCase):
    def test_a_step_keeps_its_visit_price_and_procedure_and_bad_ones_are_refused(self):
        with self.on(0):
            braces, scaling = self.prices()
            url = f"/api/v1/procedures/{braces['id']}"
            step = {"days": 30, "label": "شد", "visit_price": "follow_up", "procedure_id": braces["id"]}
            r = call(self.owner, "PATCH", url, {"changes": {"follow_up": [step, {"days": 60, "label": "تاني"}]}})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.json()["procedure"]["follow_up"], [
                {"days": 30, "label": "شد", "label_en": "", "visit_price": "follow_up", "procedure_id": braces["id"]},
                {"days": 60, "label": "تاني", "label_en": "", "visit_price": "none", "procedure_id": None}])
            for bad in ({"visit_price": "free"}, {"visit_price": 1}, {"procedure_id": "x"}, {"procedure_id": -1}):
                r = call(self.owner, "PATCH", url, {"changes": {"follow_up": [{**step, **bad}]}})
                self.assertEqual(r.status_code, 400, (bad, r.content))
            theirs = next(p for p in self.catalogue(self.b.client("owner")))
            r = call(self.owner, "PATCH", url, {"changes": {"follow_up": [{**step, "procedure_id": theirs["id"]}]}})
            self.assertEqual(r.status_code, 404, r.content)  # another clinic's procedure is not found
            kept = next(p for p in self.catalogue(self.owner) if p["id"] == braces["id"])
            self.assertEqual(kept["follow_up"][0]["visit_price"], "follow_up")  # the refusals changed nothing


class PercentDepositTests(DepositTestCase):
    """A deposit as a percent of the visit's fee (Saged, 2026-11-19), to the nearest pound."""

    def test_the_deposit_is_the_percent_of_the_fee(self):
        with self.on(0):
            r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                     {"changes": {"booking_deposit": True, "booking_deposit_percent": 50}, "expected": {}})
            self.assertEqual(r.status_code, 200, r.content)
            fee = self.services_a["consultation"].price
            asked = (fee * 50 + 5000) // 10000 * 100
            wrong = self.book(payment=self.deposit(asked + 100), expect=409, **self.slot(3))
            self.assertEqual((wrong.status_code, wrong.json()["error"]["code"]), (409, "deposit_changed"))
            self.assertEqual(wrong.json()["error"]["amount"], asked)
            visit = self.book(payment=self.deposit(asked), **self.slot(3, "11:00")).json()["appointment"]
            self.assertEqual(visit["invoice"]["paid"], asked)

    def test_only_the_owner_sets_it(self):
        with self.on(0):
            r = call(self.desk, "PUT", "/api/v1/settings/clinic",
                     {"changes": {"booking_deposit_percent": 50}, "expected": {}})
            self.assertEqual(r.status_code, 403)


class RegisterAutoCloseTests(DepositTestCase):
    """Saged, 2026-11-24: a register nobody closed closes by itself after the day ends, not counted, and the next
    shift starts with its cash (still in the drawer)."""

    def shift(self, **filters):
        from finance.models import Shift

        with clinic_transaction(self.a.id, self.a.shard):
            return Shift.objects.filter(clinic_id=self.a.id, **filters).order_by("-opened_at").first()

    def test_the_sweep_closes_yesterdays_register_and_the_next_one_starts_with_its_cash(self):
        with self.on(0):
            self.deposit_on()
            self.book(payment=self.deposit(), doctor_id=self.owner_id)  # cash: opens the shift by itself
            r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": "50", "reason": "فكة"})
            self.assertEqual(r.status_code, 201, r.content)
        held = DEPOSIT + 50_00
        from finance.models import Shift

        with clinic_transaction(self.a.id, self.a.shard):  # the shift's clock isn't the frozen one: it opened on DAY
            Shift.objects.filter(clinic_id=self.a.id, status="open").update(
                opened_at=dt.datetime(2026, 11, 10, 19, 0, tzinfo=CAIRO))
        # Before the day ends (04:00 the next morning) nothing closes.
        self.assertNotIn("register_closed", sweep_clinic(self.a.id, self.a.shard, dt.datetime(2026, 11, 11, 3, 0, tzinfo=CAIRO)))
        self.assertEqual(self.shift().status, "open")
        counts = sweep_clinic(self.a.id, self.a.shard, dt.datetime(2026, 11, 11, 4, 30, tzinfo=CAIRO))
        self.assertEqual(counts.get("register_closed"), held, counts)
        closed = self.shift()
        self.assertEqual((closed.status, closed.auto_closed, closed.expected, closed.counted, closed.difference,
                          closed.closed_by_id), ("closed", True, held, held, 0, None))
        # Again: nothing more to close.
        self.assertNotIn("register_closed", sweep_clinic(self.a.id, self.a.shard, dt.datetime(2026, 11, 11, 4, 40, tzinfo=CAIRO)))
        # The next cash entry opens a shift that starts with that cash.
        with self.on(1):
            r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": "10", "reason": "فكة"})
            self.assertEqual(r.status_code, 201, r.content)
        now_open = self.shift(status="open")
        self.assertEqual((now_open.auto_opened, now_open.opening_float), (True, held))
        # The owner hears of it, and the register's history says it wasn't counted.
        with self.on(1):
            alerts = self.owner.get("/alerts").content.decode()
            self.assertIn("الخزنة اتقفلت لوحدها من غير عد", alerts)
            rows = self.owner.get("/finances/shifts", HTTP_X_MEMBERSHIP=str(self.owner.membership_id)).content.decode()
            self.assertIn("ما اتعدّتش", rows)
