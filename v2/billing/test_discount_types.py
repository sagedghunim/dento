"""Discount types (Saged, 2026-11-13): the clinic defines its discounts, the doctor suggests one, the desk applies it.

- Managing needs settings.manage; at most 12 active; names unique among the active ones.
- Applying a type the desk may apply needs billing.collect; any other needs billing.discount. A percent is taken from
  the subtotal in whole piastres and stays true when lines change. The invoice keeps the type and its name.
- The doctor's suggestion is stored on the visit and changes no money.
"""

import threading
import uuid

from django.db import IntegrityError, connections, transaction
from django.test import TransactionTestCase
from django.urls import get_resolver

from audit.models import AuditChange
from billing.models import DiscountType, Invoice
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from scheduling.testing import add_schedule, services_of


class DiscountTestCase(M2TestCase):
    def setUp(self):
        super().setUp()
        self.fee = self.services_a["consultation"].price

    def kind(self, name="Friends", kind="percent", value=10, client=None, expect=201, **extra):
        r = call(client or self.owner, "POST", "/api/v1/discount-types", {"name": name, "kind": kind, "value": value, **extra})
        if expect is not None:
            self.assertEqual(r.status_code, expect, r.content)
        return r.json()["discount_type"] if r.status_code == 201 else r

    def invoice(self, client=None, **body):
        """An unpaid invoice for a visit of the owner (the desk's default booking)."""
        visit = self.book(**body).json()["appointment"]
        return call(client or self.desk, "POST", f"/api/v1/appointments/{visit['id']}/invoice").json()["invoice"]

    def apply(self, invoice, kind, client=None, expected=None):
        return call(client or self.desk, "PUT", f"/api/v1/invoices/{invoice['id']}/discount-type",
                    {"type_id": kind["id"] if kind else None,
                     "expected_discount": invoice["discount"] if expected is None else expected})

    def add_line(self, invoice, client=None, **body):
        body = body or {"kind": "procedure", "service_id": self.services_a["procedure"].id}
        r = call(client or self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines", body)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["invoice"]

    def remove_line(self, invoice, line, client=None):
        return call(client or self.desk, "DELETE", f"/api/v1/invoices/{invoice['id']}/lines/{line['id']}")

    def pay(self, invoice, amount):
        r = call(self.desk, "POST", f"/api/v1/appointments/{invoice['appointment']['id']}/payments",
                 {"amount": amount, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)

    def changes(self, action):
        with clinic_transaction(self.a.id, self.a.shard):
            return list(AuditChange.objects.filter(clinic_id=self.a.id, action=action).order_by("id")
                        .values_list("details", flat=True))

    def stop(self, kind):
        r = call(self.owner, "PATCH", f"/api/v1/discount-types/{kind['id']}", {"is_active": False})
        self.assertEqual(r.status_code, 200, r.content)


class ManageTests(DiscountTestCase):
    def test_the_owner_adds_edits_and_stops_and_nobody_else_does(self):
        friends = self.kind()
        self.assertEqual((friends["name"], friends["kind"], friends["value"], friends["desk_can_apply"], friends["is_active"]),
                         ("Friends", "percent", 10, True, True))
        staff = self.kind("Staff", "fixed", 5000, desk_can_apply=False)
        self.assertEqual((staff["kind"], staff["value"], staff["desk_can_apply"]), ("fixed", 5000, False))
        self.assertEqual(self.kind("Desk's own", client=self.desk, expect=None).status_code, 403)
        self.assertEqual(call(self.desk, "PATCH", f"/api/v1/discount-types/{friends['id']}", {"value": 90}).status_code, 403)
        self.assertEqual(call(self.desk, "GET", "/api/v1/discount-types").status_code, 403)
        r = call(self.owner, "PATCH", f"/api/v1/discount-types/{friends['id']}", {"name": "Family", "value": 15})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["discount_type"]["name"], r.json()["discount_type"]["value"]), ("Family", 15))
        self.stop(staff)
        names = [t["name"] for t in call(self.owner, "GET", "/api/v1/discount-types").json()["items"]]
        self.assertEqual(names, ["Family"])
        everything = call(self.owner, "GET", "/api/v1/discount-types?all=1").json()["items"]
        self.assertEqual([(t["name"], t["is_active"]) for t in everything], [("Family", True), ("Staff", False)])
        self.assertEqual(len(self.changes("discount_type.create")), 2)
        self.assertEqual(self.changes("discount_type.update")[0], {"before": {"name": "Friends", "value": 10},
                                                                    "after": {"name": "Family", "value": 15}})

    def test_bad_values_are_refused_with_a_400(self):
        for body in ({"name": "", "kind": "percent", "value": 10}, {"name": "x" * 61, "kind": "percent", "value": 10},
                     {"name": "A", "kind": "half", "value": 10}, {"name": "A", "kind": "percent", "value": 0},
                     {"name": "A", "kind": "percent", "value": 101}, {"name": "A", "kind": "fixed", "value": 0},
                     {"name": "A", "kind": "fixed", "value": -5}, {"name": "A", "kind": "percent", "value": "ten"},
                     {"name": "A", "kind": "percent", "value": 12.5}, {"name": "A", "kind": "percent", "value": True},
                     {"name": "A", "kind": "fixed", "value": 10**13}, {"name": "A", "kind": "percent"},
                     {"name": "A", "kind": "percent", "value": 10, "desk_can_apply": "yes"}):
            r = call(self.owner, "POST", "/api/v1/discount-types", body)
            self.assertEqual(r.status_code, 400, (body, r.content))
        friends = self.kind()
        url = f"/api/v1/discount-types/{friends['id']}"
        self.assertEqual(call(self.owner, "PATCH", url, {"value": 101}).status_code, 400)  # still a percent
        self.assertEqual(call(self.owner, "PATCH", url, {}).status_code, 400)
        self.assertEqual(call(self.owner, "PATCH", url, {"is_active": "no"}).status_code, 400)
        self.assertEqual(call(self.owner, "PATCH", url, {"kind": "fixed", "value": 25000}).status_code, 200)
        self.assertEqual(call(self.owner, "PATCH", url, {"kind": "percent"}).status_code, 400)  # 25000 isn't a percent
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(list(DiscountType.objects.filter(clinic_id=self.a.id).values_list("kind", "value")),
                             [("fixed", 25000)])

    def test_names_are_unique_among_the_active_types(self):
        friends = self.kind()
        r = self.kind(expect=None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "duplicate"))
        other = self.kind("Other")
        r = call(self.owner, "PATCH", f"/api/v1/discount-types/{other['id']}", {"name": "Friends"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "duplicate"))
        self.stop(friends)
        again = self.kind()  # the name is free once its type is stopped
        self.assertNotEqual(again["id"], friends["id"])
        r = call(self.owner, "PATCH", f"/api/v1/discount-types/{friends['id']}", {"is_active": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "duplicate"))

    def test_at_most_twelve_active(self):
        made = [self.kind(f"Type {n}") for n in range(12)]
        r = self.kind("One too many", expect=None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "too_many"))
        self.stop(made[0])
        self.kind("Thirteenth")
        r = call(self.owner, "PATCH", f"/api/v1/discount-types/{made[0]['id']}", {"is_active": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "too_many"))  # using one again counts too
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(DiscountType.objects.filter(clinic_id=self.a.id, is_active=True).count(), 12)

    def test_a_repeated_request_creates_one_type(self):
        key = str(uuid.uuid4())
        body = {"name": "Friends", "kind": "percent", "value": 10}
        first = call(self.owner, "POST", "/api/v1/discount-types", body, key=key)
        again = call(self.owner, "POST", "/api/v1/discount-types", body, key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(first.json()["discount_type"]["id"], again.json()["discount_type"]["id"])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(DiscountType.objects.filter(clinic_id=self.a.id).count(), 1)

    def test_another_clinics_types_are_not_found(self):
        friends = self.kind()
        theirs = self.b.client("owner")
        self.assertEqual(call(theirs, "GET", "/api/v1/discount-types").json()["items"], [])
        self.assertEqual(call(theirs, "PATCH", f"/api/v1/discount-types/{friends['id']}", {"value": 99}).status_code, 404)
        b_visit = self.book(fixture=self.b).json()["appointment"]
        b_desk = self.b.client("assistant")
        b_invoice = call(b_desk, "POST", f"/api/v1/appointments/{b_visit['id']}/invoice").json()["invoice"]
        self.assertEqual(self.apply(b_invoice, friends, client=b_desk).status_code, 404)  # A's type on B's invoice
        self.assertEqual(self.apply(b_invoice, friends).status_code, 404)  # A's desk on B's invoice
        self.assertEqual(call(self.desk, "GET", f"/api/v1/appointments/{b_visit['id']}/discount-types").status_code, 404)
        b_room = self.in_room(fixture=self.b)
        r = call(self.b.client("doctor"), "PUT", f"/api/v1/appointments/{b_room['id']}/suggested-discount",
                 {"type_id": friends["id"]})
        self.assertEqual(r.status_code, 404)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(DiscountType.objects.get(clinic_id=self.a.id).value, 10)
        with clinic_transaction(self.b.id, self.b.shard):
            self.assertEqual(Invoice.objects.get(clinic_id=self.b.id, id=b_invoice["id"]).discount, 0)

    def test_the_owner_puts_the_types_in_order_and_a_stale_list_is_refused(self):
        friends, staff, elders = self.kind(), self.kind("Staff"), self.kind("Elders", "fixed", 5000)
        url = "/api/v1/discount-types/order"
        order = [elders["id"], friends["id"], staff["id"]]
        r = call(self.owner, "PUT", url, {"ids": order})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([t["id"] for t in r.json()["items"]], order)
        self.assertEqual(call(self.owner, "PUT", url, {"ids": order}).status_code, 200)  # a resend changes nothing
        self.assertEqual(len(self.changes("discount_type.reorder")), 1)
        names = [t["name"] for t in call(self.owner, "GET", "/api/v1/discount-types").json()["items"]]
        self.assertEqual(names, ["Elders", "Friends", "Staff"])
        self.assertEqual(call(self.desk, "PUT", url, {"ids": order}).status_code, 403)
        self.stop(staff)  # the screen still lists it: stale
        r = call(self.owner, "PUT", url, {"ids": order})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))
        for body in ({"ids": [friends["id"], friends["id"]]}, {"ids": "1,2"}, {}, {"ids": ["x"]}):
            self.assertIn(call(self.owner, "PUT", url, body).status_code, (400, 409), body)
        theirs = self.b.client("owner")
        self.assertEqual(call(theirs, "PUT", url, {"ids": [friends["id"]]}).status_code, 409)  # not B's

    def test_every_route_declares_its_code(self):
        found = {}
        for pattern in get_resolver().url_patterns:
            if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") != "billing.discount_urls":
                continue
            for route in pattern.url_patterns:
                views = getattr(route.callback, "api_views", None) or {route.callback.api_meta["method"]: route.callback}
                for method, view in views.items():
                    found[method, str(route.pattern)] = view.api_meta["perm"]
        self.assertEqual(found, {
            ("GET", "api/v1/discount-types"): ("settings.manage",),
            ("POST", "api/v1/discount-types"): ("settings.manage",),
            ("PATCH", "api/v1/discount-types/<int:type_id>"): ("settings.manage",),
            ("PUT", "api/v1/discount-types/order"): ("settings.manage",),
            ("GET", "api/v1/appointments/<int:appointment_id>/discount-types"): ("billing.collect",),
            ("PUT", "api/v1/invoices/<int:invoice_id>/discount-type"): ("billing.collect",),
            ("PUT", "api/v1/appointments/<int:appointment_id>/suggested-discount"): ("procedures.record",),
        })


