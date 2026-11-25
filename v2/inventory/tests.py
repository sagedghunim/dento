"""M3 stock (design 5.4, F2): items, movements, materials deducted by a recorded procedure, Today's low stock."""

import threading

from django.db import connections
from django.test import TransactionTestCase

from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from inventory.models import Item, Movement
from scheduling.testing import add_schedule, services_of


class StockTests(M2TestCase):
    def item(self, name="Composite", quantity=10, threshold=2, unit_cost=4000, client=None, **extra):
        r = call(client or self.owner, "POST", "/api/v1/inventory/items",
                 {"name": name, "quantity": quantity, "threshold": threshold, "unit_cost": unit_cost, **extra})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["item"]

    def test_purchase_moves_the_average_cost_and_records_an_expense(self):
        item = self.item(quantity=10, unit_cost=4000)
        r = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/purchases", {"qty": 10, "unit_price": 6000})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["item"]["quantity"], r.json()["item"]["unit_cost"]), (20, 5000))
        expenses = call(self.owner, "GET", "/api/v1/expenses").json()
        self.assertEqual((expenses["total"], expenses["items"][0]["purchase"]), (60000, True))
        voided = call(self.owner, "POST", f"/api/v1/expenses/{expenses['items'][0]['id']}/void")
        self.assertEqual((voided.status_code, voided.json()["error"]["code"]), (409, "purchase_expense"))

    def test_use_is_all_or_nothing_and_never_below_zero(self):
        one, two = self.item("Gloves", quantity=5), self.item("Gauze", quantity=1)
        r = call(self.desk, "POST", "/api/v1/inventory/uses",
                 {"lines": [{"item_id": one["id"], "qty": 2}, {"item_id": two["id"], "qty": 2}]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "out_of_stock"), r.content)
        self.assertEqual([i["id"] for i in r.json()["error"]["items"]], [two["id"]])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Item.objects.get(clinic_id=self.a.id, id=one["id"]).quantity, 5)
        r = call(self.desk, "POST", "/api/v1/inventory/uses", {"lines": [{"item_id": one["id"], "qty": 5}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"][0]["quantity"], 0)
        bad = call(self.desk, "POST", "/api/v1/inventory/uses", {"lines": [{"item_id": one["id"], "qty": 0}]})
        self.assertEqual(bad.status_code, 400)

    def test_a_count_needs_the_quantity_seen_and_writes_the_difference(self):
        item = self.item(quantity=10)
        stale = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/count", {"counted": 7, "expected": 9})
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))
        r = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/count",
                 {"counted": 7, "expected": 10, "reason": "monthly count"})
        self.assertEqual(r.json()["item"]["quantity"], 7)
        negative = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/count", {"counted": -1, "expected": 7})
        self.assertEqual(negative.status_code, 400)
        moves = call(self.owner, "GET", f"/api/v1/inventory/items/{item['id']}/movements").json()["items"]
        self.assertEqual([(m["kind"], m["qty"], m["balance_after"]) for m in moves], [("adjust", -3, 7), ("adjust", 10, 10)])

    def recipe(self, procedure, *lines):
        r = call(self.owner, "PUT", f"/api/v1/inventory/materials/{procedure['id']}",
                 {"lines": [{"item_id": item["id"], "qty": qty} for item, qty in lines]})
        self.assertEqual(r.status_code, 200, r.content)

    def quantities(self, *items):
        with clinic_transaction(self.a.id, self.a.shard):
            return [Item.objects.get(clinic_id=self.a.id, id=i["id"]).quantity for i in items]

    def finish(self, visit, uses=None, client=None, expect=200):
        body = {"status": "in_room"} if uses is None else {"status": "in_room", "uses": uses}
        r = call(client or self.doc, "POST", f"/api/v1/appointments/{visit['id']}/finish", body)
        self.assertEqual(r.status_code, expect, r.content)
        return r

    def test_recording_takes_nothing_the_finish_takes_the_cards_list(self):
        # Saged, 2026-11-25: the materials come into the visit's card; what it holds comes off at the finish.
        composite = self.item("Composite", quantity=3, unit_cost=4000)
        gloves = self.item("Gloves", quantity=1, unit_cost=300)
        bond = self.item("Bond", quantity=10, unit_cost=100)
        filling = self.procedure("Composite filling, one surface")
        self.recipe(filling, (composite, 1), (gloves, 1))
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        for tooth in (11, 12):
            self.assertEqual(call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": tooth}).status_code, 201)
        self.assertEqual(self.quantities(composite, gloves), [3, 1])  # nothing taken yet, even past what is left
        from clinical.models import VisitProcedure

        with clinic_transaction(self.a.id, self.a.shard):
            self.assertFalse(Movement.objects.filter(clinic_id=self.a.id, appointment_id=visit["id"]).exists())
            self.assertEqual(set(VisitProcedure.objects.filter(clinic_id=self.a.id, appointment_id=visit["id"])
                                 .values_list("stock_taken", "internal_cost")), {(False, 0)})
        # The doctor lowered the composite, raised the gloves past the stock's number and added bond.
        self.finish(visit, [{"item_id": composite["id"], "qty": 1}, {"item_id": gloves["id"], "qty": 3},
                            {"item_id": bond["id"], "qty": 2}])
        self.assertEqual(self.quantities(composite, gloves, bond), [2, 0, 8])
        with clinic_transaction(self.a.id, self.a.shard):
            from audit.models import AuditChange as Change

            moves = list(Movement.objects.filter(clinic_id=self.a.id, item_id=gloves["id"]).order_by("id")
                         .values_list("kind", "qty", "balance_after"))
            self.assertEqual(moves[-2:], [("adjust", 2, 3), ("use", -3, 0)])  # the stock's number was 2 short
            self.assertTrue(Change.objects.filter(clinic_id=self.a.id, action="item.short", target_id=gloves["id"]).exists())
            self.assertEqual(set(VisitProcedure.objects.filter(clinic_id=self.a.id, appointment_id=visit["id"])
                                 .values_list("stock_taken", flat=True)), {True})
        self.assertIn("Gloves", self.owner.get("/alerts").content.decode())

    def test_a_finish_without_the_card_takes_the_procedures_materials_once(self):
        composite = self.item("Composite", quantity=5, unit_cost=4000)
        filling = self.procedure("Composite filling, one surface")
        self.recipe(filling, (composite, 2))
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 11})
        self.finish(visit, client=self.desk)  # from the day's list: no card, the recipe as it is
        self.assertEqual(self.quantities(composite), [3])
        self.finish(visit, client=self.desk)  # a repeat takes nothing more
        self.assertEqual(self.quantities(composite), [3])
        # Back in the room: only what is recorded after it comes off at the next finish.
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/reopen", {"status": "done"})
        self.assertEqual(r.status_code, 200, r.content)
        call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 12})
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        data = html.split('id="visit-data" type="application/json">')[1].split("</script>")[0]
        import json

        stock = json.loads(data)["stock"]
        self.assertEqual([u["qty"] for u in stock["used"]], [2])
        self.assertEqual(len(stock["pending"]), 1)
        self.finish(visit)
        self.assertEqual(self.quantities(composite), [1])

    def test_recorded_after_the_finish_or_in_another_tab_still_comes_off(self):
        # Reviews, 2026-11-25: no finish is coming for a record on a done visit; a record the card never saw
        # comes off with its recipe at the finish.
        composite = self.item("Composite", quantity=10, unit_cost=4000)
        filling = self.procedure("Composite filling, one surface")
        self.recipe(filling, (composite, 2))
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        seen = call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 11}).json()["procedure"]["id"]
        call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 12})  # another tab: not on the card
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/finish",
                 {"status": "in_room", "uses": [{"item_id": composite["id"], "qty": 1}], "records": [seen]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.quantities(composite), [7])  # 1 from the card + 2 by the unseen one's recipe
        r = call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 13})
        self.assertEqual(r.status_code, 201, r.content)  # a late record on the finished visit comes off at once
        self.assertEqual(self.quantities(composite), [5])

    def test_the_cards_list_needs_the_stock_code(self):
        composite = self.item("Composite", quantity=5)
        visit = self.in_room()
        bad = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/finish",
                   {"status": "in_room", "uses": [{"item_id": composite["id"], "qty": 0}]})
        self.assertEqual(bad.status_code, 400)
        self.set_codes("doctor", revoked=["inventory.use"])
        self.finish(visit, [{"item_id": composite["id"], "qty": 1}], expect=403)
        self.assertEqual(self.quantities(composite), [5])

    def test_low_stock_shows_on_today_and_moves_its_version(self):
        item = self.item("Needles", quantity=3, threshold=2)
        before = call(self.desk, "GET", "/api/v1/today").json()
        self.assertEqual(before["low_stock"], [])
        call(self.desk, "POST", "/api/v1/inventory/uses", {"lines": [{"item_id": item["id"], "qty": 1}]})
        after = call(self.desk, "GET", "/api/v1/today").json()
        self.assertEqual([i["id"] for i in after["low_stock"]], [item["id"]])
        self.assertGreater(after["version"], before["version"])
        self.set_codes("assistant", revoked=["inventory.use"])
        self.assertEqual(call(self.a.client("assistant"), "GET", "/api/v1/today").json()["low_stock"], [])

    def test_codes_and_another_places_items(self):
        mine = self.item()
        theirs = self.item("Theirs", client=self.b.client("owner"))
        self.set_codes("assistant", revoked=["inventory.manage"])  # the desk has it by default (Saged, 2026-11-25)
        self.assertEqual(call(self.desk, "POST", "/api/v1/inventory/items", {"name": "x"}).status_code, 403)
        for method, url, body in (
                ("PATCH", f"/api/v1/inventory/items/{theirs['id']}", {"name": "y"}),
                ("GET", f"/api/v1/inventory/items/{theirs['id']}/movements", None),
                ("POST", f"/api/v1/inventory/items/{theirs['id']}/purchases", {"qty": 1, "unit_price": 1}),
                ("POST", f"/api/v1/inventory/items/{theirs['id']}/count", {"counted": 1, "expected": 10}),
                ("POST", f"/api/v1/inventory/items/{theirs['id']}/waste", {"qty": 1, "reason": "x"}),
                ("POST", "/api/v1/inventory/uses", {"lines": [{"item_id": theirs["id"], "qty": 1}]})):
            r = call(self.owner, method, url, body)
            self.assertEqual(r.status_code, 404, (url, r.content))
        filling = self.procedure("Composite filling, one surface")
        r = call(self.b.client("owner"), "PUT", f"/api/v1/inventory/materials/{filling['id']}",
                 {"lines": [{"item_id": theirs["id"], "qty": 1}]})
        self.assertEqual(r.status_code, 404)
        r = call(self.owner, "PUT", f"/api/v1/inventory/materials/{filling['id']}",
                 {"lines": [{"item_id": theirs["id"], "qty": 1}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual([i["id"] for i in call(self.owner, "GET", "/api/v1/inventory/items").json()["items"]],
                         [mine["id"]])

    def test_defaults_add_items_and_dental_materials_once(self):
        r = call(self.owner, "POST", "/api/v1/inventory/defaults")
        self.assertEqual(r.status_code, 200, r.content)
        count = len(r.json()["items"])
        self.assertGreater(count, 5)
        self.assertEqual(len(call(self.owner, "POST", "/api/v1/inventory/defaults").json()["items"]), count)
        filling = self.procedure("Composite filling, one surface")
        self.assertTrue(call(self.owner, "GET", f"/api/v1/inventory/materials/{filling['id']}").json()["items"])
        # Seeded items start at zero and may go below it until the place counts them, so an uncounted
        # material never refuses a recording with 409 out_of_stock.
        self.assertTrue(all(i["allow_negative"] for i in r.json()["items"]))
        visit = self.in_room()
        r = call(self.owner, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": filling["id"],
                                                                                     "tooth": 16, "surfaces": ["O"]})
        self.assertEqual(r.status_code, 201, r.content)


class StockConcurrencyTests(TransactionTestCase):
    """5.4 on real PostgreSQL: two uses of the last units take effect once; stock never goes below zero."""

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("sc", "shard_1")
        add_schedule(self.a, "owner")
        self.services = services_of(self.a)

    def test_two_uses_of_the_last_unit(self):
        owner = self.a.client("owner")
        item = call(owner, "POST", "/api/v1/inventory/items", {"name": "Last one", "quantity": 1}).json()["item"]
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(index, role):
            try:
                client = self.a.client(role)
                barrier.wait()
                results[index] = call(client, "POST", "/api/v1/inventory/uses",
                                      {"lines": [{"item_id": item["id"], "qty": 1}]}).status_code
            except Exception as error:
                results[index] = repr(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(0, "assistant")), threading.Thread(target=worker, args=(1, "doctor"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [201, 409], results)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Item.objects.get(clinic_id=self.a.id, id=item["id"]).quantity, 0)
            self.assertEqual(Movement.objects.filter(clinic_id=self.a.id, item_id=item["id"], kind="use").count(), 1)


class VisitUseTests(M2TestCase):
    item = StockTests.item

    def test_the_visit_card_has_the_items_the_recipes_and_what_is_pending(self):
        wire = self.item("Wire", quantity=20)
        filling = self.procedure("Composite filling, one surface")
        r = call(self.owner, "PUT", f"/api/v1/inventory/materials/{filling['id']}", {"lines": [{"item_id": wire["id"], "qty": 2}]})
        self.assertEqual(r.status_code, 200, r.content)
        visit = self.in_room()
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": filling["id"], "tooth": 11})
        self.assertEqual(r.status_code, 201, r.content)
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-use-card", html)
        self.assertIn("js/use.js", html)
        self.assertNotIn("{#", html)
        data = html.split('id="visit-data" type="application/json">')[1].split("</script>")[0]
        import json

        stock = json.loads(data)["stock"]
        self.assertEqual([(i["name"], i["quantity"]) for i in stock["items"]], [("Wire", 20)])
        self.assertEqual(stock["recipes"][str(filling["id"])], [{"item_id": wire["id"], "qty": 2}])
        self.assertEqual([(p["procedure_id"], p["qty"]) for p in stock["pending"]], [(filling["id"], 1)])
        self.assertEqual(stock["used"], [])
        self.assertNotIn("unit_cost", data)  # stock only: no money in the visit's list
        with clinic_transaction(self.a.id, self.a.shard):  # never the bill
            from billing.models import InvoiceLine

            self.assertFalse(InvoiceLine.objects.filter(clinic_id=self.a.id, kind="material").exists())

    def test_a_doctor_locked_out_of_stock_sees_no_use_card(self):
        self.set_codes("doctor", revoked=["inventory.use"])
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertNotIn("data-use-card", html)
        self.assertNotIn('"stock"', html)
