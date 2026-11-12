"""M3 money (design F1, F3, F4, F5): the register, expenses, staff pay and the money reports."""

import threading

from django.db import connections
from django.test import TransactionTestCase

from billing.models import Payment
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from finance.models import Shift
from scheduling.testing import add_schedule, services_of, today


def current_shift(fixture):
    with clinic_transaction(fixture.id, fixture.shard):
        return Shift.objects.filter(clinic_id=fixture.id, status="open").values_list("id", flat=True).first()


class RegisterTests(M2TestCase):
    def close(self, counted, client=None, fixture=None):
        shift_id = current_shift(fixture or self.a)
        return call(client or self.owner, "POST", f"/api/v1/register/shifts/{shift_id}/close", {"counted": counted})

    def pay(self, visit, amount, method):
        return call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments",
                    {"amount": amount, "method": method})

    def test_cash_needs_an_open_shift_other_methods_never_do(self):
        visit = self.in_room()
        self.assertEqual(self.close(0).status_code, 200)
        r = self.pay(visit, 100, "cash")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "register_closed"), r.content)
        r = self.pay(visit, 100, "card")
        self.assertEqual(r.status_code, 201, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(list(Payment.objects.filter(clinic_id=self.a.id).values_list("method", "shift_id")),
                             [("card", None)])

    def test_a_shift_closes_with_the_expected_cash_and_the_difference(self):
        call(self.owner, "POST", f"/api/v1/register/shifts/{current_shift(self.a)}/close", {"counted": 0})
        r = call(self.desk, "POST", "/api/v1/register/open", {"opening_float": 50000})
        self.assertEqual(r.status_code, 201, r.content)
        shift_id = r.json()["shift"]["id"]
        visit = self.in_room()
        self.assertEqual(self.pay(visit, 20000, "cash").status_code, 201)
        self.assertEqual(self.pay(visit, 5000, "card").status_code, 201)
        self.assertEqual(call(self.desk, "POST", "/api/v1/register/movements",
                              {"kind": "out", "amount": 3000, "reason": "Water"}).status_code, 201)
        self.assertEqual(call(self.owner, "POST", "/api/v1/expenses",
                              {"category_id": self.category("rent"), "amount": 1000, "from_shift": True}).status_code, 201)
        report = call(self.desk, "GET", "/api/v1/register").json()
        self.assertEqual(report["totals"]["methods"]["cash"]["net"], 20000)
        self.assertEqual(report["totals"]["methods"]["card"]["net"], 5000)
        self.assertEqual(report["totals"]["expected_cash"], 50000 + 20000 - 3000 - 1000)
        r = call(self.desk, "POST", f"/api/v1/register/shifts/{shift_id}/close", {"counted": 65500})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["shift"]["expected"], r.json()["shift"]["difference"]), (66000, -500))
        again = call(self.desk, "POST", f"/api/v1/register/shifts/{shift_id}/close", {"counted": 65500})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "shift_closed"))

    def test_one_open_shift_per_place(self):
        r = call(self.desk, "POST", "/api/v1/register/open", {"opening_float": 0})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "shift_open"))
        self.assertEqual(r.json()["error"]["shift_id"], current_shift(self.a))

    def test_a_cash_refund_needs_the_register_too(self):
        visit = self.in_room()
        payment = self.pay(visit, 1000, "cash").json()["payment"]
        self.close(1000)
        r = call(self.owner, "POST", f"/api/v1/payments/{payment['id']}/refund", {"amount": 500})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "register_closed"))
        r = call(self.owner, "POST", f"/api/v1/payments/{payment['id']}/refund", {"amount": 500, "method": "card"})
        self.assertEqual(r.status_code, 201, r.content)

    def test_codes_and_another_places_shift(self):
        self.set_codes("assistant", revoked=["cash.collect"])
        visit = self.in_room()
        self.assertEqual(self.pay(visit, 100, "cash").status_code, 403)
        self.assertEqual(self.pay(visit, 100, "card").status_code, 201)
        self.assertEqual(call(self.doc, "GET", "/api/v1/register/shifts").status_code, 200)
        other = current_shift(self.b)
        for method, url in (("GET", f"/api/v1/register/shifts/{other}"),
                            ("POST", f"/api/v1/register/shifts/{other}/close")):
            r = call(self.owner, method, url, {"counted": 0})
            self.assertEqual(r.status_code, 404, (url, r.content))
        self.assertIsNotNone(current_shift(self.b))

    def category(self, code, client=None):
        items = call(client or self.owner, "GET", "/api/v1/expense-categories").json()["items"]
        return next(c["id"] for c in items if c["code"] == code)


