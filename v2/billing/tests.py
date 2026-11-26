import datetime as dt
import random
import uuid
from collections import defaultdict
from zoneinfo import ZoneInfo

from audit.models import AuditAccess, AuditChange
from billing import selectors
from billing.models import Invoice, InvoiceLine, Payment
from clinics.models import Clinic, Membership
from core.routing import clinic_transaction
from core.testing import call
from scheduling.jobs import expected_summaries, system_ctx
from scheduling.testing import M1TestCase, frozen, summaries, today
from summaries.models import ClinicDay, DoctorDay

CAIRO = ZoneInfo("Africa/Cairo")


class BillingTestCase(M1TestCase):
    def setUp(self):
        super().setUp()
        self.fee = self.services_a["consultation"].price

    def visit(self, **body):
        return self.book(**body).json()["appointment"]

    def pay(self, appointment_id, amount, client=None, method="cash"):
        return call(client or self.desk, "POST", f"/api/v1/appointments/{appointment_id}/payments",
                    {"amount": amount, "method": method})

    def invoice_of(self, appointment_id):
        return call(self.desk, "POST", f"/api/v1/appointments/{appointment_id}/invoice").json()["invoice"]

    def grant(self, *codes):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(granted=list(codes))


class PaymentTests(BillingTestCase):
    def test_partial_payments_up_to_the_total(self):
        appt = self.visit()
        first = self.pay(appt["id"], 10000)
        self.assertEqual(first.status_code, 201, first.content)
        body = first.json()
        self.assertEqual((body["invoice"]["number"], body["invoice"]["paid"], body["invoice"]["due"]),
                         (1, 10000, self.fee - 10000))
        self.assertEqual(self.pay(appt["id"], self.fee - 10000).json()["invoice"]["due"], 0)
        r = self.pay(appt["id"], 1)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))
        clinic, doctors = summaries(self.a, today(self.a))
        self.assertEqual((clinic["collected"], doctors[self.a.membership("owner").id]["collected"]), (self.fee, self.fee))

    def test_invoice_numbers_are_sequential_and_a_refusal_gives_its_number_back(self):
        first, second, third = self.visit(), self.visit(), self.visit()
        self.assertEqual(self.pay(first["id"], 100).json()["invoice"]["number"], 1)
        self.assertEqual(self.pay(second["id"], self.fee + 1).status_code, 409)  # rolled back, number 2 unused
        self.assertEqual(self.pay(third["id"], 100).json()["invoice"]["number"], 2)

    def test_a_cancelled_visit_takes_no_payment(self):
        appt = self.visit()
        self.act(appt["id"], "cancel", "booked")
        r = self.pay(appt["id"], 100)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "cancelled"))

    def test_a_payment_on_a_past_visit_counts_on_the_payment_day(self):
        with frozen(dt.datetime(2026, 11, 10, 19, 0, tzinfo=CAIRO)):
            appt = self.visit(client=self.a.client("assistant"))
        with frozen(dt.datetime(2026, 11, 12, 12, 0, tzinfo=CAIRO)):
            body = self.pay(appt["id"], 5000, client=self.a.client("assistant")).json()
        self.assertEqual(body["payment"]["day"], "2026-11-12")
        visit_day, pay_day = summaries(self.a, dt.date(2026, 11, 10)), summaries(self.a, dt.date(2026, 11, 12))
        self.assertEqual((visit_day[0]["collected"], pay_day[0]["collected"]), (0, 5000))
        self.assertEqual(body["version"], visit_day[0]["version"])
        self.assertEqual(visit_day[0]["version"], 2)  # the booking, then the payment

    def test_money_must_be_a_positive_amount(self):
        appt = self.visit()
        for amount in (0, -5, "abc", "1,500", 1.5, None):
            self.assertEqual(self.pay(appt["id"], amount).status_code, 400, amount)
        self.assertEqual(self.pay(appt["id"], "١٠٠٫٥").json()["payment"]["amount"], 10050)


