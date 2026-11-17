"""A doctor and money (Saged, 2026-11-16).

A doctor employed by the place has nothing to do with money by default. The owner opens two switches per doctor on
the Team page: "Sees prices and money" (money.view, with billing.collect, cash.collect and cash.shift) and "Suggests
discounts" (discounts.suggest). Without money.view no amount reaches the person: not in a page, not in a payload.
The owner (solo, treating or managing) and the assistant are unchanged. Hidden by permission, never by role.
"""

import importlib
import re

from django.test import SimpleTestCase

from clinical.testing import M2TestCase
from clinics import permissions as perms
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call, new_patient

MONEY_MARKS = ('class="money"', "data-due=", "data-fee=", "data-paid=", 'data-act="pay"', "data-procs-total",
               "bill-total", 'id="pay-dialog"', "data-deposit-box")


def assert_no_money(test, html, where):
    for mark in MONEY_MARKS:
        test.assertNotIn(mark, html, f"{where}: {mark}")


class CodeTests(SimpleTestCase):
    def test_the_doctor_template_has_no_money_and_no_suggestion(self):
        doctor = perms.TEMPLATES["doctor"]
        for code in ("money.view", "billing.collect", "cash.collect", "cash.shift", "discounts.suggest"):
            self.assertNotIn(code, doctor, code)

    def test_the_assistant_sees_money_and_collects_but_does_not_suggest(self):
        assistant = perms.TEMPLATES["assistant"]
        for code in ("money.view", "billing.collect", "cash.collect", "cash.shift"):
            self.assertIn(code, assistant, code)
        self.assertNotIn("discounts.suggest", assistant)

    def test_the_owner_has_both(self):
        self.assertIn("money.view", perms.effective_codes("owner"))
        self.assertIn("discounts.suggest", perms.effective_codes("owner"))

    def test_no_money_power_without_money_view(self):
        """A stored grant of a money code without money.view gives nothing: the one rule every screen relies on."""
        codes = perms.effective_codes("doctor", granted=["finances.view", "billing.collect", "cash.shift"])
        self.assertFalse(codes & perms.NEEDS_MONEY)
        codes = perms.effective_codes("assistant", revoked=["money.view"])
        self.assertFalse(codes & perms.NEEDS_MONEY)
        self.assertIn("finances.view", perms.effective_codes("doctor", granted=["money.view", "finances.view"]))

    def test_the_money_switch_opens_seeing_collecting_and_the_register(self):
        doctor = perms.effective_codes("doctor")
        wanted = perms.wanted_codes(doctor, {"prices": True})
        self.assertTrue({"money.view", "billing.collect", "cash.collect", "cash.shift"} <= wanted)
        self.assertNotIn("finances.view", wanted)
        self.assertNotIn("discounts.suggest", wanted)
        self.assertEqual(perms.wanted_codes(wanted, {"prices": False}), doctor)

    def test_a_money_code_brings_money_view_and_closing_money_view_takes_them_all(self):
        doctor = perms.effective_codes("doctor")
        self.assertIn("money.view", perms.wanted_codes(doctor, codes={"finances.view": True}))
        assistant = perms.effective_codes("assistant")
        closed = perms.wanted_codes(assistant, codes={"money.view": False})
        self.assertFalse(closed & perms.NEEDS_MONEY)

    def test_the_suggestion_switch_is_one_code(self):
        wanted = perms.wanted_codes(perms.effective_codes("doctor"), {"suggest": True})
        self.assertEqual(wanted - perms.effective_codes("doctor"), {"discounts.suggest"})

    def test_existing_doctors_keep_money_they_were_explicitly_given(self):
        migration = importlib.import_module("clinics.migrations.0005_money_view")
        self.assertEqual(migration.lists("doctor", [], []), None)  # nothing to change: the three codes go
        self.assertEqual(migration.lists("doctor", ["finances.view"], []), ["finances.view", "money.view"])
        self.assertEqual(migration.lists("doctor", ["money.view", "billing.refund"], []), None)
        self.assertEqual(migration.lists("assistant", ["finances.view"], []), None)  # the template has money.view
        self.assertEqual(migration.lists("doctor", ["patients.export"], []), None)