class ExpenseTests(M2TestCase):
    def test_default_categories_editable_and_another_places_404(self):
        items = call(self.owner, "GET", "/api/v1/expense-categories").json()["items"]
        self.assertEqual([c["code"] for c in items], ["rent", "salaries", "materials", "lab", "utilities", "other"])
        r = call(self.owner, "POST", "/api/v1/expense-categories", {"name": "Marketing"})
        self.assertEqual(r.status_code, 201, r.content)
        dup = call(self.owner, "POST", "/api/v1/expense-categories", {"name": "Marketing"})
        self.assertEqual((dup.status_code, dup.json()["error"]["code"]), (409, "duplicate"))
        theirs = call(self.b.client("owner"), "GET", "/api/v1/expense-categories").json()["items"][0]["id"]
        self.assertEqual(call(self.owner, "PATCH", f"/api/v1/expense-categories/{theirs}", {"name": "x"}).status_code, 404)
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": theirs, "amount": 100})
        self.assertEqual(r.status_code, 404, r.content)
        self.assertEqual(call(self.desk, "GET", "/api/v1/expense-categories").status_code, 403)

    def test_an_expense_moves_the_days_expenses_and_a_void_takes_effect_once(self):
        from summaries.models import ClinicDay

        rent = call(self.owner, "GET", "/api/v1/expense-categories").json()["items"][0]["id"]
        day = today(self.a)
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": rent, "amount": 150000, "note": "Nov"})
        self.assertEqual(r.status_code, 201, r.content)
        expense = r.json()["expense"]
        tomorrow = day.fromordinal(day.toordinal() + 1).isoformat()
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": rent, "amount": 1, "day": tomorrow})
        self.assertEqual(r.status_code, 400)
        listed = call(self.owner, "GET", "/api/v1/expenses").json()
        self.assertEqual((len(listed["items"]), listed["total"]), (1, 150000))
        for _ in range(2):
            self.assertEqual(call(self.owner, "POST", f"/api/v1/expenses/{expense['id']}/void").status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(ClinicDay.objects.get(clinic_id=self.a.id, day=day).expenses, 0)
        self.assertEqual(call(self.owner, "GET", "/api/v1/expenses").json()["total"], 0)
        theirs = call(self.b.client("owner"), "POST", "/api/v1/expenses", {
            "category_id": call(self.b.client("owner"), "GET", "/api/v1/expense-categories").json()["items"][0]["id"],
            "amount": 100}).json()["expense"]["id"]
        self.assertEqual(call(self.owner, "POST", f"/api/v1/expenses/{theirs}/void").status_code, 404)


class StaffPayTests(M2TestCase):
    def test_rule_statement_payout_and_profit(self):
        doctor = self.a.membership("owner").id  # the owner works the queue
        r = call(self.owner, "PUT", f"/api/v1/finances/pay/{doctor}",
                 {"percent_bp": 3000, "per_visit": 1000, "per_procedure": 0, "monthly_salary": 0, "version": 0})
        self.assertEqual(r.status_code, 200, r.content)
        stale = call(self.owner, "PUT", f"/api/v1/finances/pay/{doctor}", {"percent_bp": 1, "version": 0})
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))
        visit = self.in_room()
        fee = self.services_a["procedure"].price
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": fee, "method": "cash"})
        self.act(visit["id"], "finish", "in_room")
        month = today(self.a).isoformat()[:7]
        body = call(self.owner, "GET", f"/api/v1/finances/pay/{doctor}/statement?month={month}").json()
        self.assertEqual((body["visits"], body["revenue"]), (1, fee))
        earned = (fee * 3000 + 5000) // 10000 + 1000
        self.assertEqual(body["earned"], earned)
        r = call(self.owner, "POST", f"/api/v1/finances/pay/{doctor}/payouts",
                 {"month": month, "amount": 500, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["statement"]["balance"], earned - 500)
        profit = call(self.owner, "GET", f"/api/v1/finances/profit?month={month}").json()
        self.assertEqual((profit["revenue"], profit["staff_pay"], profit["profit"]), (fee, earned, fee - earned))
        # finances.* are the owner's; another place's member is 404.
        self.assertEqual(call(self.doc, "GET", f"/api/v1/finances/pay/{doctor}/statement").status_code, 403)
        other = self.b.membership("owner").id
        self.assertEqual(call(self.owner, "GET", f"/api/v1/finances/pay/{other}/statement").status_code, 404)
        self.assertEqual(call(self.owner, "PUT", f"/api/v1/finances/pay/{other}", {"version": 0}).status_code, 404)

    def test_a_cash_payout_needs_the_register(self):
        doctor = self.a.membership("doctor").id
        call(self.owner, "POST", f"/api/v1/register/shifts/{current_shift(self.a)}/close", {"counted": 0})
        month = today(self.a).isoformat()[:7]
        r = call(self.owner, "POST", f"/api/v1/finances/pay/{doctor}/payouts", {"month": month, "amount": 1, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "register_closed"))
        r = call(self.owner, "POST", f"/api/v1/finances/pay/{doctor}/payouts",
                 {"month": month, "amount": 1, "method": "transfer"})
        self.assertEqual(r.status_code, 201, r.content)


class MoneyReportTests(M2TestCase):
    def test_receivables_and_revenue_by_section_and_method(self):
        owed = self.owed_visit()
        fee = self.services_a["procedure"].price
        body = call(self.owner, "GET", "/api/v1/finances/receivables").json()
        self.assertEqual([(i["patient"]["id"], i["owed"]) for i in body["items"]], [(owed["patient"]["id"], fee)])
        self.assertEqual(body["total"], {"owed": fee, "patients": 1})
        paid = self.in_room()
        call(self.desk, "POST", f"/api/v1/appointments/{paid['id']}/payments", {"amount": 700, "method": "cash"})
        call(self.desk, "POST", f"/api/v1/appointments/{paid['id']}/payments", {"amount": 300, "method": "card"})
        by_method = call(self.owner, "GET", "/api/v1/finances/revenue?by=method").json()
        self.assertEqual({i["key"]: i["net"] for i in by_method["items"]}, {"card": 300, "cash": 700})
        by_section = call(self.owner, "GET", "/api/v1/finances/revenue?by=section").json()
        self.assertEqual([i["net"] for i in by_section["items"]], [1000])
        self.assertIsNotNone(by_section["items"][0]["key"])
        by_doctor = call(self.owner, "GET", "/api/v1/finances/revenue?by=doctor").json()
        self.assertEqual(by_doctor["net"], 1000)
        self.assertEqual(call(self.owner, "GET", "/api/v1/finances/revenue?by=week").status_code, 400)
        self.assertEqual(call(self.owner, "GET", "/api/v1/finances/revenue?from=2020-01-01&to=2026-01-01").status_code, 400)
        self.assertEqual(call(self.desk, "GET", "/api/v1/finances/receivables").status_code, 403)
        # The other place sees none of it.
        self.assertEqual(call(self.b.client("owner"), "GET", "/api/v1/finances/receivables").json()["items"], [])

    def test_the_recompute_agrees_with_the_money_days(self):
        from scheduling.jobs import recompute_day

        visit = self.in_room()
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 700, "method": "cash"})
        self.act(visit["id"], "finish", "in_room")
        self.assertEqual(recompute_day(self.a.id, self.a.shard, today(self.a)), [])