class LineAndDiscountTests(BillingTestCase):
    def test_a_list_price_needs_collect_and_any_other_price_needs_discount(self):
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        procedure = self.services_a["procedure"]
        url = f"/api/v1/invoices/{invoice['id']}/lines"
        r = call(self.desk, "POST", url, {"kind": "procedure", "service_id": procedure.id, "qty": 2})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["invoice"]["total"], self.fee + 2 * procedure.price)
        line = r.json()["invoice"]["lines"][-1]
        self.assertTrue(line["from_list"])
        # No one charges a price that isn't on the list, billing.discount or not (Saged, 2026-11-16).
        for grant in (False, True):
            if grant:
                self.grant("billing.discount")
            for body in ({"kind": "material", "description": "Extra", "unit_price": 5000},
                         {"kind": "procedure", "service_id": procedure.id, "unit_price": 1}):
                r = call(self.desk, "POST", url, body)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"), body)
        total = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]["total"]
        self.assertEqual(total, self.fee + 2 * procedure.price)
        foreign = self.services_b["procedure"].id
        self.assertEqual(call(self.desk, "POST", url, {"kind": "procedure", "service_id": foreign}).status_code, 404)

    def test_removing_a_paid_line_leaves_a_credit_the_desk_gives_back_from_the_drawer(self):
        # No more "refund first" (Saged, 2026-11-22): what was paid over the new total is the patient's credit,
        # and "Give back" pays it out of the drawer as a refund.
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        procedure = self.services_a["procedure"]
        lines = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                     {"kind": "procedure", "service_id": procedure.id}).json()["invoice"]["lines"]
        self.pay(appt["id"], self.fee + 100)
        r = call(self.desk, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{lines[-1]['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["invoice"]["total"], r.json()["invoice"]["paid"], r.json()["invoice"]["credit"]),
                         (self.fee, self.fee + 100, 100))
        back = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/give-back", {})
        self.assertEqual(back.status_code, 201, back.content)
        self.assertEqual((back.json()["invoice"]["paid"], back.json()["invoice"]["credit"]), (self.fee, 0))
        payments = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]["payments"]
        self.assertEqual(sorted((p["kind"], p["amount"]) for p in payments)[-1], ("refund", 100))
        nothing = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/give-back", {})
        self.assertEqual((nothing.status_code, nothing.json()["error"]["code"]), (409, "no_credit"))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.a.id, action="invoice.line_remove").exists())
        self.assertEqual(call(self.desk, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{lines[-1]['id']}")
                         .status_code, 404)

    def test_a_discount_reaches_the_owners_alerts(self):
        # Saged, 2026-11-26: "a discount gives no alert at all".
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        self.grant("billing.discount")
        r = call(self.desk, "PUT", f"/api/v1/invoices/{invoice['id']}/discount", {"discount": 5000, "expected_discount": 0})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn('class="nav-count', self.owner.get("/patients").content.decode())
        page = self.owner.get("/alerts").content.decode()
        self.assertIn("alert-row level-info", page)

    def test_discounts_are_absolute_with_the_value_they_replace(self):
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        url = f"/api/v1/invoices/{invoice['id']}/discount"
        self.assertEqual(call(self.desk, "PUT", url, {"discount": 100, "expected_discount": 0}).status_code, 403)
        self.grant("billing.discount")
        r = call(self.desk, "PUT", url, {"discount": 5000, "expected_discount": 0})
        self.assertEqual((r.status_code, r.json()["invoice"]["total"]), (200, self.fee - 5000))
        stale = call(self.desk, "PUT", url, {"discount": 6000, "expected_discount": 0})
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))
        again = call(self.desk, "PUT", url, {"discount": 5000, "expected_discount": 0})  # already so
        self.assertEqual(again.status_code, 200)
        too_much = call(self.desk, "PUT", url, {"discount": self.fee + 1, "expected_discount": 5000})
        self.assertEqual(too_much.json()["error"]["code"], "discount_too_large")
        self.pay(appt["id"], self.fee - 5000)
        # A discount after paying (Saged, 2026-11-26): it goes through, and what was paid over waits in the file.
        r = call(self.desk, "PUT", url, {"discount": 6000, "expected_discount": 5000})
        self.assertEqual((r.status_code, r.json()["invoice"]["total"]), (200, self.fee - 6000))
        with clinic_transaction(self.a.id, self.a.shard):
            changes = list(AuditChange.objects.filter(clinic_id=self.a.id, action="invoice.discount").order_by("id"))
        self.assertEqual([c.details for c in changes], [{"before": {"discount": 0}, "after": {"discount": 5000}},
                                                        {"before": {"discount": 5000}, "after": {"discount": 6000}}])

    def test_a_discount_after_paying_and_the_money_given_back_is_one_alert(self):
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        self.pay(appt["id"], self.fee)
        self.grant("billing.discount")
        r = call(self.desk, "PUT", f"/api/v1/invoices/{invoice['id']}/discount", {"discount": 1000, "expected_discount": 0})
        self.assertEqual(r.status_code, 200, r.content)
        page = self.owner.get(f"/patients/{appt['patient']['id']}").content.decode()
        self.assertIn(f"give_back={invoice['id']}", page)  # the file shows what waits to be given back
        alerts = self.owner.get("/alerts").content.decode()
        self.assertEqual(alerts.count('class="alert-row'), 1)
        self.assertIn("alert-row level-warn", alerts)  # a discount with money still owed back
        self.assertEqual(alerts.count('class="alert-line"'), 2)
        r = call(self.owner, "POST", f"/api/v1/invoices/{invoice['id']}/give-back", {"method": "card"})
        self.assertEqual(r.status_code, 201, r.content)
        alerts = self.owner.get("/alerts").content.decode()
        self.assertEqual(alerts.count('class="alert-row'), 1)  # the discount told inside the refund
        self.assertIn("alert-row level-bad", alerts)
        self.assertEqual(alerts.count('class="alert-line"'), 4)
        self.assertIn(f'href="/patients/{appt["patient"]["id"]}"', alerts)

    def test_a_give_back_over_two_payments_is_one_alert_and_keeps_an_earlier_discount_apart(self):
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        url = f"/api/v1/invoices/{invoice['id']}/discount"
        self.assertEqual(call(self.owner, "PUT", url, {"discount": 1000, "expected_discount": 0}).status_code, 200)
        half = (self.fee - 1000) // 2
        self.assertEqual(self.pay(appt["id"], half, method="card").status_code, 201)
        self.assertEqual(self.pay(appt["id"], self.fee - 1000 - half, method="card").status_code, 201)
        # More than one payment's worth, so it comes back from both.
        self.assertEqual(call(self.owner, "PUT", url, {"discount": 1000 + half + 500, "expected_discount": 1000}).status_code, 200)
        r = call(self.owner, "POST", f"/api/v1/invoices/{invoice['id']}/give-back", {"method": "card"})
        self.assertEqual(r.status_code, 201, r.content)
        alerts = self.owner.get("/alerts").content.decode()
        self.assertEqual(alerts.count('class="alert-row'), 2)  # the discount before paying, and one give-back
        self.assertEqual(alerts.count("alert-row level-bad"), 1)
        self.assertEqual(alerts.count("alert-row level-info"), 1)


