"""The supplier backup and the "needs a supplier" list (design app-h1-supplier-backup.md)."""

import threading

from django.db import connections
from django.test import TransactionTestCase

from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from inventory.models import Item, OrderLine
from inventory.test_orders import OrderHelpers
from scheduling.testing import add_schedule


class BackupTests(OrderHelpers):
    def set_backup(self, supplier, backup):
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{supplier['id']}",
                 {"backup_id": backup["id"] if backup else None})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["supplier"]

    def missing(self, order, item, client=None):
        return call(client or self.desk, "POST", f"/api/v1/inventory/orders/{order['id']}/missing",
                    {"item_id": item["id"]})

    def receive(self, order, lines, close=False):
        r = call(self.desk, "POST", f"/api/v1/inventory/orders/{order['id']}/receive",
                 {"lines": [{"item_id": i["id"], "qty": q, "unit_price": 1000} for i, q in lines],
                  "paid": "outside", "close": close})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def draft_lines(self, supplier):
        for draft in self.drafts():
            if draft["supplier_id"] == supplier["id"]:
                return {l["item_id"]: l["qty"] for l in self.order(draft["id"])["lines"]}
        return {}

    def needs(self, client=None):
        r = call(client or self.desk, "GET", "/api/v1/inventory/needs")
        self.assertEqual(r.status_code, 200, r.content)
        return {n["item_id"]: n["qty"] for n in r.json()["items"]}

    def need_qty(self, item):
        with clinic_transaction(self.a.id, self.a.shard):
            return Item.objects.get(clinic_id=self.a.id, id=item["id"]).need_qty

    # --- The supplier's backup ---

    def test_a_supplier_takes_a_backup_checked_like_an_item_backup(self):
        main, other = self.supplier("Main Co"), self.supplier("Other Co")
        self.assertEqual(self.set_backup(main, other)["backup_id"], other["id"])
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{main['id']}", {"backup_id": main["id"]})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (400, {"backup_id": "same"}))
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{main['id']}", {"backup_id": 999999})
        self.assertEqual(r.status_code, 404, r.content)
        call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{other['id']}", {"is_active": False})
        r = call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{main['id']}", {"backup_id": other["id"]})
        self.assertEqual(r.status_code, 404, r.content)  # we no longer order from it
        self.assertIsNone(self.set_backup(main, None)["backup_id"])

    # --- "مش عنده" ---

    def test_not_available_moves_the_rest_to_the_suppliers_backup_and_closes_the_order(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        a = self.item("A", quantity=50, supplier=main)
        b = self.item("B", quantity=50, supplier=main)
        order = self.sent_order(main, (a, 10), (b, 3))
        self.receive(order, [(a, 4)])  # 6 of A still to come
        r = self.missing(order, a)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["moved"], [{"item_id": a["id"], "qty": 6, "to": "supplier",
                                               "supplier_id": backup["id"], "supplier_name": "Backup Co"}])
        line = {l["item_id"]: l for l in r.json()["order"]["lines"]}[a["id"]]
        self.assertEqual((line["missing"], r.json()["order"]["status"]), (6, "sent"))
        self.assertEqual(self.draft_lines(backup), {a["id"]: 6})
        r = self.missing(order, b)
        self.assertEqual(r.json()["order"]["status"], "done")  # every line is in or missing
        # A repeat on the closed order is harmless and moves nothing.
        again = self.missing(order, b)
        self.assertEqual((again.status_code, again.json()["moved"]), (200, []), again.content)
        self.assertEqual(self.draft_lines(backup), {a["id"]: 6, b["id"]: 3})

    def test_the_items_own_backup_comes_before_the_suppliers(self):
        main, sup_backup, item_backup = self.supplier("Main Co"), self.supplier("SB Co"), self.supplier("IB Co")
        self.set_backup(main, sup_backup)
        a = self.item("A", quantity=50, supplier=main, backup_supplier_id=item_backup["id"])
        order = self.sent_order(main, (a, 5))
        self.assertEqual(self.missing(order, a).json()["moved"][0]["supplier_id"], item_backup["id"])
        self.assertEqual(self.draft_lines(sup_backup), {})

    def test_an_inactive_backup_is_skipped_and_no_backup_goes_to_the_list(self):
        main, gone = self.supplier("Main Co"), self.supplier("Gone Co")
        self.set_backup(main, gone)
        call(self.owner, "PATCH", f"/api/v1/inventory/suppliers/{gone['id']}", {"is_active": False})
        a = self.item("A", quantity=1, threshold=3, supplier=main)  # low: the list shows 2 x 3 - 1 = 5
        order = self.sent_order(main, (a, 5))
        moved = self.missing(order, a).json()["moved"]
        self.assertEqual(moved, [{"item_id": a["id"], "qty": 5, "to": "list", "supplier_id": None,
                                  "supplier_name": None}])
        self.assertEqual(self.needs(), {a["id"]: 5})

    def test_one_step_only_a_backup_line_short_again_goes_to_the_list(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        self.set_backup(backup, main)  # A -> B and B -> A is harmless
        a = self.item("A", quantity=1, threshold=3, supplier=main)  # low: the list shows 2 x 3 - 1 = 5
        self.missing(self.sent_order(main, (a, 5)), a)
        draft = [d for d in self.drafts() if d["supplier_id"] == backup["id"]][0]
        self.assertEqual(call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send").status_code, 200)
        moved = self.missing(draft, a).json()["moved"]
        self.assertEqual(moved[0]["to"], "list")
        self.assertEqual((self.needs(), self.draft_lines(main)), ({a["id"]: 5}, {}))

    def test_a_moved_qty_adds_to_the_backups_draft_and_marks_the_line(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        a = self.item("A", quantity=50, supplier=main)
        call(self.desk, "POST", "/api/v1/inventory/orders", {"supplier_id": backup["id"],
                                                            "lines": [{"item_id": a["id"], "qty": 2}]})
        self.missing(self.sent_order(main, (a, 5)), a)
        self.assertEqual(self.draft_lines(backup), {a["id"]: 7})
        with clinic_transaction(self.a.id, self.a.shard):  # the whole line counts as backup now (documented)
            self.assertTrue(OrderLine.objects.get(clinic_id=self.a.id, item_id=a["id"], order__status="draft").is_backup)

    def test_not_available_needs_a_sent_order_and_a_line_on_it(self):
        main = self.supplier("Main Co")
        a, b = self.item("A", quantity=50, supplier=main), self.item("B", quantity=50, supplier=main)
        r = call(self.desk, "POST", "/api/v1/inventory/orders", {"supplier_id": main["id"],
                                                                "lines": [{"item_id": a["id"], "qty": 2}]})
        draft = r.json()["order"]
        self.assertEqual(self.missing(draft, a).status_code, 409)  # not sent yet
        call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/send")
        self.assertEqual(self.missing(draft, b).status_code, 400)  # not on the order
        self.assertEqual(self.missing({"id": 999999}, a).status_code, 404)

    # --- Receiving ---

    def test_closing_a_short_order_moves_exactly_the_short_qty(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        a = self.item("A", quantity=50, supplier=main)
        b = self.item("B", quantity=50, supplier=main)
        order = self.sent_order(main, (a, 10), (b, 2))
        answer = self.receive(order, [(a, 4), (b, 3)], close=True)  # B came over: fine
        self.assertEqual(answer["order"]["status"], "done")
        self.assertEqual([(m["item_id"], m["qty"], m["to"]) for m in answer["moved"]], [(a["id"], 6, "supplier")])
        self.assertEqual(self.draft_lines(backup), {a["id"]: 6})
        self.assertEqual(self.draft_lines(main), {})  # not back to the one who didn't bring it

    def test_an_order_closes_by_itself_when_the_rest_is_missing(self):
        main = self.supplier("Main Co")
        a, b = self.item("A", quantity=50, supplier=main), self.item("B", quantity=50, supplier=main)
        order = self.sent_order(main, (a, 10), (b, 2))
        self.missing(order, a)
        self.assertEqual(self.receive(order, [(b, 2)])["order"]["status"], "done")

    def test_a_late_receipt_on_a_missing_line_is_allowed_and_reported(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        a, b = self.item("A", quantity=50, supplier=main), self.item("B", quantity=50, supplier=main)
        order = self.sent_order(main, (a, 5), (b, 1))
        self.missing(order, a)
        answer = self.receive(order, [(a, 5)])
        self.assertEqual(answer["late"], [{"item_id": a["id"], "moved_to": "supplier", "supplier_name": "Backup Co"}])

    def test_a_receipt_lowers_what_the_list_waits_for(self):
        main = self.supplier("Main Co")
        a = self.item("A", quantity=1, threshold=5, supplier=main)
        self.missing(self.sent_order(main, (a, 5)), a)
        self.assertEqual(self.need_qty(a), 5)
        self.receive(self.sent_order(self.supplier("Other Co"), (a, 3)), [(a, 3)])
        self.assertEqual((self.need_qty(a), self.needs()), (2, {a["id"]: 6}))  # the list shows what the item lacks: 10 - 4

    def test_a_missed_item_with_enough_stock_is_not_on_the_list_or_the_badge(self):
        main = self.supplier("Main Co")
        a = self.item("A", quantity=50, supplier=main)
        self.missing(self.sent_order(main, (a, 5)), a)
        self.assertEqual(self.need_qty(a), 5)
        r = call(self.desk, "GET", "/api/v1/inventory/orders")
        self.assertEqual((self.needs(), r.json()["needs"]), ({}, 0))

    def test_cancelling_a_backup_order_sends_its_moved_lines_to_the_list(self):
        main, backup = self.supplier("Main Co"), self.supplier("Backup Co")
        self.set_backup(main, backup)
        a = self.item("A", quantity=1, threshold=3, supplier=main)  # low: the list shows 2 x 3 - 1 = 5
        self.missing(self.sent_order(main, (a, 5)), a)
        draft = [d for d in self.drafts() if d["supplier_id"] == backup["id"]][0]
        self.assertEqual(call(self.desk, "POST", f"/api/v1/inventory/orders/{draft['id']}/cancel").status_code, 200)
        self.assertEqual(self.needs(), {a["id"]: 5})

    # --- The list ---

    def test_the_list_has_low_items_with_no_supplier_once_and_refill_skips_listed_items(self):
        main = self.supplier("Main Co")
        loose = self.item("Loose", quantity=3, threshold=2)
        self.use(loose, 2)  # low, nobody sells it: 2 x 2 - 1 = 3
        a = self.item("A", quantity=50, threshold=2, supplier=main)
        self.missing(self.sent_order(main, (a, 5)), a)
        self.use(a, 49)  # low: on the list once, with what it lacks (2 x 2 - 1)
        self.assertEqual(self.needs(), {loose["id"]: 3, a["id"]: 3})
        call(self.desk, "POST", "/api/v1/inventory/orders/refill")
        self.assertEqual(self.draft_lines(main), {})
        r = call(self.desk, "GET", "/api/v1/inventory/orders")
        self.assertEqual(r.json()["needs"], 1)  # the badge counts the short ones only

    def test_picking_a_supplier_orders_from_it_and_a_repeat_adds_nothing(self):
        main, other = self.supplier("Main Co"), self.supplier("Other Co")
        a = self.item("A", quantity=50, supplier=main)
        self.missing(self.sent_order(main, (a, 5)), a)
        url = f"/api/v1/inventory/needs/{a['id']}/order"
        r = call(self.desk, "POST", url, {"supplier_id": other["id"], "qty": 4})
        self.assertEqual(r.status_code, 201, r.content)
        again = call(self.desk, "POST", url, {"supplier_id": other["id"], "qty": 4})
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual((self.draft_lines(other), self.needs()), ({a["id"]: 4}, {}))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertTrue(OrderLine.objects.get(clinic_id=self.a.id, item_id=a["id"], order__status="draft").is_backup)
        bad = call(self.desk, "POST", url, {"supplier_id": other["id"], "qty": 0})
        self.assertEqual(bad.status_code, 400)

    def test_a_low_item_with_no_supplier_can_be_picked_from_the_list(self):
        other = self.supplier("Other Co")
        loose = self.item("Loose", quantity=3, threshold=2)
        self.use(loose, 2)
        r = call(self.desk, "POST", f"/api/v1/inventory/needs/{loose['id']}/order",
                 {"supplier_id": other["id"], "qty": 3})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((self.draft_lines(other), self.needs()), ({loose["id"]: 3}, {}))

    def test_dismissing_and_setting_a_supplier_clear_the_list(self):
        main, other = self.supplier("Main Co"), self.supplier("Other Co")
        a = self.item("A", quantity=50, supplier=main)
        b = self.item("B", quantity=50, supplier=main)
        order = self.sent_order(main, (a, 5), (b, 5))
        self.missing(order, a)
        self.missing(order, b)
        r = call(self.desk, "POST", f"/api/v1/inventory/needs/{a['id']}/dismiss")
        self.assertEqual(r.status_code, 200, r.content)
        call(self.owner, "PATCH", f"/api/v1/inventory/items/{b['id']}", {"supplier_id": other["id"]})
        self.assertEqual(self.needs(), {})

    def test_another_place_cant_see_or_touch_the_list(self):
        main = self.supplier("Main Co")
        a = self.item("A", quantity=1, threshold=3, supplier=main)  # low: the list shows 2 x 3 - 1 = 5
        order = self.sent_order(main, (a, 5))
        self.missing(order, a)
        self.assertEqual(self.needs(self.b.client("owner")), {})
        self.assertEqual(self.missing(order, a, client=self.b.client("owner")).status_code, 404)
        r = call(self.b.client("owner"), "POST", f"/api/v1/inventory/needs/{a['id']}/dismiss")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.needs(), {a["id"]: 5})


class BackupConcurrencyTests(TransactionTestCase):
    """Two "مش عنده" taps on one line at once move the quantity once (the line lock)."""

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("bk", "shard_1")
        add_schedule(self.a, "owner")

    def test_two_not_available_at_once_move_once(self):
        owner = self.a.client("owner")
        main = call(owner, "POST", "/api/v1/inventory/suppliers", {"name": "Main Co", "phone": "01001234567"}).json()["supplier"]
        backup = call(owner, "POST", "/api/v1/inventory/suppliers", {"name": "Backup Co", "phone": "01001234567"}).json()["supplier"]
        call(owner, "PATCH", f"/api/v1/inventory/suppliers/{main['id']}", {"backup_id": backup["id"]})
        item = call(owner, "POST", "/api/v1/inventory/items",
                    {"name": "A", "quantity": 50, "supplier_id": main["id"]}).json()["item"]
        order = call(owner, "POST", "/api/v1/inventory/orders",
                     {"supplier_id": main["id"], "lines": [{"item_id": item["id"], "qty": 5}]}).json()["order"]
        call(owner, "POST", f"/api/v1/inventory/orders/{order['id']}/send")
        barrier, results = threading.Barrier(2), [None, None]

        def worker(index, role):
            try:
                client = self.a.client(role)
                barrier.wait()
                results[index] = call(client, "POST", f"/api/v1/inventory/orders/{order['id']}/missing",
                                      {"item_id": item["id"]}).json()["moved"]
            except Exception as error:
                results[index] = repr(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(0, "assistant")), threading.Thread(target=worker, args=(1, "owner"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(len(r) for r in results), [0, 1], results)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(list(OrderLine.objects.filter(clinic_id=self.a.id, order__supplier_id=backup["id"])
                                  .values_list("qty", flat=True)), [5])