class MoneyTestCase(M2TestCase):
    def switch(self, groups=None, codes=None, role="doctor", client=None, fixture=None):
        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            member = Membership.objects.get(clinic_id=fixture.id, id=fixture.membership(role).id)
        return call(client or fixture.client("owner"), "PUT", f"/api/v1/team/members/{member.id}/permissions",
                    {"version": member.version, "groups": groups or {}, "codes": codes or {}})

    def waiting(self, doctor="doctor"):
        """A visit of A's doctor that has arrived and waits (Today shows it; the doctor is not sent to a visit)."""
        body = {"service_id": self.services_a["procedure"].id, "doctor_id": self.a.membership(doctor).id,
                "new_patient": new_patient(full_name="Mona Ali", phone="01099999999", sex="f", age=40)}
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["appointment"]


class DoctorWithoutMoneyTests(MoneyTestCase):
    def test_today_page_fragment_and_json_carry_no_amount(self):
        visit = self.waiting()
        html = self.doc.get("/").content.decode()
        self.assertIn(f'data-id="{visit["id"]}"', html)
        assert_no_money(self, html, "today")
        r = self.doc.get("/today/queue", headers={"X-Membership": str(self.doc.membership_id)})
        assert_no_money(self, r.content.decode(), "queue")
        day = call(self.doc, "GET", "/api/v1/today").json()
        rows = [a for q in day["queues"] for a in q["appointments"]]
        self.assertTrue(rows)
        for a in rows:
            self.assertIsNone(a["fee"])
            self.assertIsNone(a["due"])
            self.assertTrue(a["invoice"] is None or set(a["invoice"]) == {"id", "number"})
        self.assertNotIn("totals", day)

    def test_appointment_answers_and_the_quote_carry_no_amount(self):
        visit = self.waiting()
        body = call(self.doc, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]
        self.assertEqual((body["fee"], body["due"]), (None, None))
        r = call(self.doc, "GET", f"/api/v1/appointments/quote?doctor={self.a.membership('doctor').id}"
                                  f"&service={self.services_a['consultation'].id}")
        self.assertEqual(r.status_code, 200, r.content)
        q = r.json()
        self.assertEqual((q["fee"], q["service_price"], q["follow_up_price"]), (None, None, None))
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/call", {"status": "waiting"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["appointment"]["fee"])
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/finish", {"status": "in_room"})
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["appointment"]["due"], body["invoice"], body["balance"]), (None, None, None))

    def test_the_visit_screen_shows_procedures_without_prices(self):
        visit = self.in_room()
        filling = self.procedure("Composite filling, one surface")
        self.assertTrue(all(p["price"] is None and p["internal_cost"] is None for p in self.catalogue(self.doc)))
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                 {"procedure_id": filling["id"], "tooth": 36})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.json()["invoice"])
        self.assertIsNone(r.json()["procedure"]["amount"])
        self.assertIsNone(r.json()["procedure"]["unit_price"])
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        assert_no_money(self, html, "visit")
        self.assertIn("data-procedures", html)
        record = call(self.doc, "GET", f"/api/v1/appointments/{visit['id']}/record").json()
        self.assertEqual((record["invoice"], record["balance"]), (None, None))
        self.assertTrue(all(p["amount"] is None and p["unit_price"] is None for p in record["procedures"]))
        self.assertNotIn('"price": 1', html)
        found = re.search(r'<script id="visit-data" type="application/json">(.*?)</script>', html, re.S)
        if found:
            self.assertNotRegex(found.group(1), r'"price": \d')
        chart = self.doc.get(f"/visits/{visit['id']}/chart", headers={"X-Membership": str(self.doc.membership_id)})
        assert_no_money(self, chart.content.decode(), "chart")

    def test_the_patient_file_and_history_carry_no_amount(self):
        visit = self.waiting()
        self.act(visit["id"], "call", "waiting")
        self.act(visit["id"], "finish", "in_room", payment={"amount": 1000, "method": "cash"})
        patient = visit["patient"]["id"]
        html = self.doc.get(f"/patients/{patient}").content.decode()
        assert_no_money(self, html, "patient")
        self.assertNotIn('data-rows="payments"', html)
        more = self.doc.get(f"/patients/{patient}/visits", headers={"X-Membership": str(self.doc.membership_id)})
        assert_no_money(self, more.content.decode(), "visits more")
        for url in (f"/api/v1/patients/{patient}/payments", f"/api/v1/patients/{patient}/balance"):
            self.assertEqual(call(self.doc, "GET", url).status_code, 403, url)
        self.assertEqual(self.doc.get(f"/patients/{patient}/payments",
                                      headers={"X-Membership": str(self.doc.membership_id)}).status_code, 403)
        items = call(self.doc, "GET", f"/api/v1/patients/{patient}/visits").json()["items"]
        self.assertTrue(items and all(v["fee"] is None and v["due"] is None for v in items))
        timeline = call(self.doc, "GET", f"/api/v1/patients/{patient}/timeline").json()["items"]
        self.assertFalse([e for e in timeline if e["type"] == "payment"])
        self.assertTrue(all(e.get("amount") is None and e.get("fee") is None for e in timeline))
        found = call(self.doc, "GET", "/api/v1/patients?q=Mona").json()["items"]
        self.assertTrue(found and all("owed" not in p or p["owed"] is None for p in found))
        search = self.doc.get("/patients/search?q=Mona", headers={"X-Membership": str(self.doc.membership_id)})
        assert_no_money(self, search.content.decode(), "search")

    def test_receipts_and_every_money_endpoint_are_refused(self):
        visit = self.waiting()
        self.act(visit["id"], "call", "waiting")
        r = self.act(visit["id"], "finish", "in_room", payment={"amount": 1000, "method": "cash"})
        invoice = r.json()["invoice"]["id"]
        self.assertEqual(self.doc.get(f"/print/receipts/{invoice}").status_code, 403)
        refused = [("POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 100, "method": "cash"}),
                   ("GET", f"/api/v1/invoices/{invoice}", None),
                   ("POST", f"/api/v1/appointments/{visit['id']}/invoice", None),
                   ("GET", f"/api/v1/appointments/{visit['id']}/discount-types", None)]
        for method, url, body in refused:
            self.assertEqual(call(self.doc, method, url, body).status_code, 403, url)
        self.assertEqual(self.doc.get("/finances").status_code, 403)
        self.assertEqual(call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/finish",
                              {"status": "done", "payment": {"amount": 100, "method": "cash"}}).status_code, 403)
        html = self.doc.get("/").content.decode()
        self.assertNotIn('href="/finances"', html)

    def test_the_clinic_services_carry_no_price(self):
        services = call(self.doc, "GET", "/api/v1/clinic").json()["services"]
        self.assertTrue(services and all(s["price"] is None for s in services))
        html = self.doc.get("/patients").content.decode()
        found = re.search(r'<script id="clinic-config" type="application/json">(.*?)</script>', html, re.S)
        if found:
            self.assertNotRegex(found.group(1), r'"price": \d')
            self.assertIn('"deposit": 0', found.group(1))

    def test_the_inventory_shows_no_cost(self):
        r = call(self.doc, "GET", "/api/v1/inventory/items")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(all(i.get("unit_cost") is None for i in r.json()["items"]))
        html = self.doc.get("/inventory").content.decode()
        self.assertNotRegex(html, r'"unit_cost": \d')

    def test_plans_and_follow_ups_carry_no_price(self):
        visit = self.in_room()
        filling = self.procedure("Composite filling, one surface")
        r = call(self.doc, "POST", f"/api/v1/patients/{visit['patient']['id']}/plans",
                 {"items": [{"procedure_id": filling["id"], "tooth": 36}]})
        self.assertEqual(r.status_code, 201, r.content)
        plans = call(self.doc, "GET", f"/api/v1/patients/{visit['patient']['id']}/plans").json()["items"]
        self.assertTrue(plans)
        for plan in plans:
            self.assertIsNone(plan["estimated_total"])
            self.assertTrue(all(i["price"] is None for i in plan["items"]))
        html = self.doc.get(f"/patients/{visit['patient']['id']}/teeth").content.decode()
        assert_no_money(self, html, "teeth")
        self.assertNotRegex(html, r'"price": \d')