class RefundAndVoidTests(BillingTestCase):
    def test_refunds_need_the_permission_count_on_their_own_day_and_are_audited(self):
        with frozen(dt.datetime(2026, 11, 10, 19, 0, tzinfo=CAIRO)):
            desk = self.a.client("assistant")
            appt = self.visit(client=desk, payment={"amount": 10000, "method": "card"})
            payment_id = call(desk, "GET", f"/api/v1/invoices/{appt['invoice']['id']}").json()["invoice"]["payments"][0]["id"]
            self.assertEqual(call(desk, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": 100}).status_code, 403)
        self.grant("billing.refund")
        with frozen(dt.datetime(2026, 11, 11, 18, 0, tzinfo=CAIRO)):
            desk = self.a.client("assistant")
            # Before going in, it all goes back, never a part (Saged, 2026-11-26).
            part = call(desk, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": 4000})
            self.assertEqual((part.status_code, part.json()["error"]["code"]), (409, "refund_all"))
            r = call(desk, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": 10000})
            self.assertEqual(r.status_code, 201, r.content)
            body = r.json()
            self.assertEqual((body["payment"]["kind"], body["payment"]["method"], body["payment"]["day"]),
                             ("refund", "card", "2026-11-11"))
            self.assertEqual((body["invoice"]["paid"], body["invoice"]["status"]), (0, "void"))
            over = call(desk, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": 1})
            self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "over_refund"))
            refund_id = body["payment"]["id"]
            self.assertEqual(call(desk, "POST", f"/api/v1/payments/{refund_id}/refund", {"amount": 1}).status_code, 400)
        first, second = summaries(self.a, dt.date(2026, 11, 10)), summaries(self.a, dt.date(2026, 11, 11))
        owner = self.a.membership("owner").id
        self.assertEqual((first[0]["collected"], first[0]["refunded"]), (10000, 0))
        self.assertEqual((second[0]["refunded"], second[1][owner]["refunded"]), (10000, 10000))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.a.id, action="payment.refund").exists())

    def test_void_only_while_nothing_is_paid_and_then_a_new_invoice_can_follow(self):
        self.grant("billing.refund")
        appt = self.visit()
        invoice = self.pay(appt["id"], 100).json()["invoice"]
        r = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/void")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "void_paid"))
        payment = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]["payments"][0]
        # All of it back before going in: the bill is void and the visit cancelled with it (Saged, 2026-11-26).
        r = call(self.desk, "POST", f"/api/v1/payments/{payment['id']}/refund", {"amount": 100})
        self.assertEqual(r.json()["invoice"]["status"], "void")
        self.assertEqual(self.appointment(appt["id"]).status, "cancelled")
        self.assertEqual(call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/void").status_code, 200)  # repeat
        r = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines", {"kind": "other", "description": "x",
                                                                                "unit_price": 0})
        self.assertEqual(r.json()["error"]["code"], "invoice_void")
        # An unpaid bill voided by hand: the next payment opens a new one.
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        r = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/void")
        self.assertEqual((r.status_code, r.json()["invoice"]["status"]), (200, "void"))
        again = self.pay(appt["id"], 200).json()["invoice"]
        self.assertEqual((again["number"], again["paid"]), (invoice["number"] + 1, 200))
        self.assertEqual(call(self.desk, "GET", f"/api/v1/appointments/{appt['id']}").json()["appointment"]["invoice"]["id"],
                         again["id"])