class ApplyTests(DiscountTestCase):
    def test_the_desk_applies_a_percent_in_one_request_and_the_invoice_remembers_it(self):
        friends = self.kind(value=15)
        invoice = self.invoice()
        invoice = self.add_line(invoice, client=self.owner, kind="other", description="Odd", unit_price=333)
        subtotal = self.fee + 333
        expected = (subtotal * 15 + 50) // 100
        r = self.apply(invoice, friends)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()["invoice"]
        self.assertEqual((body["subtotal"], body["discount"], body["total"]), (subtotal, expected, subtotal - expected))
        self.assertEqual(body["discount_type"], {"id": friends["id"], "name": "Friends", "percent": 15})
        self.assertEqual(self.changes("invoice.discount"), [{
            "before": {"discount": 0, "type": None},
            "after": {"discount": expected, "type": friends["id"], "name": "Friends"}}])
        self.assertIn('الخصم (<bdi dir="auto">Friends</bdi>)', self.desk.get(f"/print/receipts/{invoice['id']}").content.decode())
        # A later change of the type leaves this invoice as it was.
        call(self.owner, "PATCH", f"/api/v1/discount-types/{friends['id']}", {"name": "Family", "value": 50})
        body = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]
        self.assertEqual((body["discount"], body["discount_type"]["name"]), (expected, "Friends"))

    def test_half_a_piastre_rounds_up(self):
        from billing.services import percent_of

        self.assertEqual([percent_of(s, 15) for s in (0, 3, 4, 10, 30, 333)], [0, 0, 1, 2, 5, 50])
        self.assertEqual(percent_of(12345, 100), 12345)

    def test_a_type_the_desk_may_not_apply_needs_the_discount_permission(self):
        staff = self.kind("Staff", "fixed", 5000, desk_can_apply=False)
        friends = self.kind()
        invoice = self.invoice()
        r = self.apply(invoice, staff)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"))
        r = self.apply(invoice, staff, client=self.owner)
        self.assertEqual((r.status_code, r.json()["invoice"]["discount"]), (200, 5000))
        invoice = r.json()["invoice"]
        # The desk can't replace or remove what it couldn't have given.
        self.assertEqual(self.apply(invoice, friends).status_code, 403)
        self.assertEqual(self.apply(invoice, None).status_code, 403)
        seen = call(self.desk, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]
        self.assertEqual((seen["discount"], seen["discount_changeable"]), (5000, False))
        self.assertTrue(call(self.owner, "GET", f"/api/v1/invoices/{invoice['id']}").json()["invoice"]["discount_changeable"])

    def test_the_desk_cant_replace_a_typed_discount(self):
        friends = self.kind()
        invoice = self.invoice()
        r = call(self.owner, "PUT", f"/api/v1/invoices/{invoice['id']}/discount", {"discount": 700, "expected_discount": 0})
        self.assertEqual(r.status_code, 200, r.content)
        r = self.apply(r.json()["invoice"], friends)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Invoice.objects.get(clinic_id=self.a.id, id=invoice["id"]).discount, 700)

    def test_applying_the_same_type_again_is_a_harmless_repeat(self):
        friends = self.kind()
        invoice = self.invoice()
        first = self.apply(invoice, friends).json()["invoice"]
        again = self.apply(invoice, friends)  # a second tap with what the screen first knew
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()["invoice"]["discount"], first["discount"])
        self.assertEqual(len(self.changes("invoice.discount")), 1)
        key = str(uuid.uuid4())  # and the same request sent twice answers the same
        half = self.kind("Half", value=50)
        body = {"type_id": half["id"], "expected_discount": first["discount"]}
        url = f"/api/v1/invoices/{invoice['id']}/discount-type"
        answers = [call(self.desk, "PUT", url, body, key=key) for _ in range(2)]
        self.assertEqual([a.status_code for a in answers], [200, 200])
        self.assertEqual(answers[0].json()["invoice"]["discount"], answers[1].json()["invoice"]["discount"])
        self.assertEqual(len(self.changes("invoice.discount")), 2)

    def test_a_stale_screen_is_told_the_current_discount(self):
        friends, half = self.kind(), self.kind("Half", value=50)
        invoice = self.invoice()
        self.apply(invoice, friends)
        r = self.apply(invoice, half)  # still expects no discount
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))
        self.assertEqual(r.json()["error"]["fields"]["discount"], self.fee // 10)
        r = self.apply(invoice, None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))

    def test_removing_the_type_and_typing_an_amount_both_clear_it(self):
        friends = self.kind()
        invoice = self.apply(self.invoice(), friends).json()["invoice"]
        r = self.apply(invoice, None)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()["invoice"]
        self.assertEqual((body["discount"], body["total"], body["discount_type"]), (0, self.fee, None))
        self.assertEqual(self.apply(body, None).status_code, 200)  # nothing to remove: a harmless repeat
        self.assertEqual(len(self.changes("invoice.discount")), 2)
        invoice = self.apply(body, friends).json()["invoice"]
        r = call(self.owner, "PUT", f"/api/v1/invoices/{invoice['id']}/discount",
                 {"discount": invoice["discount"], "expected_discount": invoice["discount"]})  # the same amount, typed
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["invoice"]["discount"], r.json()["invoice"]["discount_type"]), (invoice["discount"], None))
        self.assertEqual(self.changes("invoice.discount")[-1], {
            "before": {"discount": invoice["discount"], "type": friends["id"]}, "after": {"discount": invoice["discount"]}})
        self.assertNotIn("الخصم (", self.desk.get(f"/print/receipts/{invoice['id']}").content.decode())
        # The free amount still needs billing.discount.
        r = call(self.desk, "PUT", f"/api/v1/invoices/{invoice['id']}/discount", {"discount": 0, "expected_discount": 0})
        self.assertEqual(r.status_code, 403)

    def test_a_fixed_type_larger_than_the_invoice_and_one_below_what_was_paid_are_refused(self):
        huge = self.kind("Huge", "fixed", self.fee + 1)
        most = self.kind("Most", "fixed", self.fee - 100)
        invoice = self.invoice()
        r = self.apply(invoice, huge)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "discount_too_large"))
        self.pay(invoice, 200)
        r = self.apply(invoice, most)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "paid_exceeds"))

    def test_a_stopped_type_and_a_void_invoice_are_refused(self):
        friends = self.kind()
        invoice = self.invoice()
        self.stop(friends)
        r = self.apply(invoice, friends)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "discount_stopped"))
        other = self.kind("Other")
        self.assertEqual(call(self.owner, "POST", f"/api/v1/invoices/{invoice['id']}/void").status_code, 200)
        r = self.apply(invoice, other)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "invoice_void"))

    def test_bad_input_is_a_400_and_unknown_ids_are_404(self):
        friends = self.kind()
        invoice = self.invoice()
        url = f"/api/v1/invoices/{invoice['id']}/discount-type"
        for body in ({"type_id": "x", "expected_discount": 0}, {"type_id": friends["id"]},
                     {"type_id": friends["id"], "expected_discount": -1}, {"expected_discount": 0}):
            self.assertEqual(call(self.desk, "PUT", url, body).status_code, 400, body)
        self.assertEqual(call(self.desk, "PUT", url, {"type_id": 2**62, "expected_discount": 0}).status_code, 404)
        self.assertEqual(call(self.desk, "PUT", "/api/v1/invoices/999999999/discount-type",
                              {"type_id": friends["id"], "expected_discount": 0}).status_code, 404)

    def test_a_doctor_never_applies_one_on_another_doctors_visit(self):
        friends = self.kind()
        invoice = self.invoice()  # the owner's visit
        self.assertEqual(self.apply(invoice, friends, client=self.doc).status_code, 404)
        self.assertEqual(call(self.doc, "GET", f"/api/v1/appointments/{invoice['appointment']['id']}/discount-types").status_code, 404)
        mine = self.in_room()
        r = call(self.doc, "POST", f"/api/v1/appointments/{mine['id']}/invoice")
        self.assertEqual(self.apply(r.json()["invoice"], friends, client=self.doc).status_code, 200)

    def test_the_requests_run_a_fixed_number_of_statements(self):
        friends = self.kind()
        invoice = self.invoice()
        few = self.queries(lambda: call(self.desk, "GET", f"/api/v1/appointments/{invoice['appointment']['id']}/discount-types"))
        for n in range(8):
            invoice = self.add_line(invoice)
            self.kind(f"More {n}")
        many = self.queries(lambda: call(self.desk, "GET", f"/api/v1/appointments/{invoice['appointment']['id']}/discount-types"))
        self.assertEqual((few, many), (4, 4))
        self.assertLessEqual(self.queries(lambda: self.apply(invoice, friends)), 14)


