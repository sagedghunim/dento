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
        # Typing the name shows the ready items (Saged, 2026-11-25); the ones the clinic has are left out.
        self.item(name="جوانتي لاتكس")
        html = self.owner.get("/inventory").content.decode()
        self.assertIn('list="ready-items"', html)
        self.assertIn('<option value="جوانتي نيتريل" label="تعقيم ومستهلكات · ≈170 ج.م"', html)
        self.assertIn('data-unit="علبة"', html)
        self.assertNotIn('<option value="جوانتي لاتكس"', html)
        self.assertNotIn("ready-items", self.desk.get("/inventory").content.decode())  # adding items is the manager's

    def test_someone_with_use_only_sees_the_list_and_records_use(self):
        self.item()
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
        self.revoke("assistant", "inventory.use")
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