class ReadTests(BillingTestCase):
    def test_invoice_view_writes_an_access_event_and_the_receipt_has_what_a_print_needs(self):
        appt = self.visit(payment={"amount": 5000, "method": "cash"})
        invoice_id = appt["invoice"]["id"]
        r = call(self.desk, "GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["invoice"]["patient"]["id"], appt["patient"]["id"])
        with clinic_transaction(self.a.id, self.a.shard):
            events = list(AuditAccess.objects.filter(clinic_id=self.a.id).values_list("patient_id", "action"))
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            receipt = selectors.receipt(ctx, invoice_id)
        self.assertEqual(events, [(appt["patient"]["id"], AuditAccess.Action.INVOICE)])
        self.assertEqual((receipt["number"], receipt["clinic"]["name"], receipt["print"]["print_language"]),
                         (1, "a clinic", "ar"))
        self.assertEqual(receipt["appointment"]["doctor"]["id"], self.a.membership("owner").id)
        self.assertEqual([p["amount"] for p in receipt["payments"]], [5000])

    def test_another_clinics_invoice_or_payment_is_404(self):
        foreign = self.book(fixture=self.b, payment={"amount": 100, "method": "cash"}).json()["appointment"]
        self.grant("billing.refund", "billing.discount")
        invoice = foreign["invoice"]["id"]
        with clinic_transaction(self.b.id, self.b.shard):
            line = InvoiceLine.objects.filter(clinic_id=self.b.id, invoice_id=invoice).values_list("id", flat=True)[0]
            payment = Payment.objects.filter(clinic_id=self.b.id, invoice_id=invoice).values_list("id", flat=True)[0]
        for method, url, body in (("GET", f"/api/v1/invoices/{invoice}", None),
                                  ("DELETE", f"/api/v1/invoices/{invoice}/lines/{line}", {}),
                                  ("POST", f"/api/v1/payments/{payment}/refund", {"amount": 50}),
                                  ("POST", f"/api/v1/invoices/{invoice}/lines", {"kind": "other", "description": "x",
                                                                                 "unit_price": 1}),
                                  ("PUT", f"/api/v1/invoices/{invoice}/discount", {"discount": 1, "expected_discount": 0}),
                                  ("POST", f"/api/v1/invoices/{invoice}/void", {}),
                                  ("POST", f"/api/v1/appointments/{foreign['id']}/payments", {"amount": 1, "method": "cash"}),
                                  ("POST", f"/api/v1/appointments/{foreign['id']}/invoice", {})):
            self.assertEqual(call(self.desk, method, url, body).status_code, 404, url)
        with clinic_transaction(self.b.id, self.b.shard):
            self.assertTrue(InvoiceLine.objects.filter(clinic_id=self.b.id, id=line).exists())
            self.assertEqual(Payment.objects.get(clinic_id=self.b.id, id=payment).refunded, 0)


class MoneyInvariantTests(BillingTestCase):
    """T7: after random sequences of actions over three days, the money adds up everywhere."""

    def check_invariants(self, days):
        with clinic_transaction(self.a.id, self.a.shard):
            payments = list(Payment.objects.filter(clinic_id=self.a.id).values())
            invoices = list(Invoice.objects.filter(clinic_id=self.a.id).values())
            lines = list(InvoiceLine.objects.filter(clinic_id=self.a.id).values("invoice_id", "amount"))
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            expected = {day: expected_summaries(ctx, day) for day in days}
            doctor_rows = list(DoctorDay.objects.filter(clinic_id=self.a.id).values())
            clinic_rows = {r["day"]: r for r in ClinicDay.objects.filter(clinic_id=self.a.id).values()}
        by_invoice, refunded_of, line_sum = defaultdict(int), defaultdict(int), defaultdict(int)
        for p in payments:
            by_invoice[p["invoice_id"]] += p["amount"] if p["kind"] == "payment" else -p["amount"]
            if p["kind"] == "refund":
                refunded_of[p["refund_of_id"]] += p["amount"]
        for line in lines:
            line_sum[line["invoice_id"]] += line["amount"]
        for invoice in invoices:
            self.assertEqual(invoice["paid"], by_invoice[invoice["id"]], invoice)  # paid = payments - refunds
            self.assertEqual(invoice["subtotal"], line_sum[invoice["id"]], invoice)
        for p in payments:
            if p["kind"] == "payment":
                self.assertEqual(p["refunded"], refunded_of[p["id"]])
        for day in days:
            doctors = [r for r in doctor_rows if r["day"] == day]
            clinic = clinic_rows.get(day, {"done": 0, "collected": 0, "refunded": 0})
            for name in ("done", "collected", "refunded"):
                self.assertEqual(sum(r[name] for r in doctors), clinic[name], (day, name))  # doctor-days = clinic-day
            want_doctors, want_clinic = expected[day]
            for name in ("done", "collected", "refunded"):
                self.assertEqual(clinic[name], want_clinic[name], (day, name))  # summaries = the recompute
            for r in doctors:
                want = want_doctors.get(r["doctor_id"], {"booked": 0, "done": 0, "collected": 0, "refunded": 0})
                for name in ("booked", "done", "collected", "refunded"):
                    self.assertEqual(r[name], want[name], (day, r["doctor_id"], name))

    def test_random_sequences(self):
        self.grant("billing.refund", "billing.discount")
        days = [dt.date(2026, 11, 10), dt.date(2026, 11, 11), dt.date(2026, 11, 12)]
        doctors = [self.a.membership("owner").id]
        procedure = self.services_a["procedure"]
        rng = random.Random(20261107)
        visits = []
        statuses = defaultdict(int)
        succeeded = defaultdict(int)
        in_room = set()  # one patient in the room per doctor (Saged, 2026-11-13): "finish" aims at them
        for step in range(160):
            day = days[min(step // 55, 2)]
            with frozen(dt.datetime.combine(day, dt.time(19, 0), tzinfo=CAIRO)):
                desk = self.a.client("assistant")
                choice = rng.choice(["book", "book_pay", "book_later_pay", "pay", "pay", "refund", "line", "remove",
                                     "discount", "void", "cancel", "reschedule", "arrive", "call", "finish"])
                target = rng.choice(visits) if visits else None
                r = None
                if choice.startswith("book") or target is None:
                    body = {"doctor_id": rng.choice(doctors)}
                    if choice == "book_pay":
                        body["payment"] = {"amount": rng.randint(1, self.fee), "method": "cash"}
                    if choice == "book_later_pay":
                        body["day"] = (day + dt.timedelta(days=1)).isoformat()
                    r = self.book(client=desk, expect=None, **body)
                    if r.status_code == 201:
                        visits.append(r.json()["appointment"]["id"])
                elif choice == "pay":
                    r = self.pay(target, rng.randint(1, self.fee), client=desk)
                elif choice in ("refund", "remove", "discount", "void", "line"):
                    detail = call(desk, "GET", f"/api/v1/appointments/{target}").json()["appointment"]
                    invoice = detail["invoice"]
                    if invoice is None:
                        continue
                    full = call(desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]
                    if choice == "refund":
                        paid = [p for p in full["payments"] if p["kind"] == "payment" and p["refunded"] < p["amount"]]
                        if paid:
                            p = rng.choice(paid)
                            r = call(desk, "POST", f"/api/v1/payments/{p['id']}/refund",
                                     {"amount": rng.randint(1, p["amount"] - p["refunded"])})
                    elif choice == "line":
                        r = call(desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                                 {"kind": "procedure", "service_id": procedure.id, "qty": rng.randint(1, 2)})
                    elif choice == "remove" and full["lines"]:
                        r = call(desk, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{rng.choice(full['lines'])['id']}")
                    elif choice == "discount":
                        r = call(desk, "PUT", f"/api/v1/invoices/{invoice['id']}/discount",
                                 {"discount": rng.randint(0, full["subtotal"]), "expected_discount": full["discount"]})
                    elif choice == "void":
                        r = call(desk, "POST", f"/api/v1/invoices/{invoice['id']}/void")
                elif choice == "cancel":
                    r = self.act(target, "cancel", "booked", client=desk)
                elif choice == "reschedule":
                    r = call(desk, "POST", f"/api/v1/appointments/{target}/reschedule",
                             {"status": "booked", "doctor_id": doctors[0], "day": (day + dt.timedelta(days=1)).isoformat()})
                else:
                    if choice == "finish" and in_room:
                        target = min(in_room)  # no draw from rng: the rest of the sequence stays as it was
                    source = {"arrive": "booked", "call": "waiting", "finish": "in_room"}[choice]
                    body = {"payment": {"amount": rng.randint(1, 3000), "method": "card"}} if choice == "finish" else {}
                    if body:  # a discount after paying can leave nothing due (Saged, 2026-11-26): then no payment
                        due = call(desk, "GET", f"/api/v1/appointments/{target}").json().get("appointment", {}).get("due")
                        if not due:
                            body = {}
                        else:
                            body["payment"]["amount"] = min(body["payment"]["amount"], due)
                    r = self.act(target, choice, source, client=desk, **body)
                    if r.status_code < 300:
                        (in_room.add if choice == "call" else in_room.discard)(target)
                if r is not None:
                    self.assertLess(r.status_code, 500, r.content)
                    statuses[r.status_code] += 1
                    if r.status_code < 300:
                        succeeded[choice] += 1
        self.check_invariants(days + [dt.date(2026, 11, 13)])
        self.assertGreater(statuses[200] + statuses[201], 60)
        self.assertGreater(statuses[409], 0)
        # Every kind of action really happened, including cancel after payment (refund_first) and void-then-reinvoice.
        for choice in ("book", "book_pay", "book_later_pay", "pay", "refund", "line", "remove", "discount", "void",
                       "cancel", "reschedule", "arrive", "call", "finish"):
            self.assertGreater(succeeded[choice], 0, (choice, dict(succeeded)))


class LanguageTests(BillingTestCase):
    def test_refusals_speak_the_users_language(self):
        appt = self.visit()
        r = self.pay(appt["id"], self.fee + 1)
        self.assertEqual(r.json()["error"]["message"], "المبلغ أكبر من المستحق.")


class LineApprovalTests(BillingTestCase):
    """The owner can make a removed bill line wait for their permission (Saged, 2026-11-25)."""

    def test_the_desks_removed_line_waits_until_the_owner_approves(self):
        r = call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"approvals": ["line"]}, "expected": {"approvals": []}})
        self.assertEqual(r.status_code, 200, r.content)
        appt = self.visit()
        invoice = self.invoice_of(appt["id"])
        lines = call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                     {"kind": "procedure", "service_id": self.services_a["procedure"].id}).json()["invoice"]["lines"]
        r = call(self.desk, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{lines[-1]['id']}")
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(len(r.json()["invoice"]["lines"]), len(lines))  # still on the bill
        url = f"/api/v1/cash-requests/{r.json()['request']['id']}/decide"
        self.assertEqual(call(self.owner, "POST", url, {"approve": True}).status_code, 200)
        after = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]
        self.assertEqual(len(after["lines"]), len(lines) - 1)
        self.assertEqual(after["total"], self.fee)
        # The owner's own removal never waits.
        lines = call(self.owner, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                     {"kind": "procedure", "service_id": self.services_a["procedure"].id}).json()["invoice"]["lines"]
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{lines[-1]['id']}").status_code, 200)


