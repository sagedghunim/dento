"""Suppliers, purchase orders and what we owe them (design H1)."""

import uuid
from unittest import mock

from clinical.testing import M2TestCase
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call
from finance.models import Expense
from inventory.models import Item, Order


class OrderTests(M2TestCase):
    def supplier(self, name="Dental Co", phone="01001234567"):
        r = call(self.owner, "POST", "/api/v1/inventory/suppliers", {"name": name, "phone": phone})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["supplier"]

    def item(self, name="Composite", quantity=5, threshold=2, supplier=None, **extra):
        body = {"name": name, "quantity": quantity, "threshold": threshold, "unit_cost": 4000, **extra}
        if supplier:
            body["supplier_id"] = supplier["id"]
        r = call(self.owner, "POST", "/api/v1/inventory/items", body)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["item"]

    def use(self, item, qty, client=None):
        r = call(client or self.desk, "POST", "/api/v1/inventory/uses", {"lines": [{"item_id": item["id"], "qty": qty}]})
        self.assertEqual(r.status_code, 201, r.content)

    def drafts(self, client=None):
        r = call(client or self.desk, "GET", "/api/v1/inventory/orders")
        self.assertEqual(r.status_code, 200, r.content)
        return [o for o in r.json()["orders"] if o["status"] == "draft"]

    def order(self, order_id, client=None):
        return call(client or self.desk, "GET", f"/api/v1/inventory/orders/{order_id}").json()["order"]

    def sent_order(self, supplier, *items):
        r = call(self.desk, "POST", "/api/v1/inventory/orders",
                 {"supplier_id": supplier["id"], "lines": [{"item_id": i["id"], "qty": q} for i, q in items]})
        self.assertEqual(r.status_code, 201, r.content)
        order = r.json()["order"]
        sent = call(self.desk, "POST", f"/api/v1/inventory/orders/{order['id']}/send")
        self.assertEqual(sent.status_code, 200, sent.content)
        return order

    def no_money_desk(self):
        _user, membership = self.a.people["assistant"]
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=membership.id).update(revoked=["money.view"])

    # --- Refill ---

    def test_low_stock_puts_the_item_on_its_suppliers_draft_once(self):
        supplier = self.supplier()
        composite = self.item(quantity=4, threshold=2, supplier=supplier)
        gloves = self.item("Gloves", quantity=10, threshold=5, supplier=supplier, max_qty=50)
        self.use(composite, 1)  # 3: still above
        self.assertEqual(self.drafts(), [])
        self.use(composite, 1)  # 2: crosses
        self.use(composite, 1)  # 1: already on the draft
        self.use(gloves, 6)     # 4: crosses, same supplier, same draft
        drafts = self.drafts()
        self.assertEqual(len(drafts), 1)
        lines = {l["item_id"]: l["qty"] for l in self.order(drafts[0]["id"])["lines"]}
        self.assertEqual(lines, {composite["id"]: 2, gloves["id"]: 46})  # 2 x 2 - 2; 50 - 4

    def test_an_item_without_a_supplier_or_a_count_down_also_refills(self):
        loose = self.item("Loose", quantity=3, threshold=2)
        self.use(loose, 2)
        self.assertEqual(self.drafts(), [])
        supplier = self.supplier()
        r = call(self.owner, "PATCH", f"/api/v1/inventory/items/{loose['id']}", {"supplier_id": supplier["id"]})
        self.assertEqual(r.status_code, 200, r.content)  # a supplier newly set on a low item
        self.assertEqual(len(self.drafts()), 1)
        counted = self.item("Counted", quantity=9, threshold=3, supplier=self.supplier("Other"))
        r = call(self.owner, "POST", f"/api/v1/inventory/items/{counted['id']}/count", {"counted": 1, "expected": 9})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.drafts()), 2)

    def test_a_line_removed_from_the_draft_doesnt_come_straight_back(self):
        supplier = self.supplier()
        one = self.item("One", quantity=3, threshold=2, supplier=supplier)
        two = self.item("Two", quantity=3, threshold=2, supplier=supplier)
        self.use(one, 1)
        self.use(two, 1)
        draft = self.drafts()[0]
        r = call(self.desk, "PUT", f"/api/v1/inventory/orders/{draft['id']}",
                 {"lines": [{"item_id": one["id"], "qty": 7}], "note": "urgent"})
        self.assertEqual(r.status_code, 200, r.content)
        self.use(two, 1)  # still low, no new crossing
        order = self.order(draft["id"])
        self.assertEqual(([(l["item_id"], l["qty"]) for l in order["lines"]], order["note"]), ([(one["id"], 7)], "urgent"))
        r = call(self.desk, "POST", "/api/v1/inventory/orders/refill")  # the button brings it back
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual({l["item_id"] for l in self.order(draft["id"])["lines"]}, {one["id"], two["id"]})

    def test_the_item_and_backup_suppliers_are_checked(self):
        supplier = self.supplier()
        item = self.item("Item", quantity=9)
        same = call(self.owner, "PATCH", f"/api/v1/inventory/items/{item['id']}",
                    {"supplier_id": supplier["id"], "backup_supplier_id": supplier["id"]})
        self.assertEqual(same.status_code, 400, same.content)
        other = call(self.b.client("owner"), "POST", "/api/v1/inventory/suppliers", {"name": "B's"}).json()["supplier"]
        foreign = call(self.owner, "PATCH", f"/api/v1/inventory/items/{item['id']}", {"supplier_id": other["id"]})
        self.assertEqual(foreign.status_code, 404, foreign.content)

    # --- Send and cancel ---

    def test_send_answers_a_whatsapp_link_without_prices_and_a_repeat_is_the_same(self):
        supplier = self.supplier(phone="0100 123 4567")
        self.assertEqual(supplier["phone"], "201001234567")
        item = self.item(quantity=3, supplier=supplier)
        self.use(item, 1)
        draft = self.drafts()[0]
        r = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["link"].startswith("https://wa.me/201001234567?text="))
        self.assertIn("Composite", body["text"])
        self.assertNotIn("40", body["text"])  # no prices
        again = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send")
        self.assertEqual((again.status_code, again.json()["text"]), (200, body["text"]))
        self.assertEqual(self.order(draft["id"])["status"], "sent")

    def test_the_doctor_sees_no_orders_or_suppliers(self):
        for url in ("/api/v1/inventory/orders", "/api/v1/inventory/suppliers"):
            self.assertEqual(call(self.doc, "GET", url).status_code, 403)

    def test_cancelling_a_sent_order_puts_its_items_on_a_new_draft(self):
        supplier = self.supplier()
        item = self.item(quantity=3, supplier=supplier)
        self.use(item, 1)
        draft = self.drafts()[0]
        call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send")
        r = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/cancel")
        self.assertEqual((r.status_code, r.json()["order"]["status"]), (200, "cancelled"), r.content)
        drafts = self.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertNotEqual(drafts[0]["id"], draft["id"])
        late = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/receive",
                    {"lines": [{"item_id": item["id"], "qty": 1, "unit_price": 100}], "paid": "outside"})
        self.assertEqual((late.status_code, late.json()["error"]["code"]), (409, "order_status"))

    # --- Receive ---

    def test_receive_with_money_needs_how_it_was_paid_and_moves_the_cost(self):
        supplier = self.supplier()
        item = self.item(quantity=10, threshold=2, supplier=supplier)  # 10 at 40.00
        order = self.sent_order(supplier, (item, 10))
        url = f"/api/v1/inventory/orders/{order['id']}/receive"
        unpaid = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 10, "unit_price": 6000}]})
        self.assertEqual((unpaid.status_code, unpaid.json()["error"]["fields"]), (400, {"paid": "required"}))
        no_price = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 10}], "paid": "outside"})
        self.assertEqual(no_price.status_code, 400)
        empty = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 10, "unit_price": 6000}],
                                              "paid": "drawer"})
        self.assertEqual((empty.status_code, empty.json()["error"]["code"]), (409, "not_enough_cash"))
        r = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 10, "unit_price": 6000}],
                                          "paid": "outside"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["order"]["status"], "done")
        with clinic_transaction(self.a.id, self.a.shard):
            stored = Item.objects.get(clinic_id=self.a.id, id=item["id"])
            expense = Expense.objects.get(clinic_id=self.a.id, id=r.json()["expense_id"])
        self.assertEqual((stored.quantity, stored.unit_cost), (20, 5000))
        self.assertEqual((expense.amount, expense.purchase, expense.shift_id is not None), (60000, True, False))

    def test_on_account_makes_a_bill_and_a_payment_settles_it(self):
        supplier = self.supplier()
        item = self.item(quantity=10, supplier=supplier)
        order = self.sent_order(supplier, (item, 5))
        r = call(self.owner, "POST", f"/api/v1/inventory/orders/{order['id']}/receive",
                 {"lines": [{"item_id": item["id"], "qty": 5, "unit_price": 2000}], "paid": "account"})
        self.assertEqual((r.status_code, r.json()["expense_id"]), (201, None), r.content)
        account = call(self.owner, "GET", f"/api/v1/inventory/suppliers/{supplier['id']}/account").json()
        self.assertEqual(account["supplier"]["owed"], 10000)
        today = account["entries"][0]["day"]
        paid = call(self.owner, "POST", f"/api/v1/inventory/suppliers/{supplier['id']}/payments",
                    {"amount": 12000, "day": today, "from_shift": False})
        self.assertEqual(paid.status_code, 201, paid.content)
        owed = call(self.owner, "GET", "/api/v1/inventory/suppliers").json()["suppliers"][0]["owed"]
        self.assertEqual(owed, -2000)  # credit
        entry = paid.json()["entry"]
        self.assertEqual(call(self.desk, "POST", f"/api/v1/inventory/vendor-entries/{entry['id']}/void").status_code, 403)
        voided = call(self.owner, "POST", f"/api/v1/inventory/vendor-entries/{entry['id']}/void")
        self.assertEqual((voided.status_code, voided.json()["voided"]), (200, True))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertIsNotNone(Expense.objects.get(clinic_id=self.a.id, id=entry["expense_id"]).voided_at)
        self.assertEqual(call(self.owner, "GET", "/api/v1/inventory/suppliers").json()["suppliers"][0]["owed"], 10000)

    def test_without_money_view_the_desk_receives_quantities_only(self):
        supplier = self.supplier()
        item = self.item(quantity=10, supplier=supplier)
        order = self.sent_order(supplier, (item, 5))
        self.no_money_desk()
        url = f"/api/v1/inventory/orders/{order['id']}/receive"
        priced = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 5, "unit_price": 100}]})
        self.assertEqual(priced.status_code, 403)
        paid = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 5}], "paid": "account"})
        self.assertEqual(paid.status_code, 403)
        r = call(self.desk, "POST", url, {"lines": [{"item_id": item["id"], "qty": 5}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.json()["order"]["lines"][0]["unit_price"])
        with clinic_transaction(self.a.id, self.a.shard):
            stored = Item.objects.get(clinic_id=self.a.id, id=item["id"])
            self.assertEqual(Expense.objects.filter(clinic_id=self.a.id).count(), 0)
        self.assertEqual((stored.quantity, stored.unit_cost), (15, 4000))
        self.assertEqual(call(self.desk, "GET", "/api/v1/inventory/suppliers").json()["suppliers"][0]["owed"], None)

    def test_a_short_delivery_stays_open_and_closing_it_refills_the_rest(self):
        supplier = self.supplier()
        item = self.item(quantity=3, threshold=2, supplier=supplier)
        self.use(item, 2)  # 1: on the draft for 3
        draft = self.drafts()[0]
        call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send")
        url = f"/api/v1/inventory/orders/{draft['id']}/receive"
        r = call(self.owner, "POST", url, {"lines": [{"item_id": item["id"], "qty": 1, "unit_price": 4000}],
                                           "paid": "outside"})
        self.assertEqual((r.status_code, r.json()["order"]["status"], r.json()["order"]["lines"][0]["received"]),
                         (201, "sent", 1), r.content)
        r = call(self.owner, "POST", url, {"lines": [{"item_id": item["id"], "qty": 0}], "close": True})
        self.assertEqual((r.status_code, r.json()["order"]["status"]), (201, "done"), r.content)
        self.assertEqual(len(self.drafts()), 1)  # still low (2 <= 2): back on a new draft

    def test_more_than_ordered_is_allowed_and_a_repeat_key_receives_once(self):
        supplier = self.supplier()
        item = self.item(quantity=10, supplier=supplier)
        order = self.sent_order(supplier, (item, 5))
        body = {"lines": [{"item_id": item["id"], "qty": 6, "unit_price": 4000}], "paid": "outside"}
        url = f"/api/v1/inventory/orders/{order['id']}/receive"
        key = str(uuid.uuid4())
        first = call(self.owner, "POST", url, body, key=key)
        again = call(self.owner, "POST", url, body, key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201), again.content)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Item.objects.get(clinic_id=self.a.id, id=item["id"]).quantity, 16)

    # --- Auto-send ---

    def test_auto_send_claims_the_draft_and_a_failure_puts_it_back(self):
        from inventory.jobs import auto_send

        supplier = self.supplier()
        item = self.item(quantity=3, supplier=supplier)
        with clinic_transaction(self.a.id, self.a.shard):
            from clinics.models import Clinic

            clinic = Clinic.objects.get(id=self.a.id)
            clinic.settings = {**clinic.settings, "orders_auto_send": True}
            clinic.save(update_fields=["settings"])
        with mock.patch("core.whatsapp.configured", return_value=False):
            self.use(item, 1)
        draft = self.drafts()[0]
        self.assertEqual(auto_send(self.a.id, self.a.shard, draft["id"]), "skipped")  # no provider: nothing runs
        with mock.patch("core.whatsapp.configured", return_value=True), \
                mock.patch("core.whatsapp.send", side_effect=RuntimeError("down")):
            self.assertEqual(auto_send(self.a.id, self.a.shard, draft["id"]), "failed")
        self.assertEqual(self.order(draft["id"])["status"], "draft")
        with mock.patch("core.whatsapp.configured", return_value=True), \
                mock.patch("core.whatsapp.send", return_value="m1"):
            self.assertEqual(auto_send(self.a.id, self.a.shard, draft["id"]), "sent")
            self.assertEqual(auto_send(self.a.id, self.a.shard, draft["id"]), "skipped")  # one claim wins
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Order.objects.get(clinic_id=self.a.id, id=draft["id"]).status, "sent")

    # --- Closed orders, a page at a time ---

    def test_closed_orders_come_a_page_at_a_time(self):
        supplier = self.supplier()
        composite = self.item(supplier=supplier)
        made = []
        for _ in range(3):
            r = call(self.desk, "POST", "/api/v1/inventory/orders",
                     {"supplier_id": supplier["id"], "lines": [{"item_id": composite["id"], "qty": 1}]})
            self.assertEqual(r.status_code, 201, r.content)
            made.append(r.json()["order"]["id"])
            self.assertEqual(call(self.desk, "POST", f"/api/v1/inventory/orders/{made[-1]}/cancel").status_code, 200)
        with mock.patch("inventory.order_views.CLOSED_PAGE", 2):
            first = call(self.desk, "GET", "/api/v1/inventory/orders?state=closed").json()
            self.assertEqual([o["id"] for o in first["orders"]], made[:0:-1])  # newest first
            self.assertTrue(first["next"])
            second = call(self.desk, "GET", f"/api/v1/inventory/orders?state=closed&after={first['next']}").json()
            self.assertEqual([o["id"] for o in second["orders"]], [made[0]])
            self.assertIsNone(second["next"])
        self.assertIsNone(call(self.desk, "GET", "/api/v1/inventory/orders").json()["next"])
        self.assertEqual(call(self.desk, "GET", "/api/v1/inventory/orders?state=closed&after=x").status_code, 400)


