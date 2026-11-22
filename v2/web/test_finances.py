"""The Finances screen (design F1, F3, F4, F5): who sees which tab, and that money stays with finances.view."""

from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call

MONEY_MARKERS = ('data-stat="collected"', 'data-stat="profit"', 'data-stat="staff"', "المحصَّل", "الربح",
                 "مستحقات المرضى", "أجور الفريق", "?tab=month", "?tab=pay", "?tab=owed")


def fragment(client, url):
    return client.get(url, headers={"X-Membership": str(client.membership_id)})


class FinancesCase(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.owner = self.a.client("owner")      # every code
        self.desk = self.a.client("assistant")   # cash.collect and cash.shift

    def codes(self, role, granted=(), revoked=()):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership(role).id).update(
                granted=list(granted), revoked=list(revoked))
        return self.a.client(role)

    def expense(self, client, amount=12345, note="فاتورة كهرباء"):
        categories = call(client, "GET", "/api/v1/expense-categories").json()["items"]
        r = call(client, "POST", "/api/v1/expenses", {"category_id": categories[0]["id"], "amount": amount, "note": note})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["expense"]


class FinancesPageTests(FinancesCase):
    def test_the_owner_sees_every_tab(self):
        self.expense(self.owner)
        r = self.owner.get("/finances")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn('href="/finances" aria-current="page"', html)
        for key in ("month", "register", "expenses", "pay", "owed"):
            self.assertIn(f'href="?tab={key}', html, key)
        self.assertIn('data-stat="collected"', html)
        self.assertIn("123.45", html)  # the month's expenses
        for tab in ("register", "expenses", "pay", "owed"):
            r = self.owner.get(f"/finances?tab={tab}")
            self.assertEqual(r.status_code, 200, tab)
            self.assertIn(f'data-tab="{tab}"', r.content.decode(), tab)

    def test_each_tab_offers_its_actions(self):
        expenses = self.owner.get("/finances?tab=expenses").content.decode()
        for marker in ("data-new-expense", "data-new-category", "data-rename-category", "data-remove-category",
                       'id="expense-dialog"', 'id="void-dialog"'):
            self.assertIn(marker, expenses, marker)
        register = self.owner.get("/finances?tab=register").content.decode()
        for marker in ("data-new-expense data-from-drawer", 'id="expense-dialog"', 'name="category_id"',
                       'data-cash="in"', 'id="cash-dialog"', 'id="close-dialog"', "دفع مصروف", "حركة أخرى للخزنة"):
            self.assertIn(marker, register, marker)
        # No generic cash in / cash out buttons: money out is an expense by its category.
        self.assertNotIn('data-cash="out"', register)
        self.assertTrue("data-open-shift" in register or "data-close-shift" in register)  # one, by the shift's state
        self.assertIn("تُفتح تلقائيًا مع أول دفعة نقدية", register)  # says plainly that it opens by itself
        pay = self.owner.get("/finances?tab=pay").content.decode()
        for marker in ("data-statement", "data-edit-rule", 'id="rule-dialog"', 'id="payout-dialog"'):
            self.assertIn(marker, pay, marker)

    def test_the_register_shows_the_open_shift_after_cash_in(self):
        r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": 5000, "reason": "فكة"})
        self.assertEqual(r.status_code, 201, r.content)
        html = self.desk.get("/finances").content.decode()
        self.assertIn("data-expected", html)
        self.assertIn("data-close-shift", html)
        self.assertIn("فكة", html)
        r = fragment(self.desk, "/finances/part?tab=register")
        self.assertEqual(r.status_code, 200)
        self.assertIn("data-shift-id", r.content.decode())
        shift = r.content.decode().split('data-shift-id="')[1].split('"')[0]
        # A shift's report and the past shifts are the owner's (Saged, 2026-11-22).
        self.assertEqual(fragment(self.desk, f"/finances/shifts/{shift}").status_code, 403)
        r = fragment(self.owner, f"/finances/shifts/{shift}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("فكة", r.content.decode())
        self.assertEqual(fragment(self.desk, "/finances/shifts").status_code, 403)
        self.assertEqual(fragment(self.owner, "/finances/shifts").status_code, 200)

    def test_the_register_lists_drawer_expenses_by_category_and_other_stays(self):
        items = {c["code"]: c for c in call(self.owner, "GET", "/api/v1/expense-categories").json()["items"]}
        r = call(self.owner, "POST", "/api/v1/expenses", {"category_id": items["lab"]["id"], "amount": 7000,
                                                          "from_shift": True, "note": "تركيبة"})
        self.assertEqual(r.status_code, 201, r.content)
        call(self.owner, "POST", "/api/v1/expenses", {"category_id": items["rent"]["id"], "amount": 9000})  # not drawer
        register = fragment(self.owner, "/finances/part?tab=register").content.decode()
        moves = register.split("حركة الخزنة في هذه الوردية")[1]
        self.assertIn(items["lab"]["name"], moves)
        self.assertIn("تركيبة", moves)
        self.assertNotIn(items["rent"]["name"], moves)
        shift = register.split('data-shift-id="')[1].split('"')[0]
        self.assertIn(items["lab"]["name"], fragment(self.owner, f"/finances/shifts/{shift}").content.decode())
        # "Other" can't be removed from the screen, and comes back if the API removed it; it is offered last.
        expenses = self.owner.get("/finances?tab=expenses").content.decode()
        row = expenses.split(f'&quot;id&quot;: {items["other"]["id"]},')[1].split("</li>")[0]
        self.assertNotIn("data-remove-category", row)
        call(self.owner, "PATCH", f"/api/v1/expense-categories/{items['other']['id']}", {"is_active": False})
        register = self.owner.get("/finances?tab=register").content.decode()
        chips = register.split("data-category-chips")[1].split("</fieldset>")[0]
        self.assertEqual(chips.count('name="category_id"'), 5)
        self.assertIn('data-code="other"', chips.split('name="category_id"')[-1])

    def test_the_desk_pays_an_expense_out_of_the_drawer_only(self):
        categories = {c["code"]: c for c in call(self.owner, "GET", "/api/v1/expense-categories").json()["items"]}
        r = call(self.desk, "POST", "/api/v1/drawer-expenses", {"category_id": categories["rent"]["id"], "amount": 5000})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNotNone(r.json()["expense"]["shift_id"])
        register = fragment(self.desk, "/finances/part?tab=register").content.decode()
        self.assertIn(categories["rent"]["name"], register.split("حركة الخزنة في هذه الوردية")[1])
        # Voiding, other days and the expenses list stay the owner's.
        self.assertEqual(call(self.desk, "POST", "/api/v1/expenses", {"category_id": categories["rent"]["id"],
                                                                      "amount": 100}).status_code, 403)
        expense_id = r.json()["expense"]["id"]
        self.assertEqual(call(self.desk, "POST", f"/api/v1/expenses/{expense_id}/void", {}).status_code, 403)
        r = call(self.desk, "POST", "/api/v1/drawer-expenses", {"category_id": categories["other"]["id"], "amount": 100})
        self.assertEqual(r.status_code, 400)  # "Other" needs a note

    def test_the_front_desk_sees_the_register_only(self):
        r = self.desk.get("/finances")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-tab="register"', html)
        self.assertIn('data-cash="in"', html)  # the quiet "other drawer movement"
        for marker in MONEY_MARKERS + ("?tab=expenses",):
            self.assertNotIn(marker, html, marker)
        # The desk pays rent, electricity, salaries… out of the drawer, by category (Saged, 2026-11-22).
        self.assertIn("data-new-expense", html)
        self.assertIn('data-drawer-only', html)
        self.assertIn('data-tab="register"', self.desk.get("/finances?tab=month").content.decode())  # no way in
        self.assertIn('href="/finances"', self.desk.get("/patients").content.decode())
        for url in ("/finances/part?tab=month", "/finances/part?tab=pay", "/finances/part?tab=expenses", "/finances/owed",
                    f"/finances/pay/{self.a.membership('doctor').id}", "/finances/expenses"):
            self.assertEqual(fragment(self.desk, url).status_code, 403, url)

    def test_the_expenses_only_member_sees_no_money_totals(self):
        clerk = self.codes("assistant", granted=["expenses.manage"], revoked=["cash.collect", "cash.shift"])
        self.expense(self.owner, note="Bt rent")
        r = clerk.get("/finances")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-tab="expenses"', html)
        self.assertIn("Bt rent", html)
        self.assertNotIn("data-new-payout", html)
        for marker in MONEY_MARKERS + ("?tab=register", "data-cash=", 'name="from_shift"'):
            self.assertNotIn(marker, html, marker)
        for url in ("/finances/part?tab=month", "/finances/part?tab=register", "/finances/shifts", "/finances/owed"):
            self.assertEqual(fragment(clerk, url).status_code, 403, url)
        self.assertEqual(fragment(clerk, "/finances/part?tab=expenses").status_code, 200)
        self.assertEqual(fragment(clerk, "/finances/expenses").status_code, 200)

    def test_without_a_money_code_the_page_is_refused(self):
        desk = self.codes("assistant", revoked=["cash.collect", "cash.shift"])
        self.assertEqual(desk.get("/finances").status_code, 403)
        self.assertEqual(fragment(desk, "/finances/part?tab=register").status_code, 403)
        self.assertNotIn('href="/finances"', desk.get("/patients").content.decode())

    def test_view_without_pay_reads_but_cannot_edit_pay(self):
        doctor = self.codes("doctor", granted=["money.view", "finances.view"])  # no money code without money.view
        r = doctor.get("/finances?tab=pay")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("data-statement", html)
        for marker in ("data-edit-rule", 'id="rule-dialog"', 'id="payout-dialog"', "data-new-payout"):
            self.assertNotIn(marker, html, marker)
        member = self.a.membership("assistant").id
        r = fragment(doctor, f"/finances/pay/{member}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'data-member-id="{member}"', r.content.decode())

    def test_typed_text_is_escaped(self):
        self.expense(self.owner, note='<img src=x onerror="alert(1)">')
        html = self.owner.get("/finances?tab=expenses").content.decode()
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertIn("&lt;img src=x", html)

    def test_another_clinics_shift_is_not_found(self):
        other = self.clinic("b", "shard_2")
        r = call(other.client("owner"), "POST", "/api/v1/register/movements", {"kind": "in", "amount": 100, "reason": "x"})
        theirs = r.json()["shift"]["id"]
        self.assertEqual(fragment(self.owner, f"/finances/shifts/{theirs}").status_code, 404)

    def test_english_mode_and_a_past_month(self):
        self.owner.cookies["django_language"] = "en"
        r = self.owner.get("/finances?tab=month&month=2026-01")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="2026-01" selected', html)
        r = self.owner.get("/finances?tab=month&month=nonsense")
        self.assertEqual(r.status_code, 200)