class DoctorWithMoneyTests(MoneyTestCase):
    def test_the_switch_opens_seeing_and_collecting(self):
        r = self.switch({"prices": True})
        self.assertEqual(r.status_code, 200, r.content)
        codes = set(r.json()["member"]["codes"])
        self.assertTrue({"money.view", "billing.collect", "cash.collect", "cash.shift"} <= codes)
        self.assertNotIn("finances.view", codes)
        doc = self.a.client("doctor")
        visit = self.waiting()
        html = doc.get("/").content.decode()
        self.assertIn("data-due=", html)
        self.assertIn('id="pay-dialog"', html)
        body = call(doc, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]
        self.assertEqual(body["fee"], self.services_a["procedure"].price)
        r = call(doc, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 100, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["invoice"]["paid"], 100)
        self.assertEqual(self.doc.get("/finances").status_code, 200)  # the register

    def test_closing_the_switch_takes_it_all_back(self):
        self.switch({"prices": True})
        r = self.switch({"prices": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(set(r.json()["member"]["codes"]) & (perms.NEEDS_MONEY | {"money.view"}))
        visit = self.waiting()
        self.assertIsNone(call(self.doc, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]["fee"])

    def test_the_visit_screen_shows_the_bill_with_the_switch(self):
        self.switch({"prices": True})
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-procs-total", html)
        self.assertIn('class="money"', html)


class OwnerAndAssistantTests(MoneyTestCase):
    def test_the_assistant_is_unchanged(self):
        visit = self.waiting()
        html = self.desk.get("/").content.decode()
        for mark in ("data-due=", 'id="pay-dialog"', 'data-act="pay"'):
            self.assertIn(mark, html, mark)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/appointments/{visit['id']}").json()["appointment"]["fee"],
                         self.services_a["procedure"].price)
        r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 100, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)

    def test_a_treating_owner_sees_the_bill_and_the_types_with_their_values(self):
        friends = call(self.owner, "POST", "/api/v1/discount-types",
                       {"name": "Friends", "kind": "percent", "value": 10}).json()["discount_type"]
        visit = self.book(arrive=True).json()["appointment"]  # the owner's own queue
        self.assertEqual(self.act(visit["id"], "call", "waiting").status_code, 200)
        html = self.owner.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-procs-total", html)
        self.assertIn(f'data-type="{friends["id"]}"', html)
        self.assertIn("10%", html)

    def test_a_solo_doctor_owner_sees_everything(self):
        solo = self.clinic("solo", "shard_1")
        from scheduling.testing import add_schedule, services_of

        add_schedule(solo, "owner")
        with clinic_transaction(solo.id, solo.shard):  # alone: the doctor and the assistant leave
            Membership.objects.filter(clinic_id=solo.id).exclude(role="owner").update(is_active=False)
        owner = solo.client("owner")
        body = {"service_id": services_of(solo)["procedure"].id, "doctor_id": solo.membership("owner").id,
                "new_patient": new_patient(full_name="Solo Patient", phone="01011111111", sex="m", age=20)}
        r = call(owner, "POST", "/api/v1/appointments/walk-in", body)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["appointment"]["fee"], services_of(solo)["procedure"].price)
        html = owner.get("/").content.decode()
        self.assertIn('id="pay-dialog"', html)
        self.assertIn("data-due=", html)


