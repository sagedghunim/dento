"""Test helpers for M2: visits in the room, the clinic's catalogue, and query counts on the shard."""

import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test.utils import CaptureQueriesContext

from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import M1TestCase, add_schedule

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class M2TestCase(M1TestCase):
    """M1's clinics A (shard 1) and B (shard 2), plus the doctor's client and the copied dental catalogue.

    B's doctor works a queue too, so each place's doctor has visits of their own: a doctor never sees or acts on
    another doctor's patients (Saged, 2026-11-13)."""

    def setUp(self):
        super().setUp()
        self.doc = self.a.client("doctor")
        add_schedule(self.b, "doctor")

    def in_room(self, patient_id=None, service="procedure", client=None, fixture=None):
        """The doctor's visit, called in, on a procedure service so a repeat visit is never a free follow-up.

        Booked by the desk as a walk-in (the next turn with no time, already arrived), so it goes to the doctor's
        own queue although A's doctor works slots.
        """
        fixture = fixture or self.a
        services = self.services_a if fixture is self.a else self.services_b
        body = {"service_id": services[service].id, "doctor_id": fixture.membership("doctor").id}
        if patient_id is not None:
            body["patient_id"] = patient_id
        else:
            body["new_patient"] = {"full_name": "Mohamed Ahmed", "phone": "01012345678", "sex": "m", "age": 30}
        desk = self.desk if fixture is self.a else fixture.client("assistant")
        r = call(desk, "POST", "/api/v1/appointments/walk-in", body)
        self.assertEqual(r.status_code, 201, r.content)
        visit = r.json()["appointment"]
        r = self.act(visit["id"], "call", "waiting", client=desk)
        if r.status_code == 409 and r.json()["error"]["code"] == "doctor_busy":
            # One patient in the room per doctor (Saged, 2026-11-13): the earlier one is finished first.
            busy = r.json()["error"]["appointment_id"]
            self.assertEqual(self.act(busy, "finish", "in_room", client=desk).status_code, 200)
            r = self.act(visit["id"], "call", "waiting", client=desk)
        self.assertEqual(r.status_code, 200, r.content)
        return visit

    def booked(self, hhmm="10:00"):
        """A booked (not arrived) visit of A's doctor, at one of their slots today."""
        from scheduling.testing import today

        return self.book(doctor_id=self.a.membership("doctor").id,
                         starts_at=self.slot_iso(self.a, today(self.a), hhmm)).json()["appointment"]

    def owed_visit(self, patient_id=None, fixture=None):
        """A finished visit with nothing paid: its invoice stays open with the whole fee owed."""
        visit = self.in_room(patient_id, fixture=fixture)
        desk = self.desk if (fixture or self.a) is self.a else fixture.client("assistant")
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=desk).status_code, 200)
        return visit

    def catalogue(self, client=None):
        return call(client or self.doc, "GET", "/api/v1/procedures").json()["items"]

    def procedure(self, name_en, client=None):
        return next(p for p in self.catalogue(client) if p["name_en"] == name_en)

    def set_codes(self, role, granted=(), revoked=(), fixture=None):
        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            Membership.objects.filter(clinic_id=fixture.id, id=fixture.membership(role).id).update(
                granted=list(granted), revoked=list(revoked))

    def queries(self, fn):
        """Statements the request ran on the shard (the test's SAVEPOINTs aren't work)."""
        with CaptureQueriesContext(connections[self.a.shard]) as shard:
            fn()
        return len([q for q in shard.captured_queries if "SAVEPOINT" not in q["sql"]])


def put(client, url, body, version):
    """A PUT with If-Match, as the visit screen sends it (design 5.6)."""
    headers = {"X-Membership": str(client.membership_id), "Idempotency-Key": str(uuid.uuid4())}
    if version is not None:
        headers["If-Match"] = str(version)
    import json

    return client.generic("PUT", url, json.dumps(body), content_type="application/json", headers=headers)


def upload(client, url, content, name="scan.png", content_type="image/png", key=None, **fields):
    headers = {"X-Membership": str(client.membership_id), "Idempotency-Key": str(key or uuid.uuid4())}
    return client.post(url, {"file": SimpleUploadedFile(name, content, content_type=content_type), **fields},
                       headers=headers)