class RefundRequestTests(BillingTestCase):
    """A refund that waits for the owner is checked when it is asked (Saged, 2026-11-26): four requests the owner
    could only refuse reached the alerts, each one more than the payment had left."""

    def test_checked_when_asked_one_at_a_time_and_shown_with_its_file(self):
        r = call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"approvals": ["refund"]}, "expected": {"approvals": []}})
        self.assertEqual(r.status_code, 200, r.content)
        self.grant("billing.refund")
        appt = self.visit(payment={"amount": 10000, "method": "card"})
        invoice = call(self.desk, "GET", f"/api/v1/invoices/{appt['invoice']['id']}").json()["invoice"]
        url = f"/api/v1/payments/{invoice['payments'][0]['id']}/refund"
        over = call(self.desk, "POST", url, {"amount": 10001})
        self.assertEqual((over.status_code, over.json()["error"]["code"]), (409, "over_refund"))
        self.assertIn("100", over.json()["error"]["message"])  # what is left, said at once
        key = str(uuid.uuid4())
        asked = call(self.desk, "POST", url, {"amount": 10000}, key=key)  # all of it, before going in
        self.assertEqual(asked.status_code, 202)
        retried = call(self.desk, "POST", url, {"amount": 10000}, key=key)  # the same tap sent again: the same request
        self.assertEqual((retried.status_code, retried.json()["request"]["id"]), (202, asked.json()["request"]["id"]))
        again = call(self.desk, "POST", url, {"amount": 10000})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "already_asked"))
        page = self.owner.get("/alerts").content.decode()
        self.assertIn(f'href="/patients/{invoice["patient"]["id"]}"', page)  # the file it is about
        self.assertIn(f"فاتورة {invoice['number']}", page)
        self.assertNotIn("data-request-problem", page)
        # The owner gives it back meanwhile: the request can no longer be done, and says so before Approve.
        self.assertEqual(call(self.owner, "POST", url, {"amount": 10000}).status_code, 201)
        page = self.owner.get("/alerts").content.decode()
        self.assertIn("data-request-problem", page)
