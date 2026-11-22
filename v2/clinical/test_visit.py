"""The visit (design E3): finishing with the earlier balance and the next booking, notes, vitals, procedures,
the dental chart, treatment plans, the timeline and the patient's balance."""

import datetime as dt

from audit.models import AuditAccess, AuditChange
from billing.models import Invoice, InvoiceLine, Payment
from clinical.models import Tooth, ToothHistory, VisitNoteRevision
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import summaries, today

from .testing import M2TestCase, put


def invoices(fixture, patient_id):
    with clinic_transaction(fixture.id, fixture.shard):
        return {i.appointment_id: (i.total, i.paid) for i in Invoice.objects.filter(clinic_id=fixture.id,
                                                                                    patient_id=patient_id)}


def payments(fixture, patient_id):
    with clinic_transaction(fixture.id, fixture.shard):
        return list(Payment.objects.filter(clinic_id=fixture.id, patient_id=patient_id).order_by("id")
                    .values_list("invoice__appointment_id", "amount", "method", "doctor_id"))


class FinishTests(M2TestCase):
    """E3 'Finishing the visit': one request, the earlier balance oldest first, the next visit in the same transaction."""

    def setUp(self):
        super().setUp()
        self.fee = self.services_a["procedure"].price
        first = self.owed_visit()
        self.patient = first["patient"]["id"]
        self.first = first["id"]
        self.second = self.owed_visit(self.patient)["id"]
        self.visit = self.in_room(self.patient)["id"]

    def test_the_earlier_balance_pays_the_oldest_invoice_first(self):
        r = self.act(self.visit, "finish", "in_room", payment={"amount": self.fee, "method": "card"},
                     earlier={"amount": self.fee + 100_00, "method": "transfer"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(invoices(self.a, self.patient), {self.first: (self.fee, self.fee),
                                                         self.second: (self.fee, 100_00),
                                                         self.visit: (self.fee, self.fee)})
        body = r.json()
        self.assertEqual(body["invoice"]["remaining"], 0)
        self.assertEqual(body["balance"]["owed"], self.fee - 100_00)
        self.assertEqual([i["appointment_id"] for i in body["balance"]["invoices"]], [self.second])
        doctor = self.a.membership("doctor").id  # the visits' doctor
        self.assertEqual(payments(self.a, self.patient), [(self.visit, self.fee, "card", doctor),
                                                          (self.first, self.fee, "transfer", doctor),
                                                          (self.second, 100_00, "transfer", doctor)])
        clinic, doctors = summaries(self.a, today(self.a))
        self.assertEqual(clinic["collected"], 2 * self.fee + 100_00)
        self.assertEqual(doctors[doctor]["collected"], 2 * self.fee + 100_00)

    def test_more_than_the_earlier_balance_is_refused_and_nothing_happens(self):
        r = self.act(self.visit, "finish", "in_room", earlier={"amount": 2 * self.fee + 1, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "over_balance"))
        self.assertEqual(self.appointment(self.visit).status, "in_room")
        self.assertEqual(payments(self.a, self.patient), [])

    def test_the_next_visit_is_booked_in_the_same_transaction(self):
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()
        owner = self.a.membership("owner").id
        r = self.act(self.visit, "finish", "in_room", next_visit={
            "doctor_id": owner, "service_id": self.services_a["consultation"].id, "day": tomorrow})
        self.assertEqual(r.status_code, 200, r.content)
        following = r.json()["next_appointment"]
        self.assertEqual((following["day"], following["status"], following["patient"]["id"]),
                         (tomorrow, "booked", self.patient))
        _clinic, doctors = summaries(self.a, dt.date.fromisoformat(tomorrow))
        self.assertEqual((doctors[owner]["booked"], doctors[owner]["last_queue_no"]), (1, 1))
        _clinic, doctors = summaries(self.a, today(self.a))
        self.assertEqual(doctors[self.a.membership("doctor").id]["done"], 3)

    def test_a_next_visit_that_cant_be_booked_undoes_the_finish_and_its_payments(self):
        yesterday = (today(self.a) - dt.timedelta(days=1)).isoformat()
        r = self.act(self.visit, "finish", "in_room", payment={"amount": self.fee, "method": "cash"},
                     earlier={"amount": self.fee, "method": "cash"},
                     next_visit={"doctor_id": self.a.membership("owner").id,
                                 "service_id": self.services_a["consultation"].id, "day": yesterday})
        self.assertEqual(r.status_code // 100, 4, r.content)
        self.assertEqual(self.appointment(self.visit).status, "in_room")
        self.assertEqual(payments(self.a, self.patient), [])

    def test_a_finish_after_someone_elses_with_an_earlier_payment_is_already_finished(self):
        self.assertEqual(self.act(self.visit, "finish", "in_room").status_code, 200)
        r = self.act(self.visit, "finish", "in_room", earlier={"amount": 100, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_finished"))
        self.assertEqual(payments(self.a, self.patient), [])

    def test_the_earlier_balance_needs_billing_collect(self):
        self.set_codes("assistant", revoked=["billing.collect"])
        r = self.act(self.visit, "finish", "in_room", earlier={"amount": 100, "method": "cash"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.appointment(self.visit).status, "in_room")

    def test_a_repeat_with_the_same_key_replays_and_pays_once(self):
        import uuid

        key = uuid.uuid4()
        body = {"status": "in_room", "earlier": {"amount": self.fee, "method": "cash"}}
        first = call(self.desk, "POST", f"/api/v1/appointments/{self.visit}/finish", body, key=key)
        again = call(self.desk, "POST", f"/api/v1/appointments/{self.visit}/finish", body, key=key)
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(again.json()["balance"], first.json()["balance"])
        self.assertEqual(len(payments(self.a, self.patient)), 1)


class NoteTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.url = f"/api/v1/appointments/{self.visit['id']}/note"

    def test_versioned_with_if_match_and_a_mismatch_shows_the_current_text(self):
        r = put(self.doc, self.url, {"complaint": "ألم في الضرس", "notes": "حساسية من البارد"}, 0)
        self.assertEqual((r.status_code, r.json()["note"]["version"]), (200, 1), r.content)
        r = put(self.doc, self.url, {"complaint": "ألم في الضرس", "diagnosis": "تسوس عميق", "notes": "x"}, 1)
        self.assertEqual(r.json()["note"]["version"], 2)
        stale = put(self.owner, self.url, {"complaint": "other"}, 1)
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))
        self.assertEqual(stale.json()["error"]["note"]["diagnosis"], "تسوس عميق")
        self.assertEqual(put(self.owner, self.url, {"complaint": "other"}, 0).status_code, 409)  # create on top of one

    def test_if_match_is_required(self):
        r = put(self.doc, self.url, {"complaint": "x"}, None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (428, "precondition_required"))

    def test_the_same_text_again_is_a_harmless_repeat(self):
        put(self.doc, self.url, {"complaint": "x"}, 0)
        r = put(self.doc, self.url, {"complaint": "x"}, 0)
        self.assertEqual((r.status_code, r.json()["changed"], r.json()["note"]["version"]), (200, False, 1))

    def test_writing_needs_visits_notes_and_reading_needs_clinical_view_with_an_access_event(self):
        self.assertEqual(put(self.desk, self.url, {"complaint": "x"}, 0).status_code, 403)
        put(self.doc, self.url, {"complaint": "x"}, 0)
        self.assertEqual(call(self.desk, "GET", self.url).status_code, 200) # the desk reads the file (Saged, 2026-11-19)
        with clinic_transaction(self.a.id, self.a.shard):
            before = AuditAccess.objects.filter(clinic_id=self.a.id, action=AuditAccess.Action.NOTE).count()
        self.assertEqual(call(self.doc, "GET", self.url).json()["note"]["complaint"], "x")
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id, action=AuditAccess.Action.NOTE).count(),
                             before + 1)

    def test_an_edit_after_the_visit_day_keeps_the_old_text_as_a_revision(self):
        put(self.doc, self.url, {"complaint": "first"}, 0)
        put(self.doc, self.url, {"complaint": "second"}, 1)  # same day: no revision
        from unittest import mock

        with mock.patch("clinical.services.business_day", return_value=today(self.a) + dt.timedelta(days=1)):
            self.assertEqual(put(self.doc, self.url, {"complaint": "third"}, 2).status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            revisions = list(VisitNoteRevision.objects.filter(clinic_id=self.a.id).values_list("version", "complaint"))
        self.assertEqual(revisions, [(2, "second")])

    def test_a_cancelled_visit_takes_no_note(self):
        booked = self.booked()
        self.assertEqual(self.act(booked["id"], "cancel", "booked").status_code, 200)
        r = put(self.doc, f"/api/v1/appointments/{booked['id']}/note", {"complaint": "x"}, 0)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))


class VitalsTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.url = f"/api/v1/appointments/{self.in_room()['id']}/vitals"

    def test_an_assistant_enters_them_and_every_field_is_optional(self):
        r = put(self.desk, self.url, {"bp_systolic": 120, "bp_diastolic": 80, "temperature": "37.25", "weight": "٧٢٫٥"}, 0)
        self.assertEqual(r.status_code, 200, r.content)
        vitals = r.json()["vitals"]
        self.assertEqual((vitals["temperature"], vitals["weight"], vitals["pulse"], vitals["version"]),
                         ("37.2", "72.5", None, 1))  # one decimal place, Arabic digits accepted
        r = put(self.desk, self.url, {"pulse": 88}, 1)
        self.assertEqual((r.json()["vitals"]["pulse"], r.json()["vitals"]["bp_systolic"]), (88, None))  # absolute values

    def test_out_of_range_and_half_blood_pressures_are_clear_400s(self):
        for body, field in (({"pulse": 400}, "pulse"), ({"spo2": 101}, "spo2"), ({"temperature": "50"}, "temperature"),
                            ({"bp_systolic": 120}, "bp_diastolic"), ({"bp_systolic": 80, "bp_diastolic": 90}, "bp_diastolic"),
                            ({"weight": "heavy"}, "weight"), ({"height": True}, "height"), ({"mood": 1}, "mood")):
            r = put(self.desk, self.url, body, 0)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(field, r.json()["error"]["fields"], body)

    def test_a_stale_version_is_409_and_reading_is_logged(self):
        put(self.desk, self.url, {"pulse": 70}, 0)
        r = put(self.desk, self.url, {"pulse": 75}, 0)
        self.assertEqual((r.status_code, r.json()["error"]["vitals"]["pulse"]), (409, 70))
        self.assertEqual(call(self.desk, "GET", self.url).json()["vitals"]["pulse"], 70)  # visits.vitals may read them


class RecordProcedureTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.url = f"/api/v1/appointments/{self.visit['id']}/procedures"
        self.filling = self.procedure("Composite filling, two surfaces")
        self.sees_money()  # these tests read the bill the doctor's procedures make

    def test_a_procedure_adds_an_invoice_line_and_sets_the_tooth_with_history(self):
        r = call(self.doc, "POST", self.url, {"procedure_id": self.filling["id"], "tooth": 36, "surfaces": ["O", "M"]})
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["tooth"]["condition"], body["tooth"]["surfaces"]), ("filling", ["M", "O"]))
        self.assertEqual(body["invoice"]["total"], self.services_a["procedure"].price + self.filling["price"])
        with clinic_transaction(self.a.id, self.a.shard):
            line = InvoiceLine.objects.get(clinic_id=self.a.id, id=body["procedure"]["invoice_line_id"])
            self.assertEqual((line.kind, line.amount, line.from_list), ("procedure", self.filling["price"], True))
            history = list(ToothHistory.objects.filter(clinic_id=self.a.id, tooth=36).values_list(
                "condition", "surfaces", "appointment_id", "procedure_id"))
        self.assertEqual(history, [("filling", ["M", "O"], self.visit["id"], self.filling["id"])])

    def test_a_price_not_from_the_list_is_refused_for_everyone(self):
        """No one edits prices, the owner included (Saged, 2026-11-16)."""
        body = {"procedure_id": self.filling["id"], "tooth": 36, "unit_price": self.filling["price"] - 100_00}
        for client in (self.doc, self.owner):
            r = call(client, "POST", self.url, body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"))

    def test_bad_teeth_and_surfaces_are_400(self):
        crown = self.procedure("Zirconia crown")
        # No tooth at all is fine (Saged, 2026-11-13: one tap on the visit screen); a wrong one is not.
        for body, field in (({"procedure_id": self.filling["id"], "surfaces": ["O"]}, "tooth"),
                            ({"procedure_id": self.filling["id"], "tooth": 19}, "tooth"),
                            ({"procedure_id": self.filling["id"], "tooth": 86}, "tooth"),
                            ({"procedure_id": self.filling["id"], "tooth": 36, "surfaces": ["X"]}, "surfaces"),
                            ({"procedure_id": self.filling["id"], "tooth": 36, "surfaces": ["O", "O"]}, "surfaces"),
                            ({"procedure_id": crown["id"], "tooth": 36, "surfaces": ["O"]}, "surfaces"),
                            ({"procedure_id": self.filling["id"], "tooth": 36, "qty": 0}, "qty")):
            r = call(self.doc, "POST", self.url, body)
            self.assertEqual(r.status_code, 400, (body, r.content))
            self.assertIn(field, r.json()["error"]["fields"], body)

    def test_a_primary_tooth_works_and_a_booked_visit_is_refused(self):
        extraction = self.procedure("Primary tooth extraction")
        r = call(self.doc, "POST", self.url, {"procedure_id": extraction["id"], "tooth": 85})
        self.assertEqual((r.status_code, r.json()["tooth"]["condition"]), (201, "extracted"))
        booked = self.booked()
        r = call(self.doc, "POST", f"/api/v1/appointments/{booked['id']}/procedures",
                 {"procedure_id": extraction["id"], "tooth": 85})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "visit_closed"))

    def test_a_procedure_goes_on_a_visit_once_and_a_tooth_procedure_once_per_tooth(self):
        """Saged, 2026-11-17: a second tap says "already added"; another tooth is another procedure."""
        scaling = self.procedure("Scaling and polishing")
        self.assertEqual(call(self.doc, "POST", self.url, {"procedure_id": scaling["id"]}).status_code, 201)
        r = call(self.doc, "POST", self.url, {"procedure_id": scaling["id"]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_added"))
        r = call(self.doc, "POST", self.url, {"procedure_id": scaling["id"], "qty": 2})
        self.assertEqual(r.status_code, 409)  # more of it is a quantity on the first, not a second line
        self.assertEqual(call(self.doc, "POST", self.url, {"procedure_id": self.filling["id"], "tooth": 36}).status_code, 201)
        self.assertEqual(call(self.doc, "POST", self.url, {"procedure_id": self.filling["id"], "tooth": 46}).status_code, 201)
        r = call(self.doc, "POST", self.url, {"procedure_id": self.filling["id"], "tooth": 36, "surfaces": ["O"]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_added"))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(InvoiceLine.objects.filter(clinic_id=self.a.id, kind="procedure").count(), 3)
        # Another visit of the same patient takes it again.
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room").status_code, 200)
        later = self.in_room(self.visit["patient"]["id"])
        r = call(self.doc, "POST", f"/api/v1/appointments/{later['id']}/procedures", {"procedure_id": scaling["id"]})
        self.assertEqual(r.status_code, 201, r.content)

    def test_the_doctors_list_leaves_out_what_the_visit_fee_charges(self):
        """The examination and consultation items would charge the كشف twice (Saged, 2026-11-17)."""
        r = self.doc.get(f"/visits/{self.visit['id']}")
        listed = {p["id"] for p in r.context["config"]["procedures"]}
        self.assertNotIn(self.procedure("Full examination")["id"], listed)
        self.assertNotIn(self.procedure("Orthodontic consultation")["id"], listed)
        self.assertIn(self.procedure("Scaling and polishing")["id"], listed)
        self.assertTrue(set(r.context["config"]["top"]) <= listed)

    def test_recording_needs_procedures_record(self):
        r = call(self.desk, "POST", self.url, {"procedure_id": self.filling["id"], "tooth": 36})
        self.assertEqual(r.status_code, 403)


class ChartAndPlanTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.patient = self.visit["patient"]["id"]
        self.root_canal = self.procedure("Root canal treatment, molar")
        self.crown = self.procedure("Zirconia crown")
        self.sees_money()  # the plans' estimated totals

    def plan(self, **body):
        body.setdefault("items", [{"procedure_id": self.root_canal["id"], "tooth": 46},
                                  {"procedure_id": self.crown["id"], "tooth": 46}])
        return call(self.doc, "POST", f"/api/v1/patients/{self.patient}/plans", body)

    def test_a_plan_shows_its_estimated_total_and_open_teeth_are_in_progress(self):
        r = self.plan(title="علاج 46")
        self.assertEqual(r.status_code, 201, r.content)
        plan = r.json()["plan"]
        self.assertEqual((plan["estimated_total"], plan["open_count"], plan["unfinished"]),
                         (self.root_canal["price"] + self.crown["price"], 2, True))
        chart = call(self.doc, "GET", f"/api/v1/patients/{self.patient}/teeth").json()
        self.assertEqual([(t["tooth"], t["in_progress"]) for t in chart["teeth"]], [(46, True)])
        self.assertEqual(len(chart["numbers"]["permanent"]) + len(chart["numbers"]["primary"]), 52)

    def test_recording_a_plan_item_marks_it_done_at_the_plan_price(self):
        plan = self.plan(items=[{"procedure_id": self.root_canal["id"], "tooth": 46}]).json()["plan"]
        item = plan["items"][0]
        r = call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/procedures",
                 {"procedure_id": self.root_canal["id"], "plan_item_id": item["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["procedure"]["tooth"], r.json()["tooth"]["condition"]), (46, "root_canal"))
        plan = call(self.doc, "GET", f"/api/v1/patients/{self.patient}/plans").json()["items"][0]
        self.assertEqual((plan["items"][0]["status"], plan["items"][0]["done_appointment_id"], plan["unfinished"]),
                         ("done", self.visit["id"], False))
        again = call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/procedures",
                     {"procedure_id": self.root_canal["id"], "plan_item_id": item["id"]})
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "status_changed"))

    def test_plan_items_move_from_the_status_the_screen_saw(self):
        item = self.plan().json()["plan"]["items"][0]
        url = f"/api/v1/plan-items/{item['id']}/status"
        self.assertEqual(call(self.doc, "POST", url, {"status": "in_progress", "expected": "planned"}).status_code, 200)
        repeat = call(self.doc, "POST", url, {"status": "in_progress", "expected": "planned"})
        self.assertEqual((repeat.status_code, repeat.json()["changed"]), (200, False))
        self.assertEqual(call(self.doc, "POST", url, {"status": "dropped", "expected": "planned"}).status_code, 409)
        self.assertEqual(call(self.doc, "POST", url, {"status": "dropped", "expected": "in_progress"}).status_code, 200)
        final = call(self.doc, "POST", url, {"status": "planned", "expected": "dropped"})
        self.assertEqual((final.status_code, final.json()["error"]["code"]), (400, "invalid_transition"))
        plan = call(self.doc, "GET", f"/api/v1/patients/{self.patient}/plans").json()["items"][0]
        self.assertEqual(plan["estimated_total"], self.crown["price"])  # a dropped item leaves the estimate

    def test_a_price_off_the_list_in_a_plan_needs_billing_discount(self):
        items = [{"procedure_id": self.crown["id"], "tooth": 11, "price": 1}]
        self.assertEqual(self.plan(items=items).status_code, 403)

    def test_an_examination_finding_and_the_tooth_history_newest_first(self):
        url = f"/api/v1/patients/{self.patient}/teeth/16"
        r = call(self.doc, "PUT", url, {"condition": "caries", "surfaces": "OD", "expected_condition": None})
        self.assertEqual((r.status_code, r.json()["tooth"]["surfaces"]), (200, ["O", "D"]), r.content)
        stale = call(self.doc, "PUT", url, {"condition": "fractured", "expected_condition": None})
        self.assertEqual((stale.status_code, stale.json()["error"]["tooth"]["condition"]), (409, "caries"))
        self.assertEqual(call(self.doc, "PUT", url, {"condition": "crown", "surfaces": ["O"]}).status_code, 400)
        self.assertEqual(call(self.doc, "PUT", url, {"condition": "fractured", "expected_condition": "caries"}).status_code,
                         200)
        history = call(self.doc, "GET", f"{url}/history").json()["items"]
        self.assertEqual([h["condition"] for h in history], ["fractured", "caries"])
        self.assertEqual(call(self.doc, "GET", f"/api/v1/patients/{self.patient}/teeth/19/history").status_code, 404)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Tooth.objects.get(clinic_id=self.a.id, tooth=16).condition, "fractured")


    def test_several_teeth_are_saved_together_or_not_at_all(self):
        # One Save for every tooth changed (Saged, 2026-11-22).
        url = f"/api/v1/patients/{self.patient}/teeth"
        r = call(self.doc, "PUT", url, {"teeth": [
            {"tooth": 16, "condition": "caries", "surfaces": ["O"], "expected_condition": None},
            {"tooth": 26, "condition": "missing", "expected_condition": None},
            {"tooth": 36, "condition": "filling", "surfaces": ["M", "O"], "note": "قديم"}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(sorted((t["tooth"], t["condition"]) for t in r.json()["teeth"]),
                         [(16, "caries"), (26, "missing"), (36, "filling")])
        # One stale tooth refuses the whole save: nothing changes.
        stale = call(self.doc, "PUT", url, {"teeth": [
            {"tooth": 17, "condition": "caries", "surfaces": ["O"], "expected_condition": None},
            {"tooth": 16, "condition": "fractured", "expected_condition": None}]})
        self.assertEqual((stale.status_code, stale.json()["error"]["tooth"]["tooth"]), (409, 16))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertFalse(Tooth.objects.filter(clinic_id=self.a.id, tooth=17).exists())
        bad = [{"tooth": 16, "condition": "caries"}, {"tooth": 16, "condition": "missing"}]
        self.assertEqual(call(self.doc, "PUT", url, {"teeth": bad}).status_code, 400)  # the same tooth twice
        self.assertEqual(call(self.doc, "PUT", url, {"teeth": []}).status_code, 400)
        self.assertEqual(call(self.a.client("assistant"), "PUT", url, {"teeth": bad[:1]}).status_code, 403)


class TimelineAndBalanceTests(M2TestCase):
    def test_the_timeline_is_newest_first_across_kinds_and_pages_without_gaps(self):
        self.sees_money()  # the timeline's payments
        first = self.owed_visit()
        patient = first["patient"]["id"]
        visit = self.in_room(patient)
        filling = self.procedure("Composite filling, one surface")
        call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": filling["id"], "tooth": 21})
        call(self.doc, "POST", "/api/v1/prescriptions", {"patient_id": patient, "appointment_id": visit["id"],
                                                         "items": [{"name": "Panadol", "dose": "قرص"}]})
        self.act(visit["id"], "finish", "in_room", payment={"amount": 100_00, "method": "cash"})
        whole = call(self.doc, "GET", f"/api/v1/patients/{patient}/timeline?limit=100").json()
        kinds = [e["type"] for e in whole["items"]]
        self.assertEqual(sorted(kinds), ["payment", "prescription", "procedure", "visit", "visit"])
        self.assertEqual([e["at"] for e in whole["items"]], sorted((e["at"] for e in whole["items"]), reverse=True))
        seen, after = [], None
        while True:
            page = call(self.doc, "GET", f"/api/v1/patients/{patient}/timeline?limit=2"
                                          + (f"&after={after}" if after else "")).json()
            seen += [(e["type"], e["id"]) for e in page["items"]]
            after = page["next"]
            if not after:
                break
        self.assertEqual(seen, [(e["type"], e["id"]) for e in whole["items"]])
        self.assertEqual(call(self.doc, "GET", f"/api/v1/patients/{patient}/timeline?after=junk").status_code, 400)

    def test_the_balance_covers_every_open_invoice_oldest_first(self):
        first = self.owed_visit()
        patient = first["patient"]["id"]
        second = self.owed_visit(patient)
        balance = call(self.desk, "GET", f"/api/v1/patients/{patient}/balance").json()["balance"]
        fee = self.services_a["procedure"].price
        self.assertEqual((balance["owed"], [i["appointment_id"] for i in balance["invoices"]]),
                         (2 * fee, [first["id"], second["id"]]))

    def test_hot_reads_run_a_fixed_number_of_statements_whatever_the_history(self):
        visit = self.in_room()
        patient = visit["patient"]["id"]

        def counts():
            return (self.queries(lambda: call(self.doc, "GET", f"/api/v1/appointments/{visit['id']}/record")),
                    self.queries(lambda: call(self.doc, "GET", f"/api/v1/patients/{patient}/timeline")),
                    self.queries(lambda: call(self.doc, "GET", f"/api/v1/patients/{patient}/teeth")))

        filling = self.procedure("Composite filling, one surface")
        call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": filling["id"], "tooth": 21})
        small = counts()  # the visit has its invoice from here on
        # Three more visits of the patient's (on later days: one open visit a day, Saged 2026-11-17), closed directly.
        import datetime as dt

        from scheduling.models import Appointment
        from scheduling.testing import today

        now = dt.datetime.now(dt.UTC)
        for days in (1, 2, 3):
            more = self.book(patient_id=patient, day=(today(self.a) + dt.timedelta(days=days)).isoformat()).json()
            with clinic_transaction(self.a.id, self.a.shard):
                Appointment.objects.filter(clinic_id=self.a.id, id=more["appointment"]["id"]).update(
                    status="done", arrived_at=now, called_at=now, finished_at=now)
        for tooth in (11, 12, 13):
            call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                 {"procedure_id": filling["id"], "tooth": tooth})
        self.assertEqual(counts(), small)


class AuditTests(M2TestCase):
    def test_a_procedure_price_change_is_in_the_change_log(self):
        crown = self.procedure("Zirconia crown")
        r = call(self.owner, "PATCH", f"/api/v1/procedures/{crown['id']}", {"changes": {"price": crown["price"] + 500_00}})
        self.assertEqual(r.status_code, 200, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            change = AuditChange.objects.get(clinic_id=self.a.id, action="procedure.price")
        self.assertEqual(change.details, {"before": {"price": crown["price"]}, "after": {"price": crown["price"] + 500_00}})