class IsolationTests(MoneyTestCase):
    def test_the_switch_is_per_place_and_another_place_is_not_found(self):
        self.switch({"prices": True})
        other = self.b.client("doctor")
        self.assertIsNone(call(other, "GET", "/api/v1/clinic").json()["services"][0]["price"])
        visit = self.waiting()
        self.assertEqual(call(other, "GET", f"/api/v1/appointments/{visit['id']}").status_code, 404)
        r = call(other, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 100, "method": "cash"})
        self.assertEqual(r.status_code, 403)  # B's doctor has no switch in B
        b_visit = call(self.b.client("assistant"), "POST", "/api/v1/appointments/walk-in",
                       {"service_id": self.services_b["procedure"].id, "doctor_id": self.b.membership("doctor").id,
                        "new_patient": new_patient(full_name="Other Place", phone="01022222222", sex="m", age=30)})
        self.assertEqual(b_visit.status_code, 201, b_visit.content)
        b_id = b_visit.json()["appointment"]["id"]
        doc = self.a.client("doctor")  # A's doctor, with the switch in A: B's visit is not found
        r = call(doc, "POST", f"/api/v1/appointments/{b_id}/payments", {"amount": 100, "method": "cash"})
        self.assertEqual(r.status_code, 404, r.content)
        self.assertEqual(call(doc, "GET", f"/api/v1/appointments/{b_id}").status_code, 404)
        with clinic_transaction(self.b.id, self.b.shard):
            member = Membership.objects.get(clinic_id=self.b.id, id=self.b.membership("doctor").id)
        r = call(self.a.client("owner"), "PUT", f"/api/v1/team/members/{member.id}/permissions",
                 {"version": member.version, "groups": {"prices": True}})
        self.assertEqual(r.status_code, 404)


