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


class KitTests(M2TestCase):
    """Packages (Saged, 2026-11-25): a stock manager sets them once; the doctor picks one in the visit and its items
    come into the visit's use together. Stock only, never the bill."""

    item = StockTests.item

    def kit(self, name, lines, client=None, method="POST", kit_id=None, expect=201):
        url = "/api/v1/inventory/kits" + (f"/{kit_id}" if kit_id else "")
        r = call(client or self.owner, method, url, {"name": name, "lines": lines})
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def test_a_manager_makes_edits_and_deletes_a_package(self):
        wire, bands = self.item("Wire", quantity=20), self.item("Bands", quantity=40)
        kit = self.kit("Braces", [{"item_id": wire["id"], "qty": 2}, {"item_id": bands["id"], "qty": 4}])["kit"]
        self.assertEqual([(l["name"], l["qty"]) for l in kit["lines"]], [("Bands", 4), ("Wire", 2)])
        dup = self.kit("Braces", [{"item_id": wire["id"], "qty": 1}], expect=409)
        self.assertEqual(dup["error"]["fields"], {"name": "duplicate"})
        kit = self.kit("Braces, upper", [{"item_id": wire["id"], "qty": 1}], method="PUT", kit_id=kit["id"], expect=200)["kit"]
        self.assertEqual((kit["name"], [(l["name"], l["qty"]) for l in kit["lines"]]), ("Braces, upper", [("Wire", 1)]))
        listed = call(self.doc, "GET", "/api/v1/inventory/kits").json()["kits"]
        self.assertEqual([k["name"] for k in listed], ["Braces, upper"])
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/inventory/kits/{kit['id']}").status_code, 200)
        self.assertEqual(call(self.owner, "GET", "/api/v1/inventory/kits").json()["kits"], [])
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/inventory/kits/{kit['id']}").status_code, 404)
        with clinic_transaction(self.a.id, self.a.shard):  # setting a package never touches stock
            self.assertEqual(Item.objects.get(clinic_id=self.a.id, id=wire["id"]).quantity, 20)

    def test_a_package_save_sent_again_answers_the_same(self):
        # Its answer was lost and the same key comes again (backend review, 2026-11-25): the package, not a crash.
        wire = self.item("Wire")
        body = {"name": "Braces", "lines": [{"item_id": wire["id"], "qty": 1}]}
        first = call(self.owner, "POST", "/api/v1/inventory/kits", body, key="0b7c1a52-5d4e-4c39-9d1e-2f1a7a6e9c01")
        again = call(self.owner, "POST", "/api/v1/inventory/kits", body, key="0b7c1a52-5d4e-4c39-9d1e-2f1a7a6e9c01")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(again.json()["kit"]["id"], first.json()["kit"]["id"])
        self.assertEqual(len(call(self.owner, "GET", "/api/v1/inventory/kits").json()["kits"]), 1)

    def test_only_stock_managers_change_packages_and_never_another_places(self):
        wire = self.item("Wire")
        self.kit("Braces", [{"item_id": wire["id"], "qty": 1}], client=self.doc, expect=403)
        empty = call(self.owner, "POST", "/api/v1/inventory/kits", {"name": "Empty", "lines": []})
        self.assertEqual(empty.status_code, 400)
        kit = self.kit("Braces", [{"item_id": wire["id"], "qty": 1}])["kit"]
        other = self.b.client("owner")
        self.assertEqual(call(other, "GET", "/api/v1/inventory/kits").json()["kits"], [])
        self.kit("Mine", [{"item_id": wire["id"], "qty": 1}], client=other, method="PUT", kit_id=kit["id"], expect=400)  # A's item is not B's
        self.assertEqual(call(other, "DELETE", f"/api/v1/inventory/kits/{kit['id']}").status_code, 404)
        self.kit("Theirs", [{"item_id": wire["id"], "qty": 1}], client=other, expect=400)
        r = call(self.owner, "PATCH", f"/api/v1/inventory/items/{wire['id']}", {"is_active": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.kit("Old wire", [{"item_id": wire["id"], "qty": 1}], expect=400)

    def test_the_visit_shows_items_packages_and_what_was_taken_by_hand(self):
        wire = self.item("Wire", quantity=20)
        self.kit("Braces", [{"item_id": wire["id"], "qty": 2}])
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-use-card", html)
        self.assertIn("js/use.js", html)
        self.assertNotIn("{#", html)
        r = call(self.doc, "POST", "/api/v1/inventory/uses",
                 {"lines": [{"item_id": wire["id"], "qty": 3}], "appointment_id": visit["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["items"][0]["quantity"], 17)
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        data = html.split('id="visit-data" type="application/json">')[1].split("</script>")[0]
        import json

        stock = json.loads(data)["stock"]
        self.assertEqual([(i["name"], i["quantity"]) for i in stock["items"]], [("Wire", 17)])
        self.assertEqual([(k["name"], k["lines"]) for k in stock["kits"]], [("Braces", [{"item_id": wire["id"], "qty": 2}])])
        self.assertEqual([(u["name"], u["qty"]) for u in stock["used"]], [("Wire", 3)])
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
