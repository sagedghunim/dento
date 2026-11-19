"""T1, T2, T3 and T9 over every M1 endpoint, with the endpoint list checked against the URL configuration."""

import datetime as dt
import uuid
from types import SimpleNamespace

from django.urls import get_resolver

from audit.models import AuditAccess, AuditChange
from billing.models import Invoice, InvoiceLine, Payment
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call, new_patient
from patients.models import Patient
from scheduling.models import Appointment
from scheduling.testing import M1TestCase, free_rooms, today
from summaries.models import ClinicDay

FULL = ["billing.discount", "billing.refund"]  # what the assistant template lacks among the M1 codes


def objects(test, fixture):
    """A booked, a waiting and an in-room visit, a paid visit with a list line, and an unpaid invoice."""
    client = fixture.client("assistant")
    services = test.services_a if fixture is test.a else test.services_b
    owner = fixture.membership("owner").id

    def book(**body):
        r = call(client, "POST", "/api/v1/appointments", {"doctor_id": owner, "service_id": services["consultation"].id,
                                                          "new_patient": new_patient(full_name="Matrix Patient"), **body})
        assert r.status_code == 201, r.content
        return r.json()["appointment"]

    free_rooms(fixture)  # a doctor has one patient in the room at a time (Saged, 2026-11-13)
    booked, waiting, in_room = book(), book(arrive=True), book(arrive=True)
    call(client, "POST", f"/api/v1/appointments/{in_room['id']}/call", {"status": "waiting"})
    # A waiting visit on another day: the owner's room is taken today, so "call" is tried on this one.
    callable_ = book(day=(today(fixture) + dt.timedelta(days=1)).isoformat())
    with clinic_transaction(fixture.id, fixture.shard):
        Appointment.objects.filter(clinic_id=fixture.id, id=callable_["id"]).update(
            status="waiting", arrived_at=dt.datetime.now(dt.UTC))
    assisted, readied = book(arrive=True), book(arrive=True)
    call(client, "POST", f"/api/v1/appointments/{assisted['id']}/assist", {"status": "waiting"})
    call(client, "POST", f"/api/v1/appointments/{readied['id']}/ready", {"status": "waiting"})
    paid = book(payment={"amount": 1000, "method": "cash"})
    invoice = call(client, "GET", f"/api/v1/invoices/{paid['invoice']['id']}").json()["invoice"]
    line = call(client, "POST", f"/api/v1/invoices/{invoice['id']}/lines",
                {"kind": "procedure", "service_id": services["procedure"].id}).json()["invoice"]["lines"][-1]
    unpaid = book()
    no_show = book()
    call(client, "POST", f"/api/v1/appointments/{no_show['id']}/no-show", {"status": "booked"})
    unpaid_invoice = call(client, "POST", f"/api/v1/appointments/{unpaid['id']}/invoice").json()["invoice"]
    return SimpleNamespace(booked=booked["id"], waiting=waiting["id"], in_room=in_room["id"], paid=paid["id"],
                           callable=callable_["id"],
                           assisted=assisted["id"], readied=readied["id"], no_show=no_show["id"],
                           invoice=invoice["id"], line=line["id"], payment=invoice["payments"][0]["id"],
                           unpaid_invoice=unpaid_invoice["id"], patient=booked["patient"]["id"], owner=owner,
                           # a patient with no open visit today: Book and Walk-in may take them (Saged, 2026-11-17)
                           free_patient=callable_["patient"]["id"],
                           doctor=fixture.membership("doctor").id, procedure=services["procedure"].id,
                           consultation=services["consultation"].id)


def tomorrow(test):
    return (today(test.a) + dt.timedelta(days=1)).isoformat()


