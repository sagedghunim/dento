"""T1, T2, T3 and T9 over every M2 clinical endpoint, with the list checked against the URL configuration."""

import shutil
import tempfile
import uuid
from types import SimpleNamespace

from django.test import override_settings
from django.urls import get_resolver

from audit.models import AuditAccess
from clinical import models as m
from core.routing import clinic_transaction
from core.testing import call

from .testing import PNG, M2TestCase, upload

# What the doctor template lacks among the codes these endpoints need.
EXTRA = ["settings.manage", "billing.discount", "appointments.others", "appointments.book"]  # others: the visits are the owner's (G4)


def objects(test, fixture):
    """One of everything an endpoint can address, in `fixture`'s clinic."""
    doc = fixture.client("doctor")
    owner = fixture.client("owner")
    visit = test.in_room(fixture=fixture)
    patient = visit["patient"]["id"]
    procedures = call(doc, "GET", "/api/v1/procedures").json()["items"]
    filling = next(p for p in procedures if p["name_en"] == "Composite filling, one surface")
    crown = next(p for p in procedures if p["name_en"] == "Zirconia crown")
    medicine = call(doc, "GET", "/api/v1/medicines").json()["items"][0]
    template = call(doc, "GET", "/api/v1/prescription-templates").json()["items"][0]
    plan = call(doc, "POST", f"/api/v1/patients/{patient}/plans",
                {"items": [{"procedure_id": crown["id"], "tooth": 11}, {"procedure_id": crown["id"], "tooth": 21}]}).json()
    rx = call(doc, "POST", "/api/v1/prescriptions", {"patient_id": patient, "appointment_id": visit["id"],
                                                     "items": [{"name": "Panadol"}]}).json()["prescription"]
    attachment = upload(fixture.client("assistant"), f"/api/v1/patients/{patient}/attachments", PNG).json()["attachment"]
    own = call(owner, "POST", "/api/v1/procedures", {"name_ar": f"إجراء {uuid.uuid4().hex[:6]}", "price": 100}).json()
    section = call(owner, "GET", "/api/v1/clinic").json().get("sections", [{}])[0].get("id")
    follow = call(doc, "POST", f"/api/v1/appointments/{visit['id']}/follow-ups",
                  {"items": [{"label": "متابعة", "days": 7}, {"label": "متابعة", "days": 14}]}).json()["upcoming"]
    recorded = call(doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                    {"procedure_id": crown["id"], "tooth": 22}).json()["procedure"]  # one to remove
    return SimpleNamespace(visit=visit["id"], patient=patient, filling=filling["id"], crown=crown["id"],
                           medicine=medicine["id"], template=template["id"], template_version=template["version"],
                           plan=plan["plan"]["id"], item=plan["plan"]["items"][0]["id"],
                           item2=plan["plan"]["items"][1]["id"], rx=rx["id"], attachment=attachment["id"],
                           own_procedure=own["procedure"]["id"], section=section,
                           follow=follow[0]["id"], follow2=follow[1]["id"], recorded=recorded["id"])