class PercentStaysTrueTests(DiscountTestCase):
    def test_a_percent_follows_lines_added_and_removed_at_the_desk(self):
        half = self.kind("Half", value=50)
        invoice = self.apply(self.invoice(), half).json()["invoice"]
        self.assertEqual(invoice["discount"], self.fee // 2)
        price = self.services_a["procedure"].price
        invoice = self.add_line(invoice)
        self.assertEqual((invoice["subtotal"], invoice["discount"], invoice["total"]),
                         (self.fee + price, (self.fee + price) // 2, (self.fee + price) - (self.fee + price) // 2))
        r = self.remove_line(invoice, invoice["lines"][-1])
        self.assertEqual(r.status_code, 200, r.content)
        invoice = r.json()["invoice"]
        self.assertEqual((invoice["subtotal"], invoice["discount"], invoice["total"]),
                         (self.fee, self.fee // 2, self.fee - self.fee // 2))
        self.assertEqual(self.changes("invoice.line_remove")[0]["after"], {"discount": self.fee // 2})

    def test_a_percent_follows_a_procedure_the_doctor_records(self):
        friends = self.kind(value=10)
        visit = self.in_room()
        invoice = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/invoice").json()["invoice"]
        invoice = self.apply(invoice, friends).json()["invoice"]
        proc = self.procedure("Composite filling, one surface")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": proc["id"], "tooth": 36})
        self.assertEqual(r.status_code, 201, r.content)
        after = r.json()["invoice"]
        subtotal = invoice["subtotal"] + proc["price"]
        self.assertEqual((after["subtotal"], after["discount"], after["total"]),
                         (subtotal, (subtotal * 10 + 50) // 100, subtotal - (subtotal * 10 + 50) // 100))

    def test_a_removal_that_would_leave_more_paid_than_the_total_is_refused(self):
        half = self.kind("Half", value=50)
        invoice = self.add_line(self.invoice())
        invoice = self.apply(invoice, half).json()["invoice"]
        self.pay(invoice, invoice["total"])
        r = self.remove_line(invoice, invoice["lines"][-1])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "paid_exceeds"))

    def test_a_fixed_type_keeps_its_amount_and_the_lower_it_first_rule(self):
        price = self.services_a["procedure"].price
        fixed = self.kind("Fixed", "fixed", self.fee + 100)
        invoice = self.add_line(self.invoice())
        invoice = self.apply(invoice, fixed).json()["invoice"]
        self.assertEqual((invoice["discount"], invoice["total"]), (self.fee + 100, price - 100))
        invoice = self.add_line(invoice)
        self.assertEqual((invoice["discount"], invoice["discount_type"]["percent"]), (self.fee + 100, None))
        self.assertEqual(self.remove_line(invoice, invoice["lines"][1]).status_code, 200)
        r = self.remove_line(invoice, invoice["lines"][2])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "discount_exceeds"))

    def test_the_table_refuses_a_percent_discount_that_was_not_computed_again(self):
        half = self.kind("Half", value=50)
        invoice = self.apply(self.invoice(), half).json()["invoice"]
        with clinic_transaction(self.a.id, self.a.shard):
            with self.assertRaises(IntegrityError), transaction.atomic(using=self.a.shard):
                Invoice.objects.filter(clinic_id=self.a.id, id=invoice["id"]).update(
                    subtotal=self.fee + 100, total=self.fee + 100 - invoice["discount"])


class SuggestTests(DiscountTestCase):
    def suggest(self, visit, kind, client=None):
        return call(client or self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount",
                    {"type_id": kind["id"] if kind else None})

    def test_the_doctor_suggests_and_clears_and_no_money_changes(self):
        friends = self.kind()
        self.kind("Staff", "fixed", 5000, desk_can_apply=False)
        visit = self.in_room()
        with clinic_transaction(self.a.id, self.a.shard):
            before = list(Invoice.objects.filter(clinic_id=self.a.id).values("id", "subtotal", "discount", "total", "discount_type_id"))
        r = self.suggest(visit, friends)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["suggested"], {"id": friends["id"], "name": "Friends"})
        self.assertEqual(self.appointment(visit["id"]).suggested_discount_type_id, friends["id"])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(list(Invoice.objects.filter(clinic_id=self.a.id).values("id", "subtotal", "discount", "total", "discount_type_id")), before)
        seen = call(self.desk, "GET", f"/api/v1/appointments/{visit['id']}/discount-types").json()
        self.assertEqual(seen["suggested"], {"id": friends["id"], "name": "Friends"})
        self.assertEqual([(t["name"], t["allowed"]) for t in seen["items"]], [("Friends", True), ("Staff", False)])
        self.assertEqual(self.suggest(visit, friends).status_code, 200)  # the same again
        r = self.suggest(visit, None)
        self.assertEqual((r.status_code, r.json()["suggested"]), (200, None))
        self.assertIsNone(self.appointment(visit["id"]).suggested_discount_type_id)

    def test_only_who_may_record_procedures_on_that_visit(self):
        friends = self.kind()
        visit = self.in_room()
        self.assertEqual(self.suggest(visit, friends, client=self.desk).status_code, 403)  # no procedures.record
        owners = self.book(arrive=True).json()["appointment"]  # the owner's visit: another doctor's patient
        self.assertEqual(self.suggest(owners, friends).status_code, 404)
        self.assertEqual(self.suggest(visit, friends, client=self.b.client("doctor")).status_code, 404)
        self.assertEqual(self.suggest(visit, {"id": 2**62}).status_code, 404)
        r = call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {"type_id": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {}).status_code, 400)
        self.assertIsNone(self.appointment(visit["id"]).suggested_discount_type_id)
        self.assertIsNone(self.appointment(owners["id"]).suggested_discount_type_id)

    def test_not_on_a_visit_that_has_not_arrived_and_not_a_stopped_type(self):
        friends = self.kind()
        booked = self.booked()
        r = self.suggest(booked, friends)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))
        visit = self.in_room()
        self.stop(friends)
        r = self.suggest(visit, friends)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "discount_stopped"))

    def test_a_stopped_suggestion_is_no_longer_shown(self):
        friends = self.kind()
        visit = self.in_room()
        self.suggest(visit, friends)
        self.stop(friends)
        seen = call(self.desk, "GET", f"/api/v1/appointments/{visit['id']}/discount-types").json()
        self.assertEqual((seen["suggested"], seen["items"]), (None, []))


