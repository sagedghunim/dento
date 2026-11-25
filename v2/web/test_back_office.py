"""The Inventory and Prescription screens (design F2, E3): who sees them, and what each person is offered."""

from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call


def fragment(client, url):
    return client.get(url, headers={"X-Membership": str(client.membership_id)})


class BackOfficeCase(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.owner = self.a.client("owner")      # inventory.manage, settings.medicines
        self.doctor = self.a.client("doctor")    # inventory.use, settings.medicines
        self.desk = self.a.client("assistant")   # inventory.use only

    def revoke(self, role, *codes):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership(role).id).update(revoked=list(codes))

    def item(self, name="Gloves box", quantity=3, threshold=5):
        r = call(self.owner, "POST", "/api/v1/inventory/items", {"name": name, "unit": "box", "quantity": quantity,
                                                                 "threshold": threshold, "unit_cost": 4500})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["item"]


class InventoryPageTests(BackOfficeCase):
    def test_a_manager_gets_every_action(self):
        item = self.item()
        r = self.owner.get("/inventory")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("Gloves box", html)
        self.assertIn("رصيد منخفض", html)  # 3 at or below the minimum of 5
        for marker in ("data-new-item", 'data-inv="purchase"', 'data-inv="count"', 'data-inv="waste"',
                       'data-inv="edit"', 'data-inv="remove"', 'data-inv="use"', "data-defaults", 'id="item-dialog"',
                       'id="materials-dialog"'):
            self.assertIn(marker, html, marker)
        self.assertIn(f'&quot;id&quot;: {item["id"]}', html)  # the card's values, escaped in the attribute
        # Every owner has Inventory in the side bar (Saged, 2026-11-25; it had left the managing owner's on 2026-11-12).
        self.assertIn('href="/inventory" aria-current="page"', self.doctor.get("/inventory").content.decode())

    def test_a_manager_picks_a_new_item_from_the_ready_list(self):
        # A tap drops the ready items under the name, each with its unit (Saged, 2026-11-25); the ones the clinic
        # has are left out. Our own list, not the browser's datalist.
        self.item(name="جوانتي لاتكس")
        html = self.owner.get("/inventory").content.decode()
        self.assertIn('aria-controls="ready-items"', html)
        self.assertNotIn("<datalist", html)
        row = html.split('data-ready="جوانتي نيتريل"')[1].split("</li>")[0]
        self.assertIn("تعقيم ومستهلكات", row)
        self.assertIn('<bdi dir="auto">علبة</bdi>', row)
        self.assertIn("≈ 170 ج.م", row)
        self.assertNotIn('data-ready="جوانتي لاتكس"', html)
        self.assertNotIn("{#", html)  # a comment never shows as text
        self.revoke("assistant", "inventory.manage")
        self.assertNotIn("ready-items", self.desk.get("/inventory").content.decode())  # adding items is the manager's

    def test_someone_with_use_only_sees_the_list_and_records_use(self):
        self.item()
        self.revoke("assistant", "inventory.manage", "inventory.order")
        r = self.desk.get("/inventory")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Gloves box", html)
        self.assertIn('data-inv="use"', html)
        self.assertIn('data-inv="history"', html)
        for marker in ("data-new-item", 'data-inv="purchase"', 'data-inv="count"', 'data-inv="waste"',
                       'data-inv="edit"', 'data-inv="remove"', "data-defaults", 'id="item-dialog"',
                       'id="purchase-dialog"', 'id="materials-dialog"', "data-removed"):
            self.assertNotIn(marker, html, marker)

    def test_someone_without_a_stock_code_is_refused(self):
        self.revoke("assistant", "inventory.use", "inventory.order", "inventory.manage")
        desk = self.a.client("assistant")
        self.assertEqual(desk.get("/inventory").status_code, 403)
        self.assertEqual(fragment(desk, "/inventory/items").status_code, 403)
        self.assertNotIn('href="/inventory"', desk.get("/patients").content.decode())

    def test_the_cards_fragment_and_removed_items(self):
        item = self.item()
        r = fragment(self.desk, "/inventory/items")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Gloves box", r.content.decode())
        self.assertEqual(self.desk.get("/inventory/items").status_code, 409)  # without X-Membership, as the API
        call(self.owner, "PATCH", f"/api/v1/inventory/items/{item['id']}", {"is_active": False})
        self.assertNotIn("Gloves box", fragment(self.owner, "/inventory/items").content.decode())
        removed = fragment(self.owner, "/inventory/items?removed=1").content.decode()
        self.assertIn("Gloves box", removed)
        self.assertIn('data-inv="restore"', removed)
        # The desk manages the stock by default (Saged, 2026-11-25); the owner's switch takes it away.
        self.assertIn("Gloves box", fragment(self.desk, "/inventory/items?removed=1").content.decode())
        self.revoke("assistant", "inventory.manage")
        self.assertNotIn("Gloves box", fragment(self.desk, "/inventory/items?removed=1").content.decode())

    def test_an_items_history_newest_first(self):
        item = self.item()
        r = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/purchases", {"qty": 4, "unit_price": 5000})
        self.assertEqual(r.status_code, 201, r.content)
        r = fragment(self.desk, f"/inventory/items/{item['id']}/movements")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertLess(html.index("شراء"), html.index("جرد"))  # the purchase, then the opening quantity
        self.assertIn("+4", html)
        self.assertEqual(r["X-Next"], "")

    def test_another_clinics_item_is_not_found(self):
        other = self.clinic("b", "shard_2")
        r = call(other.client("owner"), "POST", "/api/v1/inventory/items", {"name": "Theirs"})
        theirs = r.json()["item"]["id"]
        r = fragment(self.desk, f"/inventory/items/{theirs}/movements")
        self.assertEqual(r.status_code, 404)

    def test_the_section_shows_in_the_navigation(self):
        self.assertIn('href="/inventory"', self.desk.get("/patients").content.decode())