# name: (method, path, body, alternative body for the reused-key check, codes, pattern in config)
def mutating(test, o):
    return {
        "create patient": ("POST", "/api/v1/patients", new_patient("New Person"), new_patient("Other Person"),
                           ["patients.edit"]),
        "edit patient": ("PATCH", f"/api/v1/patients/{o.patient}", {"changes": {"notes": "n1"}, "expected": {"notes": ""}},
                         {"changes": {"notes": "n2"}, "expected": {"notes": ""}}, ["patients.edit"]),
        "book": ("POST", "/api/v1/appointments", {"doctor_id": o.owner, "service_id": o.consultation, "patient_id": o.free_patient},
                 {"doctor_id": o.owner, "service_id": o.consultation, "patient_id": o.free_patient, "note": "x"},
                 ["appointments.book"]),
        "walk in": ("POST", "/api/v1/appointments/walk-in", {"patient_id": o.free_patient},
                    {"patient_id": o.free_patient, "note": "x"}, ["appointments.book", "appointments.queue"]),
        "arrive": ("POST", f"/api/v1/appointments/{o.booked}/arrive", {"status": "booked"},
                   {"status": "booked", "note": 1}, ["appointments.queue"]),
        "call": ("POST", f"/api/v1/appointments/{o.callable}/call", {"status": "waiting"},
                 {"status": "waiting", "note": 1}, ["appointments.queue"]),
        "undo arrive": ("POST", f"/api/v1/appointments/{o.waiting}/undo-arrive", {"status": "waiting"},
                        {"status": "waiting", "note": 1}, ["appointments.queue"]),
        "undo call": ("POST", f"/api/v1/appointments/{o.in_room}/undo-call", {"status": "in_room"},
                      {"status": "in_room", "note": 1}, ["appointments.queue"]),
        "assist": ("POST", f"/api/v1/appointments/{o.waiting}/assist", {"status": "waiting"},
                   {"status": "waiting", "note": 1}, ["appointments.queue"]),
        "ready": ("POST", f"/api/v1/appointments/{o.waiting}/ready", {"status": "waiting"},
                  {"status": "waiting", "note": 1}, ["appointments.queue"]),
        "undo assist": ("POST", f"/api/v1/appointments/{o.assisted}/undo-assist", {"status": "with_assistant"},
                        {"status": "with_assistant", "note": 1}, ["appointments.queue"]),
        "undo ready": ("POST", f"/api/v1/appointments/{o.readied}/undo-ready", {"status": "ready"},
                       {"status": "ready", "note": 1}, ["appointments.queue"]),
        "no show": ("POST", f"/api/v1/appointments/{o.booked}/no-show", {"status": "booked"},
                    {"status": "booked", "note": 1}, ["appointments.queue"]),
        "undo no show": ("POST", f"/api/v1/appointments/{o.no_show}/undo-no-show", {"status": "no_show"},
                         {"status": "no_show", "note": 1}, ["appointments.queue"]),
        "finish": ("POST", f"/api/v1/appointments/{o.in_room}/finish", {"status": "in_room"},
                   {"status": "in_room", "note": 1}, ["appointments.queue"]),
        "cancel": ("POST", f"/api/v1/appointments/{o.booked}/cancel", {"status": "booked"},
                   {"status": "booked", "reason": "x"}, ["appointments.book"]),
        "reschedule": ("POST", f"/api/v1/appointments/{o.booked}/reschedule",
                       {"status": "booked", "doctor_id": o.owner, "day": tomorrow(test)},
                       {"status": "booked", "doctor_id": o.owner, "day": tomorrow(test), "extra": False},
                       ["appointments.book"]),
        "pay": ("POST", f"/api/v1/appointments/{o.booked}/payments", {"amount": 100, "method": "cash"},
                {"amount": 101, "method": "cash"}, ["billing.collect"]),
        "visit invoice": ("POST", f"/api/v1/appointments/{o.booked}/invoice", {}, {"x": 1}, ["billing.collect"]),
        "add line": ("POST", f"/api/v1/invoices/{o.invoice}/lines", {"kind": "procedure", "service_id": o.procedure},
                     {"kind": "procedure", "service_id": o.procedure, "qty": 2}, ["billing.collect"]),
        "remove line": ("DELETE", f"/api/v1/invoices/{o.invoice}/lines/{o.line}", {}, {"x": 1}, ["billing.collect"]),
        "discount": ("PUT", f"/api/v1/invoices/{o.invoice}/discount", {"discount": 100, "expected_discount": 0},
                     {"discount": 200, "expected_discount": 0}, ["billing.discount"]),
        "void": ("POST", f"/api/v1/invoices/{o.unpaid_invoice}/void", {}, {"x": 1}, ["billing.refund"]),
        "refund": ("POST", f"/api/v1/payments/{o.payment}/refund", {"amount": 50}, {"amount": 60}, ["billing.refund"]),
    }


