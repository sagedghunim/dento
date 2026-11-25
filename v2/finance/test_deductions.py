"""A shortage taken from pay (Saged, 2026-11-25, an accountant's advice): only when the owner decides, from a shift
that closed short, at most the shortage; it lowers the member's statement and they mark it seen."""

from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from finance.models import Deduction


class DeductionTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.owner = self.a.client("owner")
        self.desk = self.a.client("assistant")
        call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": 10000, "reason": "فكة"})
        with clinic_transaction(self.a.id, self.a.shard):
            from finance.models import Shift

            self.shift = Shift.objects.filter(clinic_id=self.a.id, status="open").values_list("id", flat=True).first()
        r = call(self.desk, "POST", f"/api/v1/register/shifts/{self.shift}/close", {"counted": 7000})  # 30 short
        self.assertEqual(r.status_code, 200, r.content)

    def test_the_owner_takes_part_of_a_shortage_and_the_member_sees_it(self):
        url = f"/api/v1/register/shifts/{self.shift}/deduct"
        self.assertEqual(call(self.desk, "POST", url, {"amount": 1000}).status_code, 403)
        r = call(self.owner, "POST", url, {"amount": 5000})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_shortage"))
        r = call(self.owner, "POST", url, {"amount": 2000})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["deduction"]["membership_id"], self.desk.membership_id)
        again = call(self.owner, "POST", url, {"amount": 1000})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "deducted"))
        statement = call(self.owner, "GET", f"/api/v1/finances/pay/{self.desk.membership_id}/statement").json()
        self.assertEqual((statement["deducted"], statement["balance"]), (2000, statement["earned"] - 2000))
        # The member sees it on the register and marks it seen.
        page = self.desk.get("/finances?tab=register").content.decode()
        self.assertIn("data-my-deduction", page)
        self.assertIn("اتخصم من مرتبك عجز", page)
        deduction = r.json()["deduction"]["id"]
        self.assertEqual(call(self.owner, "POST", f"/api/v1/deductions/{deduction}/seen", {}).status_code, 404)  # not theirs
        self.assertEqual(call(self.desk, "POST", f"/api/v1/deductions/{deduction}/seen", {}).status_code, 200)
        self.assertNotIn("data-my-deduction", self.desk.get("/finances?tab=register").content.decode())
        # The report shows it; the owner can cancel it, and then take it again.
        report = self.owner.get(f"/finances/shifts/{self.shift}", headers={"X-Membership": str(self.owner.membership_id)})
        self.assertIn("data-void-deduction", report.content.decode())
        self.assertEqual(call(self.owner, "POST", f"/api/v1/deductions/{deduction}/void", {}).status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Deduction.objects.filter(clinic_id=self.a.id, voided_at__isnull=True).count(), 0)
        self.assertEqual(call(self.owner, "POST", url, {"amount": 3000}).status_code, 201)

    def test_never_from_a_shift_that_matched(self):
        call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": 500, "reason": "فكة"})
        with clinic_transaction(self.a.id, self.a.shard):
            from finance.models import Shift

            open_id = Shift.objects.filter(clinic_id=self.a.id, status="open").values_list("id", flat=True).first()
        call(self.desk, "POST", f"/api/v1/register/shifts/{open_id}/close", {"counted": 500})
        r = call(self.owner, "POST", f"/api/v1/register/shifts/{open_id}/deduct", {"amount": 100})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_short"))