class PrescriptionPageTests(BackOfficeCase):
    def test_the_lists_with_add_edit_and_remove(self):
        r = call(self.doctor, "POST", "/api/v1/medicines", {"name": "Zentomed", "generic": "zentocillin",
                                                            "strength": "1 g", "default_dose": "قرص كل 12 ساعة"})
        self.assertEqual(r.status_code, 201, r.content)
        medicine = r.json()["medicine"]
        r = call(self.doctor, "POST", "/api/v1/prescription-templates",
                 {"name": "روشتة اختبار", "notes": "ثلج", "items": [{"medicine_id": medicine["id"]}]})
        self.assertEqual(r.status_code, 201, r.content)
        r = self.doctor.get("/prescriptions")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Zentomed", html)
        self.assertIn("zentocillin", html)
        self.assertIn("روشتة اختبار", html)
        for marker in ("data-new-medicine", "data-new-template", 'data-med="edit"', 'data-med="remove"',
                       'data-rx="edit"', 'data-rx="remove"', 'id="template-dialog"'):
            self.assertIn(marker, html, marker)
        self.assertIn('href="/prescriptions" aria-current="page"', html)

    def test_the_fragments_follow_a_removal(self):
        r = call(self.doctor, "POST", "/api/v1/medicines", {"name": "Bt Drug"})
        medicine = r.json()["medicine"]
        self.assertIn("Bt Drug", fragment(self.doctor, "/prescriptions/medicines").content.decode())
        call(self.doctor, "DELETE", f"/api/v1/medicines/{medicine['id']}")
        self.assertNotIn("Bt Drug", fragment(self.doctor, "/prescriptions/medicines").content.decode())
        self.assertEqual(fragment(self.doctor, "/prescriptions/templates").status_code, 200)

    def test_someone_without_the_code_is_refused(self):
        self.assertEqual(self.desk.get("/prescriptions").status_code, 403)
        self.assertEqual(fragment(self.desk, "/prescriptions/medicines").status_code, 403)
        self.assertEqual(fragment(self.desk, "/prescriptions/templates").status_code, 403)
        self.assertNotIn('href="/prescriptions"', self.desk.get("/patients").content.decode())
        self.assertIn('href="/prescriptions"', self.doctor.get("/patients").content.decode())


class OrderPageTests(BackOfficeCase):
    """Purchase orders and suppliers on the Inventory page (design H1)."""

    def supplier(self, name="Dental Co"):
        r = call(self.owner, "POST", "/api/v1/inventory/suppliers", {"name": name, "phone": "01001234567"})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["supplier"]

    def draft(self, supplier, item):
        r = call(self.desk, "POST", "/api/v1/inventory/orders",
                 {"supplier_id": supplier["id"], "lines": [{"item_id": item["id"], "qty": 4}]})
        self.assertEqual(r.status_code, 201, r.content)

    def test_the_desk_opens_on_its_orders_and_the_side_bar_counts_the_drafts(self):
        supplier = self.supplier()
        self.draft(supplier, self.item())
        html = self.desk.get("/inventory").content.decode()
        for marker in ('data-part="orders"', 'data-part="suppliers"', 'id="order-dialog"', 'id="receive-dialog"',
                       'id="new-order-dialog"', 'id="order-data"', "orders.js", "data-refill"):
            self.assertIn(marker, html, marker)
        self.assertLess(html.index('data-part="orders"'), html.index("Gloves box"))  # orders first for the desk
        self.assertIn("طلبات الشراء (<span class=\"num\">1</span>)", html)  # the side bar counts the unsent drafts (Latin digits here)
        self.assertIn('id="account-dialog"', html)  # the desk sees money
        self.assertIn('id="supplier-dialog"', html)  # the desk manages suppliers by default (Saged, 2026-11-25)
        self.revoke("assistant", "inventory.manage")
        self.assertNotIn('id="supplier-dialog"', self.desk.get("/inventory").content.decode())  # unless the owner says no

    def test_the_owner_opens_on_the_items_and_manages_suppliers(self):
        self.supplier()
        html = self.owner.get("/inventory").content.decode()
        self.assertLess(html.index("Gloves box" if "Gloves box" in html else 'data-new-item'),
                        html.index('data-part="orders"'))
        for marker in ('id="supplier-dialog"', "data-new-supplier", 'name="backup_supplier_id"', 'name="max_qty"'):
            self.assertIn(marker, html, marker)

    def test_without_the_order_code_there_are_no_orders(self):
        self.supplier()
        html = self.doctor.get("/inventory").content.decode()
        for marker in ('data-part="orders"', 'id="order-dialog"', "orders.js", 'id="order-data"'):
            self.assertNotIn(marker, html, marker)
        self.revoke("assistant", "inventory.order")
        self.assertNotIn("طلبات الشراء", self.a.client("assistant").get("/inventory").content.decode())

    def test_the_desk_without_money_gets_no_prices_or_account(self):
        self.revoke("assistant", "money.view")
        html = self.a.client("assistant").get("/inventory").content.decode()
        self.assertIn('id="receive-dialog"', html)
        self.assertNotIn('id="account-dialog"', html)
        self.assertNotIn("data-receive-total", html)

    def test_order_only_sees_the_orders_without_the_items(self):
        self.revoke("assistant", "inventory.use")
        r = self.a.client("assistant").get("/inventory")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-part="orders"', r.content.decode())
        self.assertNotIn('data-inv="use"', r.content.decode())

    def test_the_owner_switches_auto_send_on_team(self):
        html = self.owner.get("/team").content.decode()
        self.assertIn('name="orders_auto_send"', html)