def reads(test, o):
    day = today(test.a).isoformat()
    return {
        "search": ("GET", "/api/v1/patients?q=matrix", ["patients.view"], None),
        "by phone": ("GET", "/api/v1/patients/by-phone?phone=01012345678", ["patients.view"], None),
        "file": ("GET", f"/api/v1/patients/{o.patient}", ["patients.view"], "open"),
        "visits": ("GET", f"/api/v1/patients/{o.patient}/visits", ["patients.view"], "history"),
        "payments": ("GET", f"/api/v1/patients/{o.patient}/payments", ["patients.view"], "history"),
        "today": ("GET", "/api/v1/today", ["appointments.book", "appointments.queue"], None),
        "quote": ("GET", f"/api/v1/appointments/quote?doctor={o.owner}&service={o.consultation}&patient={o.patient}"
                         f"&day={day}", ["appointments.book"], None),
        "appointment": ("GET", f"/api/v1/appointments/{o.booked}", ["appointments.book", "appointments.queue"], None),
        "slots": ("GET", f"/api/v1/doctors/{o.doctor}/slots?day={day}", ["appointments.book"], None),
        "month": ("GET", f"/api/v1/doctors/{o.doctor}/month?month={day[:7]}", ["appointments.book"], None),
        "invoice": ("GET", f"/api/v1/invoices/{o.invoice}", ["billing.collect"], "invoice"),
    }


def snapshot(fixture):
    with clinic_transaction(fixture.id, fixture.shard):
        return (
            Patient.objects.filter(clinic_id=fixture.id).count(),
            list(Appointment.objects.filter(clinic_id=fixture.id).order_by("id").values_list(
                "status", "day", "queue_no", "fee")),
            list(Invoice.objects.filter(clinic_id=fixture.id).order_by("id").values_list("status", "total", "paid", "discount")),
            InvoiceLine.objects.filter(clinic_id=fixture.id).count(),
            list(Payment.objects.filter(clinic_id=fixture.id).order_by("id").values_list("amount", "refunded")),
            list(ClinicDay.objects.filter(clinic_id=fixture.id).order_by("day").values_list("day", "version")),
            AuditChange.objects.filter(clinic_id=fixture.id).count(),
            list(Patient.objects.filter(clinic_id=fixture.id).order_by("id").values_list("notes", flat=True)),
        )