class SupplierRedesignTests(M2TestCase):
    """Usual visit days, items ticked in the supplier's dialog, the owner's order message (Saged, 2026-11-25)."""

    supplier, item, use, drafts = OrderTests.supplier, OrderTests.item, OrderTests.use, OrderTests.drafts

    def links(self, *items):
        with clinic_transaction(self.a.id, self.a.shard):
            return {i["id"]: Item.objects.filter(clinic_id=self.a.id, id=i["id"]).values_list(
                "supplier_id", "backup_supplier_id").get() for i in items}

    def test_visit_days_and_time_are_saved_and_checked(self):
        r = call(self.owner, "POST", "/api/v1/inventory/suppliers",
                 {"name": "Weekly", "visit_days": [5, 1, 1], "visit_time": "11:30"})
        self.assertEqual(r.status_code, 201, r.content)
        supplier = r.json()["supplier"]
        self.assertEqual((supplier["visit_days"], supplier["visit_time"]), ([1, 5], "11:30"))
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}", {"visit_time": ""})
        self.assertEqual((r.json()["supplier"]["visit_time"], r.json()["supplier"]["visit_days"]), ("", [1, 5]))
        for bad in ({"visit_days": [7]}, {"visit_days": [True]}, {"visit_days": "1"}, {"visit_time": "25:00"},
                    {"visit_time": "9:30"}):
            r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}", bad)
            self.assertEqual(r.status_code, 400, (bad, r.content))

    def test_ticked_items_move_to_the_supplier_and_unticked_ones_leave_it(self):
        first, second = self.supplier("First"), self.supplier("Second")
        a = self.item("A", quantity=9, supplier=first)
        b = self.item("B", quantity=9, supplier=first, backup_supplier_id=second["id"])
        c = self.item("C", quantity=9)
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{second['id']}", {"item_ids": [b["id"], c["id"]]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(sorted(r.json()["item_ids"]), sorted([b["id"], c["id"]]))
        # b moves from First and no longer has Second as its backup; a stays with First.
        self.assertEqual(self.links(a, b, c), {a["id"]: (first["id"], None), b["id"]: (second["id"], None),
                                               c["id"]: (second["id"], None)})
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{second['id']}", {"item_ids": [c["id"]]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.links(b, c), {b["id"]: (None, None), c["id"]: (second["id"], None)})

    def test_a_new_supplier_takes_its_ticked_items(self):
        a = self.item("A", quantity=9)
        r = call(self.owner, "POST", "/api/v1/inventory/suppliers", {"name": "New", "item_ids": [a["id"]]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.links(a), {a["id"]: (r.json()["supplier"]["id"], None)})

    def test_linking_refuses_other_places_items_and_stopped_suppliers(self):
        supplier = self.supplier()
        theirs = call(self.b.client("owner"), "POST", "/api/v1/inventory/items",
                      {"name": "Theirs", "quantity": 1, "threshold": 0}).json()["item"]
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}", {"item_ids": [theirs["id"]]})
        self.assertEqual(r.status_code, 404, r.content)
        a = self.item("A", quantity=9)
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}",
                 {"is_active": False, "item_ids": [a["id"]]})
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self.links(a), {a["id"]: (None, None)})  # nothing of the request stayed

    def test_the_desk_manages_suppliers_and_items_by_default_and_the_doctor_does_not(self):
        r = call(self.desk, "POST", "/api/v1/inventory/suppliers", {"name": "By the desk"})
        self.assertEqual(r.status_code, 201, r.content)
        r = call(self.doc, "POST", "/api/v1/inventory/suppliers", {"name": "By the doctor"})
        self.assertEqual(r.status_code, 403, r.content)

    def test_the_order_message_follows_the_owners_template(self):
        supplier = self.supplier("Nile Dental")
        item = self.item(quantity=3, supplier=supplier)
        self.use(item, 1)
        draft = self.drafts()[0]
        template = "Hi {المورد}, {clinic} needs:\n{items}\nBye"
        r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"order_message": template}, "expected": {"order_message": ""}})
        self.assertEqual(r.status_code, 200, r.content)
        text = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send").json()["text"]
        self.assertEqual(text.splitlines()[0], f"Hi Nile Dental, {self.a.entry.name} needs:")
        self.assertIn("- Composite: 2", text)
        self.assertTrue(text.endswith("Bye"))

    def test_a_name_that_looks_like_a_word_is_filled_once(self):
        supplier = self.supplier("{items}")
        item = self.item(quantity=3, supplier=supplier)
        self.use(item, 1)
        draft = self.drafts()[0]
        call(self.owner, "PUT", "/api/v1/settings/clinic",
             {"changes": {"order_message": "To {المورد}:\n{الأصناف}"}, "expected": {"order_message": ""}})
        text = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send").json()["text"]
        self.assertEqual(text.splitlines()[0], "To {items}:")
        self.assertEqual(text.count("- Composite"), 1)

    def test_a_template_without_the_items_word_still_sends_the_items(self):
        supplier = self.supplier()
        item = self.item(quantity=3, supplier=supplier)
        self.use(item, 1)
        draft = self.drafts()[0]
        call(self.owner, "PUT", "/api/v1/settings/clinic",
             {"changes": {"order_message": "Hello"}, "expected": {"order_message": ""}})
        text = call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send").json()["text"]
        self.assertTrue(text.startswith("Hello\n- Composite"), text)

    def test_only_the_owner_sets_the_order_message(self):
        r = call(self.desk, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"order_message": "x"}, "expected": {"order_message": ""}})
        self.assertIn(r.status_code, (403, 400), r.content)
        r = call(self.owner, "PUT", "/api/v1/settings/clinic",
                 {"changes": {"order_message": "x" * 1001}, "expected": {"order_message": ""}})
        self.assertEqual(r.status_code, 400, r.content)

    def test_a_low_item_ticked_to_a_supplier_goes_on_its_draft(self):
        supplier = self.supplier()
        low = self.item("Low", quantity=1, threshold=2)
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}", {"item_ids": [low["id"]]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([d["supplier_id"] for d in self.drafts()], [supplier["id"]])

    def test_an_item_moved_to_another_supplier_leaves_the_old_draft(self):
        old, new = self.supplier("Old"), self.supplier("New")
        low = self.item("Low", quantity=3, threshold=2, supplier=old)
        self.use(low, 1)
        self.assertEqual([d["supplier_id"] for d in self.drafts()], [old["id"]])
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{new['id']}", {"item_ids": [low["id"]]})
        self.assertEqual(r.status_code, 200, r.content)
        lines = {d["supplier_id"]: [l["item_id"] for l in OrderTests.order(self, d["id"])["lines"]] for d in self.drafts()}
        self.assertEqual(lines.get(new["id"]), [low["id"]])
        self.assertFalse(lines.get(old["id"]))
        # The old supplier's side of the move is in its own change log (Saged, 2026-11-26).
        from audit.models import AuditChange

        with clinic_transaction(self.a.id, self.a.shard):
            row = AuditChange.objects.filter(clinic_id=self.a.id, action="supplier.items", target_type="supplier",
                                             target_id=old["id"]).values_list("details", flat=True).get()
        self.assertEqual((row["before"], row["after"]), ({"item_ids": [low["id"]]}, {"item_ids": []}))
        self.assertEqual(row["to_supplier_id"], new["id"])

    def test_an_unticked_item_leaves_the_suppliers_own_draft(self):
        supplier = self.supplier()
        low = self.item("Low", quantity=3, threshold=2, supplier=supplier)
        self.use(low, 1)
        self.assertEqual([d["supplier_id"] for d in self.drafts()], [supplier["id"]])
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}", {"item_ids": []})
        self.assertEqual(r.status_code, 200, r.content)
        lines = [l["item_id"] for d in self.drafts() for l in OrderTests.order(self, d["id"])["lines"]]
        self.assertNotIn(low["id"], lines)