def mutating(o):
    """name: (method, path, body, another body for the same key, codes, If-Match)."""
    v, p = o.visit, o.patient
    return {
        "note": ("PUT", f"/api/v1/appointments/{v}/note", {"complaint": "a"}, {"complaint": "b"}, ["visits.notes"], 0),
        "vitals": ("PUT", f"/api/v1/appointments/{v}/vitals", {"pulse": 80}, {"pulse": 81}, ["visits.vitals"], 0),
        "record": ("POST", f"/api/v1/appointments/{v}/procedures", {"procedure_id": o.filling, "tooth": 36},
                   {"procedure_id": o.filling, "tooth": 37}, ["procedures.record"], None),
        "remove procedure": ("DELETE", f"/api/v1/appointments/{v}/procedures/{o.recorded}", {}, {"x": 1},
                             ["procedures.record"], None),
        "tooth": ("PUT", f"/api/v1/patients/{p}/teeth/16", {"condition": "caries", "surfaces": ["O"]},
                  {"condition": "caries", "surfaces": ["D"]}, ["procedures.record"], None),
        "teeth": ("PUT", f"/api/v1/patients/{p}/teeth", {"teeth": [{"tooth": 17, "condition": "caries", "surfaces": ["O"]}]},
                  {"teeth": [{"tooth": 17, "condition": "caries", "surfaces": ["D"]}]}, ["procedures.record"], None),
        "plan": ("POST", f"/api/v1/patients/{p}/plans", {"items": [{"procedure_id": o.crown, "tooth": 12}]},
                 {"items": [{"procedure_id": o.crown, "tooth": 13}]}, ["procedures.record"], None),
        "plan items": ("POST", f"/api/v1/plans/{o.plan}/items", {"items": [{"procedure_id": o.crown, "tooth": 14}]},
                       {"items": [{"procedure_id": o.crown, "tooth": 15}]}, ["procedures.record"], None),
        "plan item status": ("POST", f"/api/v1/plan-items/{o.item}/status", {"status": "in_progress", "expected": "planned"},
                             {"status": "dropped", "expected": "planned"}, ["procedures.record"], None),
        "prescription": ("POST", "/api/v1/prescriptions", {"patient_id": p, "items": [{"medicine_id": o.medicine}],
                                                           "appointment_id": v},
                         {"patient_id": p, "items": [{"name": "x"}], "appointment_id": v}, ["prescriptions.write"], None),
        "medicine add": ("POST", "/api/v1/medicines", {"name": "Matrix Med"}, {"name": "Matrix Med 2"},
                         ["settings.medicines"], None),
        "medicine edit": ("PATCH", f"/api/v1/medicines/{o.medicine}", {"changes": {"default_note": "a"}},
                          {"changes": {"default_note": "b"}}, ["settings.medicines"], None),
        "medicine remove": ("DELETE", f"/api/v1/medicines/{o.medicine}", {}, {"x": 1}, ["settings.medicines"], None),
        "template add": ("POST", "/api/v1/prescription-templates", {"name": "Matrix", "items": [{"name": "a"}]},
                         {"name": "Matrix 2", "items": [{"name": "a"}]}, ["settings.medicines"], None),
        "template edit": ("PUT", f"/api/v1/prescription-templates/{o.template}", {"name": "T", "items": [{"name": "a"}]},
                          {"name": "T2", "items": [{"name": "a"}]}, ["settings.medicines"], o.template_version),
        "template remove": ("DELETE", f"/api/v1/prescription-templates/{o.template}", {}, {"x": 1},
                            ["settings.medicines"], None),
        "procedure add": ("POST", "/api/v1/procedures", {"name_ar": "مصفوفة", "price": 1}, {"name_ar": "مصفوفة 2"},
                          ["settings.manage", "procedures.manage"], None),
        "procedure edit": ("PATCH", f"/api/v1/procedures/{o.own_procedure}", {"changes": {"price": 5}},
                           {"changes": {"price": 6}}, ["settings.manage", "prices.edit"], None),
        "procedure remove": ("DELETE", f"/api/v1/procedures/{o.own_procedure}", {}, {"x": 1}, ["settings.manage", "procedures.manage"],
                             None),
        "follow-ups add": ("POST", f"/api/v1/appointments/{v}/follow-ups", {"items": [{"label": "a", "days": 7}]},
                           {"items": [{"label": "b", "days": 7}]}, ["procedures.record"], None),
        "follow-up due": ("POST", f"/api/v1/follow-ups/{o.follow}/due", {"days": 21}, {"days": 30},
                          ["procedures.record"], None),
        "follow-up drop": ("POST", f"/api/v1/follow-ups/{o.follow2}/drop", {}, {"x": 1}, ["procedures.record"], None),
        "follow-ups snooze": ("POST", "/api/v1/follow-ups/snooze", {"item_ids": [o.follow]}, {"item_ids": [o.follow2]},
                              ["appointments.book"], None),
        "follow-ups booked": ("POST", "/api/v1/follow-ups/booked", {"item_ids": [o.follow], "appointment_id": v},
                              {"item_ids": [o.follow2], "appointment_id": v}, ["appointments.book"], None),
    }


def reads(o):
    """name: (path, codes (any of them is enough), access)."""
    v, p = o.visit, o.patient
    return {
        "record": (f"/api/v1/appointments/{v}/record", ["clinical.view"], "note"),
        "note": (f"/api/v1/appointments/{v}/note", ["clinical.view"], "note"),
        "vitals": (f"/api/v1/appointments/{v}/vitals", ["visits.vitals", "clinical.view"], "note"),
        "timeline": (f"/api/v1/patients/{p}/timeline", ["clinical.view"], "history"),
        "balance": (f"/api/v1/patients/{p}/balance", ["patients.view"], "history"),
        "attachments": (f"/api/v1/patients/{p}/attachments", ["clinical.view"], "history"),
        "attachment": (f"/api/v1/attachments/{o.attachment}", ["clinical.view"], "attachment"),
        "prescriptions": (f"/api/v1/patients/{p}/prescriptions", ["clinical.view"], "history"),
        "allergy check": (f"/api/v1/patients/{p}/allergy-check?medicine={o.medicine}", ["prescriptions.write"],
                          "prescription"),
        "chart": (f"/api/v1/patients/{p}/teeth", ["clinical.view"], "history"),
        "tooth history": (f"/api/v1/patients/{p}/teeth/16/history", ["clinical.view"], "history"),
        "plans": (f"/api/v1/patients/{p}/plans", ["clinical.view"], "history"),
        "prescription": (f"/api/v1/prescriptions/{o.rx}", ["clinical.view"], "prescription"),
        "medicines": ("/api/v1/medicines", ["prescriptions.write", "settings.medicines", "clinical.view"], None),
        "templates": ("/api/v1/prescription-templates", ["prescriptions.write", "settings.medicines"], None),
        "procedures": ("/api/v1/procedures", ["procedures.record", "settings.manage", "clinical.view",
                                              "procedures.manage", "prices.edit"], None),
    }