class SuggestSwitchTests(MoneyTestCase):
    def kind(self):
        return call(self.owner, "POST", "/api/v1/discount-types",
                    {"name": "Friends", "kind": "percent", "value": 15}).json()["discount_type"]

    def test_without_the_switch_no_row_and_the_endpoint_refuses(self):
        friends = self.kind()
        visit = self.in_room()
        self.assertNotIn("data-suggest-discount", self.doc.get(f"/visits/{visit['id']}").content.decode())
        r = call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {"type_id": friends["id"]})
        self.assertEqual(r.status_code, 403)

    def test_with_the_switch_names_only(self):
        friends = self.kind()
        self.assertEqual(self.switch({"suggest": True}).status_code, 200)
        visit = self.in_room()
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("data-suggest-discount", html)
        self.assertIn(f'data-type="{friends["id"]}"', html)
        self.assertIn("Friends", html)
        self.assertNotIn("15%", html)
        self.assertNotIn("١٥٪", html)
        r = call(self.doc, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {"type_id": friends["id"]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["suggested"], {"id": friends["id"], "name": "Friends"})

    def test_with_both_switches_the_values_show(self):
        self.kind()
        self.switch({"suggest": True, "prices": True})
        visit = self.in_room()
        self.assertIn("15%", self.doc.get(f"/visits/{visit['id']}").content.decode())

    def test_the_desk_does_not_suggest_by_default(self):
        friends = self.kind()
        visit = self.in_room()
        r = call(self.desk, "PUT", f"/api/v1/appointments/{visit['id']}/suggested-discount", {"type_id": friends["id"]})
        self.assertEqual(r.status_code, 403)


class ListPriceTests(MoneyTestCase):
    """No one edits prices, the owner included (Saged, 2026-11-16): bill lines take the list price; a lower bill is
    a discount."""

    def invoice(self):
        visit = self.book().json()["appointment"]
        r = call(self.owner, "POST", f"/api/v1/appointments/{visit['id']}/invoice")
        self.assertIn(r.status_code, (200, 201), r.content)
        return r.json()["invoice"]

    def test_the_label_says_discounts_only(self):
        from django.utils import translation

        with translation.override("en"):
            self.assertEqual(str(perms.CODES["billing.discount"]), "Give discounts")

    def test_an_invoice_line_takes_the_list_price_and_refuses_any_other(self):
        invoice = self.invoice()
        service = self.services_a["procedure"]
        url = f"/api/v1/invoices/{invoice['id']}/lines"
        for body in ({"kind": "service", "service_id": service.id, "unit_price": service.price + 100},
                     {"kind": "service", "service_id": service.id, "unit_price": 1},
                     {"kind": "other", "description": "Odd", "unit_price": 333}):
            r = call(self.owner, "POST", url, body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"), body)
        r = call(self.owner, "POST", url, {"kind": "service", "service_id": service.id, "qty": 2})
        self.assertEqual(r.status_code, 201, r.content)
        line = r.json()["invoice"]["lines"][-1]
        self.assertEqual((line["unit_price"], line["amount"]), (service.price, 2 * service.price))
        r = call(self.owner, "POST", url, {"kind": "service", "service_id": service.id, "unit_price": service.price})
        self.assertEqual(r.status_code, 201, r.content)  # the list price, said out loud, is the list price

    def test_a_procedure_and_a_plan_item_take_the_list_price_for_everyone(self):
        self.switch({"prices": True})
        visit = self.in_room()
        filling = self.procedure("Composite filling, one surface", client=self.owner)
        url = f"/api/v1/appointments/{visit['id']}/procedures"
        for client in (self.owner, self.doc):
            r = call(client, "POST", url, {"procedure_id": filling["id"], "unit_price": filling["price"] + 500})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"))
        r = call(self.doc, "POST", url, {"procedure_id": filling["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["procedure"]["amount"], filling["price"])
        plans = f"/api/v1/patients/{visit['patient']['id']}/plans"
        r = call(self.owner, "POST", plans, {"items": [{"procedure_id": filling["id"], "tooth": 36,
                                                         "price": filling["price"] - 100}]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"))
        r = call(self.owner, "POST", plans, {"items": [{"procedure_id": filling["id"], "tooth": 36}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["plan"]["items"][0]["price"], filling["price"])