class RegisterConcurrencyTests(TransactionTestCase):
    """F1 on real PostgreSQL: a close and a cash payment at once never leave a payment outside the count."""

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("rc", "shard_1")
        add_schedule(self.a, "owner")
        self.services = services_of(self.a)

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

    def test_a_close_and_a_cash_payment_race(self):
        desk = self.a.client("assistant")
        owner_id = self.a.membership("owner").id
        visits = []
        for _ in range(3):
            visit = call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": owner_id, "service_id": self.services["procedure"].id,
                "new_patient": {"full_name": "Race Patient"}}).json()["appointment"]
            call(desk, "POST", f"/api/v1/appointments/{visit['id']}/arrive", {"status": "booked"})
            visits.append(visit)
        shift_id = current_shift(self.a)
        jobs = [lambda v=v: call(self.a.client("assistant"), "POST", f"/api/v1/appointments/{v['id']}/payments",
                                 {"amount": 100, "method": "cash"}) for v in visits]
        jobs.append(lambda: call(self.a.client("owner"), "POST", f"/api/v1/register/shifts/{shift_id}/close",
                                 {"counted": 0}))
        results = self.run_together(jobs)
        codes = [r.status_code if hasattr(r, "status_code") else repr(r) for r in results]
        self.assertEqual(codes[-1], 200, codes)
        self.assertTrue(all(c in (201, 409) for c in codes[:-1]), codes)
        with clinic_transaction(self.a.id, self.a.shard):
            shift = Shift.objects.get(clinic_id=self.a.id, id=shift_id)
            taken = sum(Payment.objects.filter(clinic_id=self.a.id, shift_id=shift_id).values_list("amount", flat=True))
            self.assertEqual(Payment.objects.filter(clinic_id=self.a.id, shift__isnull=True, method="cash").count(), 0)
        self.assertEqual(taken, 100 * codes[:-1].count(201))
        self.assertEqual(shift.expected, taken)  # the count saw every payment the shift holds