def send(client, method, path, body, version=None, key=None):
    import json

    headers = {"X-Membership": str(client.membership_id), "Idempotency-Key": str(key or uuid.uuid4())}
    if version is not None:
        headers["If-Match"] = str(version)
    return client.generic(method, path, json.dumps(body), content_type="application/json", headers=headers)


def snapshot(fixture):
    with clinic_transaction(fixture.id, fixture.shard):
        return tuple(model.objects.filter(clinic_id=fixture.id).count() for model in (
            m.VisitNote, m.Vitals, m.VisitProcedure, m.Tooth, m.ToothHistory, m.Plan, m.PlanItem, m.Prescription,
            m.Medicine, m.PrescriptionTemplate, m.Procedure, m.Attachment)) + (
            list(m.PlanItem.objects.filter(clinic_id=fixture.id).order_by("id")
                 .values_list("status", "due_on", "snoozed_until", "appointment_id")),
            list(m.Medicine.objects.filter(clinic_id=fixture.id).order_by("id").values_list("is_active", "default_note")),
            list(m.Procedure.objects.filter(clinic_id=fixture.id).order_by("id").values_list("is_active", "price")),
            list(m.PrescriptionTemplate.objects.filter(clinic_id=fixture.id).order_by("id").values_list("version", "is_active")),
            list(m.Vitals.objects.filter(clinic_id=fixture.id).values_list("pulse", flat=True)),
            list(m.VisitNote.objects.filter(clinic_id=fixture.id).values_list("complaint", flat=True)),
            list(m.Tooth.objects.filter(clinic_id=fixture.id).values_list("surfaces", flat=True)),
        )