class EndpointMatrixTests(M1TestCase):
    def set_codes(self, granted=(), revoked=()):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                granted=list(granted), revoked=list(revoked))

    def test_the_lists_cover_every_m1_route(self):
        """Every route of the new apps is in one of the lists below (so T1-T3 and T9 can't miss one)."""
        o = objects(self, self.a)
        listed = {(m, p.split("?")[0]) for m, p, *_ in mutating(self, o).values()}
        listed |= {(m, p.split("?")[0]) for m, p, *_ in reads(self, o).values()}
        routes = set()
        for pattern in get_resolver().url_patterns:
            module = getattr(getattr(pattern, "urlconf_module", None), "__name__", "")
            if module not in ("patients.urls", "scheduling.urls", "billing.urls"):
                continue
            for route in pattern.url_patterns:
                views = getattr(route.callback, "api_views", None) or {route.callback.api_meta["method"]: route.callback}
                for method in views:
                    routes.add((method, str(route.pattern)))
        def shape(path):
            parts = path.strip("/").split("/")
            return "/".join("<id>" if part.isdigit() else part for part in parts)

        normalised = {(m, shape(p)) for m, p in listed}
        expected = {(m, "/".join("<id>" if part.startswith("<") else part for part in p.split("/"))) for m, p in routes}
        self.assertEqual(normalised, expected)

    def test_t9_access_is_declared_exactly_on_the_views_that_read_files(self):
        declared = {}
        for pattern in get_resolver().url_patterns:
            if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") == "clinical.urls":
                continue  # M2's routes: clinical.test_endpoints checks them the same way
            for route in getattr(pattern, "url_patterns", []):
                views = getattr(route.callback, "api_views", None) or {"": route.callback}
                for view in views.values():
                    meta = getattr(view, "api_meta", None)
                    if meta and meta["access"]:
                        declared[str(route.pattern)] = meta["access"]
        self.assertEqual(declared, {
            "api/v1/patients/<int:patient_id>": "open", "api/v1/patients/<int:patient_id>/visits": "history",
            "api/v1/patients/<int:patient_id>/payments": "history", "api/v1/invoices/<int:invoice_id>": "invoice",
            "api/v1/patients/<int:patient_id>/packages": "history",  # M3 (F7); billing.test_packages reads it
            "api/v1/patients/<int:patient_id>/installments": "invoice"})  # billing.test_installments reads it
        o = objects(self, self.a)
        for name, (method, path, codes, access) in reads(self, o).items():
            with clinic_transaction(self.a.id, self.a.shard):
                before = AuditAccess.objects.filter(clinic_id=self.a.id).count()
            r = call(self.owner, method, path)
            self.assertEqual(r.status_code, 200, (name, r.content))
            with clinic_transaction(self.a.id, self.a.shard):
                after = AuditAccess.objects.filter(clinic_id=self.a.id).count()
            self.assertEqual(after - before, 1 if access else 0, name)
            if not access:
                for field in ("allergies", "chronic_conditions", "birth_date", "address"):
                    self.assertNotIn(f'"{field}"', r.content.decode(), (name, field))
        for name, (method, path, body, _, _) in mutating(self, objects(self, self.a)).items():
            r = call(self.owner, method, path, body)
            for field in ("allergies", "chronic_conditions", "birth_date", "address"):
                self.assertNotIn(f'"{field}"', r.content.decode(), (name, field))

    def test_t1_another_clinics_ids_in_paths_and_bodies_are_404_and_write_nothing(self):
        mine, theirs = objects(self, self.a), objects(self, self.b)
        before_a, before_b = snapshot(self.a), snapshot(self.b)
        self.set_codes(granted=FULL)
        for name, (method, path, body, _, _) in mutating(self, theirs).items():
            if "/" not in path.replace("/api/v1/", "", 1):
                continue  # no id in the path: the body cases follow
            r = call(self.desk, method, path, body)
            self.assertEqual(r.status_code, 404, (name, r.content))
        for name, (method, path, _, _) in reads(self, theirs).items():
            if any(part.isdigit() for part in path.split("?")[0].split("/")):
                self.assertEqual(call(self.desk, method, path).status_code, 404, name)
        bodies = [
            ("POST", "/api/v1/appointments", {"doctor_id": theirs.owner, "service_id": mine.consultation, "patient_id": mine.patient}),
            ("POST", "/api/v1/appointments", {"doctor_id": mine.owner, "service_id": theirs.consultation, "patient_id": mine.patient}),
            ("POST", "/api/v1/appointments", {"doctor_id": mine.owner, "service_id": mine.consultation, "patient_id": theirs.patient}),
            ("POST", f"/api/v1/appointments/{mine.booked}/reschedule", {"status": "booked", "doctor_id": theirs.owner,
                                                                       "day": tomorrow(self)}),
            ("POST", f"/api/v1/invoices/{mine.invoice}/lines", {"kind": "procedure", "service_id": theirs.procedure}),
            ("GET", f"/api/v1/appointments/quote?doctor={theirs.owner}&service={mine.consultation}", None),
            ("GET", f"/api/v1/appointments/quote?doctor={mine.owner}&service={theirs.consultation}", None),
            ("GET", f"/api/v1/appointments/quote?doctor={mine.owner}&service={mine.consultation}&patient={theirs.patient}", None),
        ]
        for method, path, body in bodies:
            self.assertEqual(call(self.desk, method, path, body).status_code, 404, (path, body))
        self.assertEqual(snapshot(self.b), before_b)
        after_a = snapshot(self.a)
        self.assertEqual(after_a[1:7], before_a[1:7])  # nothing changed in A either (reads add only access events)

    def test_t2_each_endpoint_needs_its_codes(self):
        for name, (method, path, body, _, codes) in mutating(self, objects(self, self.a)).items():
            self.set_codes(granted=[c for c in FULL if c not in codes], revoked=codes)
            self.assertEqual(call(self.desk, method, path, body).status_code, 403, name)
        for name, (method, path, codes, _) in reads(self, objects(self, self.a)).items():
            self.set_codes(granted=[c for c in FULL if c not in codes], revoked=codes)
            self.assertEqual(call(self.desk, method, path).status_code, 403, name)
        self.set_codes(granted=FULL)  # a single grant (for the codes the template lacks) gives 2xx
        for client in (self.desk, self.owner):  # and the owner always may
            for name in mutating(self, objects(self, self.a)):
                method, path, body, _, _ = mutating(self, objects(self, self.a))[name]
                r = call(client, method, path, body)
                self.assertIn(r.status_code, (200, 201), (name, r.content))

    def test_t2_a_platform_admin_or_a_signed_out_browser_gets_nothing(self):
        from django.test import Client

        o = objects(self, self.a)
        admin = self.user("platform_admin", is_platform_admin=True)
        client = Client()
        client.force_login(admin)
        for name, (method, path, body, _, _) in mutating(self, o).items():
            self.assertIn(call(client, method, path, body).status_code, (403, 404), name)
            self.assertEqual(call(Client(), method, path, body).status_code, 401, name)

    def test_t3_repeats_replay_and_a_reused_key_with_another_body_is_refused(self):
        self.set_codes(granted=FULL)
        for name in mutating(self, objects(self, self.a)):
            method, path, body, other, _ = mutating(self, objects(self, self.a))[name]
            key = uuid.uuid4()
            start = snapshot(self.a)
            first = call(self.desk, method, path, body, key=key)
            self.assertIn(first.status_code, (200, 201), (name, first.content))
            once = snapshot(self.a)
            self.assertNotEqual(once, start, f"{name} had no effect")
            again = call(self.desk, method, path, body, key=key)
            self.assertEqual((again.status_code, again.json()), (first.status_code, first.json()), name)
            self.assertEqual(again["Idempotency-Key"], str(key))
            self.assertEqual(snapshot(self.a), once, f"{name} took effect twice")
            reused = call(self.desk, method, path, other, key=key)
            self.assertEqual((reused.status_code, reused.json()["error"]["code"]), (422, "request_key_reused"), name)

    def test_t3_a_retry_after_a_409_runs_again(self):
        o = objects(self, self.a)
        key = uuid.uuid4()
        fee = self.services_a["consultation"].price
        body = {"amount": fee + 500, "method": "cash"}
        self.assertEqual(call(self.desk, "POST", f"/api/v1/appointments/{o.booked}/payments", body, key=key).status_code, 409)
        invoice = call(self.desk, "POST", f"/api/v1/appointments/{o.booked}/invoice").json()["invoice"]
        call(self.desk, "POST", f"/api/v1/invoices/{invoice['id']}/lines", {"kind": "procedure", "service_id": o.procedure})
        r = call(self.desk, "POST", f"/api/v1/appointments/{o.booked}/payments", body, key=key)
        self.assertEqual(r.status_code, 201, r.content)
