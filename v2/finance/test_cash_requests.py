"""Money out of the drawer waits for the owner's OK when the owner turns it on (Saged, 2026-11-25)."""

from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from finance.models import CashMovement, CashRequest, Expense


class CashRequestTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.owner = self.a.client("owner")
        self.desk = self.a.client("assistant")
        call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": 1000, "reason": "فكة"})
        self.categories = {c["code"]: c["id"] for c in call(self.owner, "GET", "/api/v1/expense-categories").json()["items"]}

    def approval(self, on):
        r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"drawer_approval": on}, "expected": {"drawer_approval": not on}})
        self.assertEqual(r.status_code, 200, r.content)

    def count(self, model, **where):
        with clinic_transaction(self.a.id, self.a.shard):
            return model.objects.filter(clinic_id=self.a.id, **where).count()

    def spend(self, client, amount=300):
        return call(client, "POST", "/api/v1/drawer-expenses",
                    {"category_id": self.categories["utilities"], "amount": amount, "note": "كهربا"})

    def test_off_the_desk_spends_at_once(self):
        self.assertEqual(self.spend(self.desk).status_code, 201)
        self.assertEqual(self.count(Expense), 1)
        self.assertEqual(self.count(CashRequest), 0)

    def test_on_the_desks_expense_waits_and_the_owner_approves_it_once(self):
        self.approval(True)
        r = self.spend(self.desk)
        self.assertEqual(r.status_code, 202, r.content)
        request = r.json()["request"]
        self.assertEqual((request["status"], request["amount"]), ("pending", 300))
        self.assertEqual(self.count(Expense), 0)  # nothing left the drawer yet
        pending = call(self.owner, "GET", "/api/v1/cash-requests").json()["items"]
        self.assertEqual([p["id"] for p in pending], [request["id"]])
        self.assertEqual([p["id"] for p in call(self.desk, "GET", "/api/v1/cash-requests").json()["items"]], [request["id"]])
        url = f"/api/v1/cash-requests/{request['id']}/decide"
        self.assertEqual(call(self.desk, "POST", url, {"approve": True}).status_code, 403)
        r = call(self.owner, "POST", url, {"approve": True})
        self.assertEqual((r.status_code, r.json()["request"]["status"]), (200, "approved"), r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            expense = Expense.objects.get(clinic_id=self.a.id)
        self.assertEqual((expense.amount, expense.created_by_id, expense.shift_id is not None),
                         (300, self.desk.membership_id, True))  # in the drawer, as the one who asked
        again = call(self.owner, "POST", url, {"approve": True})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "decided"))
        self.assertEqual(self.count(Expense), 1)
        self.assertEqual(call(self.owner, "GET", "/api/v1/cash-requests").json()["items"], [])

    def test_on_cash_out_waits_and_a_rejected_one_takes_nothing(self):
        self.approval(True)
        r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "out", "amount": 200, "reason": "مواصلات"})
        self.assertEqual(r.status_code, 202, r.content)
        url = f"/api/v1/cash-requests/{r.json()['request']['id']}/decide"
        self.assertEqual(call(self.owner, "POST", url, {"approve": False}).json()["request"]["status"], "rejected")
        self.assertEqual(self.count(CashMovement, kind="out"), 0)
        # Cash in never waits, and the owner never waits for themself.
        self.assertEqual(call(self.desk, "POST", "/api/v1/register/movements",
                              {"kind": "in", "amount": 50, "reason": "فكة"}).status_code, 201)
        self.assertEqual(self.spend(self.owner).status_code, 201)

    def test_approving_more_than_the_drawer_holds_is_refused_and_stays_pending(self):
        self.approval(True)
        r = self.spend(self.desk, amount=5000)  # the drawer holds 1,000
        url = f"/api/v1/cash-requests/{r.json()['request']['id']}/decide"
        r = call(self.owner, "POST", url, {"approve": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_enough_cash"))
        self.assertEqual(self.count(CashRequest, status="pending"), 1)
        self.assertEqual(call(self.owner, "POST", url, {"approve": False}).status_code, 200)

    def test_only_the_owner_turns_it_on(self):
        r = call(self.desk, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"drawer_approval": True}, "expected": {"drawer_approval": False}})
        self.assertEqual(r.status_code, 403)

    def test_the_owner_hears_of_a_request(self):
        self.approval(True)
        self.spend(self.desk)
        page = self.owner.get("/alerts").content.decode()
        self.assertIn("data-cash-requests", page)
        self.assertIn("مستني موافقتك", page)
        self.assertIn('class="nav-count', self.owner.get("/patients").content.decode())