@override_settings(ATTACHMENTS_ROOT=tempfile.mkdtemp(prefix="hamada-attachments-"))
class ClinicalEndpointTests(M2TestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.ATTACHMENTS_ROOT, ignore_errors=True)
        super().tearDownClass()

    def test_the_lists_cover_every_clinical_route(self):
        o = objects(self, self.a)
        shape = lambda path: "/".join("<id>" if part.isdigit() else part for part in path.split("?")[0].strip("/").split("/"))
        listed = {(method, shape(path)) for method, path, *_ in mutating(o).values()}
        listed |= {("GET", shape(path)) for path, *_ in reads(o).values()}
        listed.add(("POST", shape(f"/api/v1/patients/{o.patient}/attachments")))  # the upload, tested on its own
        routes = set()
        for pattern in get_resolver().url_patterns:
            if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") != "clinical.urls":
                continue
            for route in pattern.url_patterns:
                views = getattr(route.callback, "api_views", None) or {route.callback.api_meta["method"]: route.callback}
                for method in views:
                    routes.add((method, "/".join("<id>" if part.startswith("<") else part
                                                 for part in str(route.pattern).split("/"))))
        self.assertEqual(listed, routes)

    def test_t9_access_is_declared_on_the_clinical_reads_and_each_writes_one_event(self):
        o = objects(self, self.a)
        declared = {}
        for pattern in get_resolver().url_patterns:
            if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") != "clinical.urls":
                continue
            for route in pattern.url_patterns:
                for view in (getattr(route.callback, "api_views", None) or {"": route.callback}).values():
                    if view.api_meta["access"]:
                        declared[str(route.pattern).replace("api/v1/", "")] = view.api_meta["access"]
        self.assertEqual(len(declared), sum(1 for *_, access in reads(o).values() if access))
        for name, (path, _codes, access) in reads(o).items():
            with clinic_transaction(self.a.id, self.a.shard):
                before = AuditAccess.objects.filter(clinic_id=self.a.id).count()
            r = self.owner.get(path, headers={"X-Membership": str(self.owner.membership_id)})
            self.assertEqual(r.status_code, 200, (name, getattr(r, "content", b"")))
            with clinic_transaction(self.a.id, self.a.shard):
                self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id).count() - before, 1 if access else 0, name)
        for name, (method, path, body, _other, _codes, version) in mutating(objects(self, self.a)).items():
            r = send(self.owner, method, path, body, version)
            for field in ("allergies", "chronic_conditions", "birth_date", "address"):
                self.assertNotIn(f'"{field}"', r.content.decode(), (name, field))

    def test_t1_another_clinics_ids_are_404_and_write_nothing(self):
        mine, theirs = objects(self, self.a), objects(self, self.b)
        before_a, before_b = snapshot(self.a), snapshot(self.b)
        self.set_codes("doctor", granted=EXTRA)
        for name, (method, path, body, _other, _codes, version) in mutating(theirs).items():
            if not any(part.isdigit() for part in path.split("/")):
                continue
            r = send(self.doc, method, path, body, version)
            self.assertEqual(r.status_code, 404, (name, r.content))
        for name, (path, _codes, _access) in reads(theirs).items():
            if any(part.isdigit() for part in path.split("?")[0].split("/")):
                self.assertEqual(call(self.owner, "GET", path).status_code, 404, name)
        bodies = [
            ("POST", f"/api/v1/appointments/{mine.visit}/procedures", {"procedure_id": theirs.filling, "tooth": 36}),
            ("POST", f"/api/v1/appointments/{mine.visit}/procedures", {"procedure_id": mine.crown, "tooth": 11,
                                                                       "plan_item_id": theirs.item}),
            ("POST", "/api/v1/prescriptions", {"patient_id": theirs.patient, "items": [{"name": "x"}], "section_id": mine.section}),
            ("POST", "/api/v1/prescriptions", {"patient_id": mine.patient, "appointment_id": theirs.visit, "items": [{"name": "x"}]}),
            ("POST", "/api/v1/prescriptions", {"patient_id": mine.patient, "appointment_id": mine.visit,
                                               "items": [{"medicine_id": theirs.medicine}]}),
            ("POST", f"/api/v1/patients/{mine.patient}/plans", {"items": [{"procedure_id": theirs.crown, "tooth": 11}]}),
            ("POST", "/api/v1/medicines", {"name": "x", "section_id": theirs.section}),
            ("POST", "/api/v1/procedures", {"name_ar": "x", "section_id": theirs.section}),
            ("PUT", f"/api/v1/patients/{mine.patient}/teeth/11", {"condition": "caries", "appointment_id": theirs.visit}),
        ]
        for method, path, body in bodies:
            self.assertEqual(send(self.doc, method, path, body).status_code, 404, (path, body))
        r = upload(self.desk, f"/api/v1/patients/{mine.patient}/attachments", PNG, appointment_id=str(theirs.visit))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(snapshot(self.b), before_b)
        self.assertEqual(snapshot(self.a), before_a)

    def test_t2_each_endpoint_needs_its_codes(self):
        for name, (method, path, body, _other, codes, version) in mutating(objects(self, self.a)).items():
            self.set_codes("doctor", granted=[c for c in EXTRA if c not in codes], revoked=codes)
            self.assertEqual(send(self.doc, method, path, body, version).status_code, 403, name)
        for name, (path, codes, _access) in reads(objects(self, self.a)).items():
            self.set_codes("doctor", granted=[c for c in EXTRA if c not in codes], revoked=codes)
            self.assertEqual(call(self.doc, "GET", path).status_code, 403, name)
        self.set_codes("doctor", granted=EXTRA)
        for name, (method, path, body, _other, _codes, version) in mutating(objects(self, self.a)).items():
            r = send(self.doc, method, path, body, version)
            self.assertIn(r.status_code, (200, 201), (name, r.content))

    def test_t3_repeats_replay_and_a_reused_key_with_another_body_is_refused(self):
        self.set_codes("doctor", granted=EXTRA)
        for name in mutating(objects(self, self.a)):
            method, path, body, other, _codes, version = mutating(objects(self, self.a))[name]
            key = uuid.uuid4()
            start = snapshot(self.a)
            first = send(self.doc, method, path, body, version, key=key)
            self.assertIn(first.status_code, (200, 201), (name, first.content))
            once = snapshot(self.a)
            self.assertNotEqual(once, start, f"{name} had no effect")
            again = send(self.doc, method, path, body, version, key=key)
            self.assertEqual((again.status_code, again.json()), (first.status_code, first.json()), name)
            self.assertEqual(snapshot(self.a), once, f"{name} took effect twice")
            reused = send(self.doc, method, path, other, version, key=key)
            self.assertEqual((reused.status_code, reused.json()["error"]["code"]), (422, "request_key_reused"), name)

    def test_signed_out_and_platform_admins_get_nothing(self):
        from django.test import Client

        o = objects(self, self.a)
        admin = self.user("platform_admin", is_platform_admin=True)
        client = Client()
        client.force_login(admin)
        client.membership_id = 0
        for name, (method, path, body, _other, _codes, version) in mutating(o).items():
            self.assertIn(send(client, method, path, body, version).status_code, (403, 404, 409), name)
            anonymous = Client()
            anonymous.membership_id = 0
            self.assertEqual(send(anonymous, method, path, body, version).status_code, 401, name)