class ScreenTests(DiscountTestCase):
    def test_the_visit_screen_offers_the_types_and_marks_the_suggested_one(self):
        friends = self.kind()
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-suggest-discount", html)
        self.assertIn(f'data-type="{friends["id"]}" aria-pressed="false"', html)
        call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {"type_id": friends["id"]})
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn(f'data-type="{friends["id"]}" aria-pressed="true"', html)
        self.assertIn("اقترح خصم", html)

    def test_no_types_no_row_on_the_visit_screen(self):
        visit = self.in_room()
        self.assertNotIn("data-suggest-discount", self.doc.get(f"/visits/{visit['id']}").content.decode())

    def test_the_pay_dialog_has_the_chips_for_the_desk_and_the_amount_only_with_the_permission(self):
        patient = self.book().json()["appointment"]["patient"]["id"]
        html = self.desk.get(f"/patients/{patient}").content.decode()
        self.assertIn("data-discount-types", html)
        self.assertNotIn('name="discount"', html)
        self.assertIn('name="discount"', self.owner.get(f"/patients/{patient}").content.decode())

    def test_the_settings_card_is_for_settings_manage(self):
        html = self.owner.get("/team").content.decode()
        self.assertIn("data-discount-card", html)
        self.assertIn("أنواع الخصم", html)
        self.assertIn("js/discounts.js", html)


