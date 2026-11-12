"""Test helpers for M2: visits in the room, the clinic's catalogue, and query counts on the shard."""

import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test.utils import CaptureQueriesContext

from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import M1TestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class M2TestCase(M1TestCase):
    """M1's clinics A (shard 1) and B (shard 2), plus the doctor's client and the copied dental catalogue."""

    def setUp(self):
        super().setUp()
        self.doc = self.a.client("doctor")

    def in_room(self, patient_id=None, service="procedure", client=None, fixture=None):
        """A visit called in, on a procedure service so a repeat visit is never a free follow-up."""
        fixture = fixture or self.a
        services = self.services_a if fixture is self.a else self.services_b
        body = {"service_id": services[service].id}
        if patient_id is not None:
            body["patient_id"] = patient_id
        desk = self.desk if fixture is self.a else fixture.client("assistant")
        visit = self.book(client=desk, fixture=fixture, **body).json()["appointment"]
        self.assertEqual(self.act(visit["id"], "arrive", "booked", client=desk).status_code, 200)
        self.assertEqual(self.act(visit["id"], "call", "waiting", client=desk).status_code, 200)
        return visit

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
