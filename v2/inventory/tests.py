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

    def test_recording_a_procedure_deducts_its_materials_and_costs_the_line(self):
        composite = self.item("Composite", quantity=3, unit_cost=4000)
        gloves = self.item("Gloves", quantity=1, unit_cost=300)
        filling = self.procedure("Composite filling, one surface")
        r = call(self.owner, "PUT", f"/api/v1/inventory/materials/{filling['id']}",
                 {"lines": [{"item_id": composite["id"], "qty": 1}, {"item_id": gloves["id"], "qty": 1}]})
        self.assertEqual(r.status_code, 200, r.content)
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        r = call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 11})
        self.assertEqual(r.status_code, 201, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            from billing.models import InvoiceLine
            from clinical.models import VisitProcedure

            record = VisitProcedure.objects.get(clinic_id=self.a.id, appointment_id=visit["id"])
            self.assertEqual(record.internal_cost, 4300)
            self.assertEqual(InvoiceLine.objects.get(clinic_id=self.a.id, id=record.invoice_line_id).internal_cost, 4300)
            self.assertEqual(Movement.objects.filter(clinic_id=self.a.id, kind="use", visit_procedure_id=record.id,
                                                     patient_id=visit["patient"]["id"]).count(), 2)
        # The gloves ran out: the next recording is refused whole, nothing taken.
        r = call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 12})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "out_of_stock"))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Item.objects.get(clinic_id=self.a.id, id=composite["id"]).quantity, 2)
        # An item the place allows below zero doesn't stop the doctor.
        call(self.owner, "PATCH", f"/api/v1/inventory/items/{gloves['id']}", {"allow_negative": True})
        self.assertEqual(call(self.doc, "POST", url, {"procedure_id": filling["id"], "tooth": 12}).status_code, 201)

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