class DiscountConcurrencyTests(TransactionTestCase):
    """On real PostgreSQL: two taps at once take effect once, and the cap of 12 holds under a race."""

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("dc", "shard_1")
        add_schedule(self.a, "owner")
        self.services = services_of(self.a)

    def race(self, calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def worker(index, role, method, url, body):
            try:
                client = self.a.client(role)
                barrier.wait()
                r = call(client, method, url, body)
                results[index] = (r.status_code, r.json().get("error", {}).get("code"))
            except Exception as error:
                results[index] = repr(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(index, *args)) for index, args in enumerate(calls)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def new_invoice(self):
        desk = self.a.client("assistant")
        visit = call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.a.membership("owner").id, "service_id": self.services["consultation"].id,
            "new_patient": {"full_name": "Race Patient"}}).json()["appointment"]
        return call(desk, "POST", f"/api/v1/appointments/{visit['id']}/invoice").json()["invoice"]

    def new_type(self, name, value):
        r = call(self.a.client("owner"), "POST", "/api/v1/discount-types", {"name": name, "kind": "percent", "value": value})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["discount_type"]

    def test_two_different_types_at_once_one_wins_and_the_other_is_stale(self):
        types = [self.new_type("Ten", 10), self.new_type("Half", 50)]
        invoice = self.new_invoice()
        url = f"/api/v1/invoices/{invoice['id']}/discount-type"
        results = self.race([("assistant", "PUT", url, {"type_id": t["id"], "expected_discount": 0}) for t in types])
        self.assertEqual(sorted(results, key=str), [(200, None), (409, "stale")])
        with clinic_transaction(self.a.id, self.a.shard):
            row = Invoice.objects.get(clinic_id=self.a.id, id=invoice["id"])
            self.assertIn((row.discount_percent, row.discount), [(10, row.subtotal // 10), (50, row.subtotal // 2)])
            self.assertEqual(row.total, row.subtotal - row.discount)
            self.assertEqual(AuditChange.objects.filter(clinic_id=self.a.id, action="invoice.discount").count(), 1)

    def test_a_percent_and_a_new_line_at_once_agree(self):
        half = self.new_type("Half", 50)
        invoice = self.new_invoice()
        results = self.race([
            ("assistant", "PUT", f"/api/v1/invoices/{invoice['id']}/discount-type", {"type_id": half["id"], "expected_discount": 0}),
            ("assistant", "POST", f"/api/v1/invoices/{invoice['id']}/lines",
             {"kind": "procedure", "service_id": self.services["procedure"].id}),
        ])
        self.assertEqual(results, [(200, None), (201, None)])
        with clinic_transaction(self.a.id, self.a.shard):
            row = Invoice.objects.get(clinic_id=self.a.id, id=invoice["id"])
        self.assertEqual(row.subtotal, self.services["consultation"].price + self.services["procedure"].price)
        self.assertEqual((row.discount, row.total), ((row.subtotal + 1) // 2, row.subtotal - (row.subtotal + 1) // 2))

    def test_the_cap_of_twelve_holds_when_two_are_added_at_once(self):
        for n in range(11):
            self.new_type(f"T{n}", 5)
        results = self.race([("owner", "POST", "/api/v1/discount-types", {"name": name, "kind": "percent", "value": 5})
                             for name in ("Twelfth", "Thirteenth")])
        self.assertEqual(sorted(results, key=str), [(201, None), (409, "too_many")])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(DiscountType.objects.filter(clinic_id=self.a.id, is_active=True).count(), 12)
