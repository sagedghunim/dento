"""Removing a procedure recorded by mistake (Saged, 2026-11-17): DELETE appointments/{id}/procedures/{record_id}.

The undo is whole and exact: the invoice line goes (with the bill's rules: a percent discount computed again, refused
when more was paid than the new total and the deposit held, or a typed discount would exceed what is left; a used
package session comes back), the materials go back to stock at the cost they left at (a `return` movement; history is
kept), a plan item returns to the status it had, the tooth returns to its state before (only while this record's
history row is still the tooth's latest), and the doctor-day's procedure count drops. Only while the visit is on.
"""

import uuid

from audit.models import AuditChange
from billing.models import InvoiceLine
from billing.test_deposit import DEPOSIT, DepositTestCase
from clinical.models import PlanItem, Tooth, ToothHistory, VisitProcedure
from core.routing import clinic_transaction
from core.testing import call
from inventory.models import Item, Movement
from scheduling.testing import today
from summaries.models import DoctorDay

from .testing import M2TestCase


class RemoveProcedureCase(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.patient = self.visit["patient"]["id"]
        self.url = f"/api/v1/appointments/{self.visit['id']}/procedures"
        self.filling = self.procedure("Composite filling, two surfaces")
        self.exam = self.procedure("Full examination")
        self.fee = self.services_a["procedure"].price

    def record(self, procedure, client=None, visit=None, **extra):
        url = f"/api/v1/appointments/{(visit or self.visit)['id']}/procedures"
        r = call(client or self.doc, "POST", url, {"procedure_id": procedure["id"], **extra})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["procedure"]

    def remove(self, record_id, client=None, visit_id=None, key=None):
        url = f"/api/v1/appointments/{visit_id or self.visit['id']}/procedures/{record_id}"
        return call(client or self.doc, "DELETE", url, key=key)

    def rows(self, model, **where):
        with clinic_transaction(self.a.id, self.a.shard):
            return list(model.objects.filter(clinic_id=self.a.id, **where).order_by("id").values())

    def procedures_today(self):
        doctor = self.a.membership("doctor").id
        with clinic_transaction(self.a.id, self.a.shard):
            return DoctorDay.objects.filter(clinic_id=self.a.id, day=today(self.a), doctor_id=doctor).values_list(
                "procedures", flat=True).first()

    def bill(self, client=None):
        visit = call(client or self.owner, "GET", f"/api/v1/appointments/{self.visit['id']}").json()["appointment"]
        return call(client or self.owner, "GET", f"/api/v1/invoices/{visit['invoice']['id']}").json()["invoice"]


class RemoveProcedureTests(RemoveProcedureCase):
    def test_the_line_the_record_the_count_and_the_tooth_go_with_an_audit(self):
        self.sees_money()
        record = self.record(self.filling, tooth=36, surfaces=["O", "M"])
        self.assertEqual(self.procedures_today(), 1)
        before = call(self.doc, "GET", "/api/v1/today").json()["version"]
        r = self.remove(record["id"])
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["invoice"]["total"], self.fee)
        self.assertEqual([line["kind"] for line in body["invoice"]["lines"]], ["service"])
        self.assertGreater(body["version"], before)
        self.assertEqual(self.rows(VisitProcedure, appointment_id=self.visit["id"]), [])
        self.assertEqual(self.rows(InvoiceLine, id=record["invoice_line_id"]), [])
        self.assertEqual(self.procedures_today(), 0)
        # The tooth had no state before: it has none again, and its history has no trace of the mistake.
        self.assertEqual(self.rows(Tooth, patient_id=self.patient), [])
        self.assertEqual(self.rows(ToothHistory, patient_id=self.patient), [])
        [audit] = self.rows(AuditChange, action="visit_procedure.remove")
        self.assertEqual((audit["target_type"], audit["target_id"]), ("visit_procedure", record["id"]))
        self.assertEqual((audit["details"]["before"]["amount"], audit["details"]["before"]["tooth"]),
                         (self.filling["price"], 36))
        self.assertTrue(self.rows(AuditChange, action="invoice.line_remove"))

    def test_the_tooth_returns_exactly_to_its_state_before(self):
        url = f"/api/v1/patients/{self.patient}/teeth/36"
        r = call(self.doc, "PUT", url, {"condition": "caries", "surfaces": "OD", "note": "بداية تسوس",
                                        "expected_condition": None})
        self.assertEqual(r.status_code, 200, r.content)
        [was] = self.rows(Tooth, patient_id=self.patient, tooth=36)
        record = self.record(self.exam, tooth=36, note="فحص")  # history only: the state stays
        record2 = self.record(self.filling, tooth=36, surfaces=["O", "M"], note="حشو")
        r = self.remove(record2["id"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["tooth"]["condition"], r.json()["tooth"]["surfaces"]), ("caries", ["O", "D"]))
        [now] = self.rows(Tooth, patient_id=self.patient, tooth=36)
        self.assertEqual(now, was)  # condition, surfaces, note, updated_at and updated_by: as they were
        history = [(h["condition"], h["visit_procedure_id"]) for h in self.rows(ToothHistory, patient_id=self.patient)]
        self.assertEqual(history, [("caries", None), ("caries", record["id"])])

    def test_a_tooth_changed_after_keeps_its_latest_state(self):
        record = self.record(self.filling, tooth=36, surfaces=["O"])
        url = f"/api/v1/patients/{self.patient}/teeth/36"
        self.assertEqual(call(self.doc, "PUT", url, {"condition": "fractured", "expected_condition": "filling"})
                         .status_code, 200)
        r = self.remove(record["id"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.rows(VisitProcedure, id=record["id"])), 0)
        self.assertEqual(len(self.rows(InvoiceLine, id=record["invoice_line_id"])), 0)
        self.assertEqual(self.procedures_today(), 0)
        self.assertEqual([t["condition"] for t in self.rows(Tooth, patient_id=self.patient)], ["fractured"])
        self.assertNotIn(record["id"], [h["visit_procedure_id"] for h in self.rows(ToothHistory, patient_id=self.patient)])

    def test_a_record_that_set_no_state_leaves_a_tooth_changed_since(self):
        record = self.record(self.exam, tooth=16)
        url = f"/api/v1/patients/{self.patient}/teeth/16"
        self.assertEqual(call(self.doc, "PUT", url, {"condition": "caries", "surfaces": "O", "expected_condition": None})
                         .status_code, 200)
        r = self.remove(record["id"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([t["condition"] for t in self.rows(Tooth, patient_id=self.patient)], ["caries"])
        self.assertEqual([h["visit_procedure_id"] for h in self.rows(ToothHistory, patient_id=self.patient)], [None])

    def test_materials_come_back_at_the_cost_they_left_at(self):
        r = call(self.owner, "POST", "/api/v1/inventory/items",
                 {"name": "Composite", "quantity": 3, "threshold": 0, "unit_cost": 4000})
        item = r.json()["item"]
        r = call(self.owner, "PUT", f"/api/v1/inventory/materials/{self.filling['id']}",
                 {"lines": [{"item_id": item["id"], "qty": 1}]})
        self.assertEqual(r.status_code, 200, r.content)
        record = self.record(self.filling, tooth=11, qty=1)
        # A purchase between: the return is averaged in at the cost it left at.
        r = call(self.owner, "POST", f"/api/v1/inventory/items/{item['id']}/purchases", {"qty": 2, "unit_price": 6000})
        self.assertEqual(r.json()["item"]["unit_cost"], 5000)
        self.assertEqual(self.remove(record["id"]).status_code, 200)
        [stock] = self.rows(Item, id=item["id"])
        self.assertEqual((stock["quantity"], stock["unit_cost"]), (5, (4 * 5000 + 4000 + 2) // 5))
        moves = [(m["kind"], m["qty"], m["unit_cost"], m["balance_after"], m["visit_procedure_id"])
                 for m in self.rows(Movement, item_id=item["id"]) if m["visit_procedure_id"]]
        self.assertEqual(moves, [("use", -1, 4000, 2, record["id"]), ("return", 1, 4000, 5, record["id"])])

    def test_a_plan_item_goes_back_to_the_status_it_had(self):
        root_canal = self.procedure("Root canal treatment, molar")
        r = call(self.doc, "POST", f"/api/v1/patients/{self.patient}/plans",
                 {"items": [{"procedure_id": root_canal["id"], "tooth": 46},
                            {"procedure_id": root_canal["id"], "tooth": 36}]})
        first, second = r.json()["plan"]["items"]
        self.assertEqual(call(self.doc, "POST", f"/api/v1/plan-items/{first['id']}/status",
                              {"status": "in_progress", "expected": "planned"}).status_code, 200)
        for item, was in ((first, "in_progress"), (second, "planned")):
            record = self.record(root_canal, plan_item_id=item["id"])
            self.assertEqual(self.rows(PlanItem, id=item["id"])[0]["status"], "done")
            self.assertEqual(self.remove(record["id"]).status_code, 200)
            [now] = self.rows(PlanItem, id=item["id"])
            self.assertEqual((now["status"], now["done_appointment_id"]), (was, None))
        # It can be recorded again.
        self.record(root_canal, plan_item_id=first["id"])

    def test_a_used_package_session_comes_back(self):
        scaling = self.procedure("Scaling and polishing", client=self.owner)
        r = call(self.owner, "POST", "/api/v1/packages",
                 {"procedure_id": scaling["id"], "name": "Scaling x3", "sessions": 3, "price": 60000})
        package = r.json()["package"]
        self.assertEqual(call(self.desk, "POST", f"/api/v1/appointments/{self.visit['id']}/packages",
                              {"package_id": package["id"]}).status_code, 201)
        record = self.record(scaling)
        packages = f"/api/v1/patients/{self.patient}/packages"
        self.assertEqual(call(self.desk, "GET", packages).json()["items"][0]["left"], 2)
        self.assertEqual(self.remove(record["id"]).status_code, 200)
        self.assertEqual(call(self.desk, "GET", packages).json()["items"][0]["left"], 3)

    def test_removed_after_paying_leaves_the_patient_a_credit(self):
        # No more "refund first" (Saged, 2026-11-22): the money paid for it is the patient's credit.
        self.sees_money()
        record = self.record(self.filling, tooth=36)
        total = self.fee + self.filling["price"]
        r = call(self.desk, "POST", f"/api/v1/appointments/{self.visit['id']}/payments", {"amount": total, "method": "cash"})
        self.assertEqual(r.status_code, 201, r.content)
        r = self.remove(record["id"])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["invoice"]["credit"], self.filling["price"])
        self.assertEqual(len(self.rows(VisitProcedure, id=record["id"])), 0)

    def test_refused_when_a_typed_discount_exceeds_what_is_left(self):
        record = self.record(self.filling, tooth=36)
        bill = self.bill()
        r = call(self.owner, "PUT", f"/api/v1/invoices/{bill['id']}/discount",
                 {"discount": self.fee + 1, "expected_discount": 0})
        self.assertEqual(r.status_code, 200, r.content)
        r = self.remove(record["id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "discount_exceeds"))

    def test_only_while_the_visit_is_on(self):
        record = self.record(self.filling, tooth=36)
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room").status_code, 200)
        r = self.remove(record["id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))
        self.assertEqual(len(self.rows(VisitProcedure, id=record["id"])), 1)
        booked = self.booked()
        r = self.remove(record["id"], visit_id=booked["id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))

    def test_needs_procedures_record_and_a_doctor_without_money_sees_no_amount(self):
        record = self.record(self.filling, tooth=36)
        self.assertEqual(self.remove(record["id"], client=self.desk).status_code, 403)
        r = self.remove(record["id"])  # the doctor holds no money.view by default
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["invoice"])
        self.assertNotIn(str(self.filling["price"]), r.content.decode())

    def test_another_visit_or_another_place_is_404(self):
        record = self.record(self.filling, tooth=36)
        # Another visit of the doctor's, waiting (on, so only the record's visit decides): this record isn't on it.
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in",
                 {"service_id": self.services_a["procedure"].id, "doctor_id": self.a.membership("doctor").id,
                  "new_patient": {"full_name": "Sara Hassan Ali", "phone": "01098765432", "sex": "f", "age": 25}})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.remove(record["id"], visit_id=r.json()["appointment"]["id"]).status_code, 404)
        b = self.b.client("owner")
        self.assertEqual(call(b, "DELETE", f"/api/v1/appointments/{self.visit['id']}/procedures/{record['id']}")
                         .status_code, 404)
        self.assertEqual(self.remove(10**15).status_code, 404)
        self.assertEqual(len(self.rows(VisitProcedure, id=record["id"])), 1)

    def test_a_repeat_with_the_same_key_replays_and_removes_once(self):
        record = self.record(self.filling, tooth=36)
        key = uuid.uuid4()
        first, again = self.remove(record["id"], key=key), self.remove(record["id"], key=key)
        self.assertEqual((first.status_code, again.status_code), (200, 200), again.content)
        self.assertEqual(self.remove(record["id"]).status_code, 404)  # a second click with its own key
        self.assertEqual(self.procedures_today(), 0)
        self.assertEqual(len(self.rows(AuditChange, action="visit_procedure.remove")), 1)


class RemoveUnderDepositTests(DepositTestCase):
    """A deposit may stay over the new total; other payments may not."""

    def test_removed_under_the_deposit_but_not_under_more(self):
        with self.on(0):
            self.deposit_on()
            braces, scaling = self.prices()
            visit = self.book(payment=self.deposit(), doctor_id=self.owner_id).json()["appointment"]
            self.to_the_room(visit["id"])
            url = f"/api/v1/appointments/{visit['id']}/procedures"
            first, second = (call(self.owner, "POST", url, {"procedure_id": p["id"]}).json()["procedure"]
                             for p in (braces, scaling))
            r = call(self.owner, "DELETE", f"{url}/{first['id']}")
            self.assertEqual(r.status_code, 200, r.content)
            total = self.figures(visit["id"])["total"]
            self.assertEqual(self.figures(visit["id"])["paid"], DEPOSIT)
            self.assertEqual(r.json()["invoice"]["total"], total)
            # Pay everything that is owed now: removing the last procedure would leave paid over total and deposit.
            self.assertEqual(self.pay(visit["id"], total - DEPOSIT).status_code, 201)
            r = call(self.owner, "DELETE", f"{url}/{second['id']}")
            self.assertEqual(r.status_code, 200, r.content)  # the patient's credit now (Saged, 2026-11-22)
