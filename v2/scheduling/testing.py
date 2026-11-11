"""Test helpers for M1: schedules, booking through the API, frozen clocks and summary reads."""

import contextlib
import datetime as dt
from unittest import mock

from clinics.models import Membership, Schedule, Section
from core.days import business_day
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call


def sections_of(fixture):
    """The place's sections in order (a clinic has one)."""
    with clinic_transaction(fixture.id, fixture.shard):
        return list(Section.objects.filter(clinic_id=fixture.id).order_by("sort", "id"))


def add_schedule(fixture, role, *, starts="09:00", ends="23:00", mode="queue", slot_minutes=None, max_bookings=None,
                 weekdays=range(7), section=None):
    """Sessions for a member on these weekdays, in `section` (an id; default: the place's first section)."""
    with clinic_transaction(fixture.id, fixture.shard):
        doctor = Membership.objects.get(clinic_id=fixture.id, id=fixture.membership(role).id)
        if section is None:
            section = Section.objects.filter(clinic_id=fixture.id).order_by("sort", "id").values_list("id", flat=True)[0]
        for weekday in weekdays:
            Schedule.objects.create(clinic_id=fixture.id, doctor=doctor, section_id=section, weekday=weekday,
                                    starts=dt.time.fromisoformat(starts), ends=dt.time.fromisoformat(ends), mode=mode,
                                    slot_minutes=slot_minutes, max_bookings=max_bookings)


def services_of(fixture):
    from clinics.models import Service

    with clinic_transaction(fixture.id, fixture.shard):
        found = {}
        for service in Service.objects.filter(clinic_id=fixture.id).order_by("sort", "id"):
            found.setdefault(service.kind, service)  # the first consultation, follow-up and procedure
        return found


def today(fixture, instant=None):
    from clinics.models import Clinic

    with clinic_transaction(fixture.id, fixture.shard):
        return business_day(Clinic.objects.get(id=fixture.id), instant)


@contextlib.contextmanager
def frozen(instant):
    """timezone.now() returns `instant` (an aware datetime) everywhere the code asks for it."""
    with mock.patch("django.utils.timezone.now", return_value=instant):
        yield


def summaries(fixture, day):
    from summaries.models import ClinicDay, DoctorDay

    with clinic_transaction(fixture.id, fixture.shard):
        clinic = ClinicDay.objects.filter(clinic_id=fixture.id, day=day).values(
            "version", "done", "collected", "refunded", "changed_since_recompute").first()
        doctors = {r["doctor_id"]: r for r in DoctorDay.objects.filter(clinic_id=fixture.id, day=day).values(
            "doctor_id", "last_queue_no", "booked", "done", "collected", "refunded")}
    return clinic, doctors


class M1TestCase(ShardTestCase):
    """Clinic A on shard 1 and clinic B on shard 2. The owners work a queue; A's doctor works 20-minute slots."""

    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")
        self.b = self.clinic("b", "shard_2")
        add_schedule(self.a, "owner")
        add_schedule(self.a, "doctor", starts="10:00", ends="14:00", mode="slots", slot_minutes=20)
        add_schedule(self.b, "owner")
        self.services_a = services_of(self.a)
        self.services_b = services_of(self.b)
        self.desk = self.a.client("assistant")
        self.owner = self.a.client("owner")

    def book(self, client=None, fixture=None, expect=201, **body):
        fixture = fixture or self.a
        services = self.services_a if fixture is self.a else self.services_b
        body.setdefault("doctor_id", fixture.membership("owner").id)
        body.setdefault("service_id", services["consultation"].id)
        if "patient_id" not in body and "new_patient" not in body:
            body["new_patient"] = {"full_name": "Mohamed Ahmed", "phone": "01012345678", "sex": "m", "age": 30}
        r = call(client or (self.desk if fixture is self.a else fixture.client("assistant")), "POST",
                 "/api/v1/appointments", body)
        if expect is not None:
            self.assertEqual(r.status_code, expect, r.content)
        return r

    def act(self, appointment_id, action, status, client=None, **body):
        return call(client or self.desk, "POST", f"/api/v1/appointments/{appointment_id}/{action}",
                    {"status": status, **body})

    def appointment(self, appointment_id, fixture=None):
        from scheduling.models import Appointment

        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            return Appointment.objects.get(clinic_id=fixture.id, id=appointment_id)

    def slot_iso(self, fixture, day, hhmm):
        """A slot start as the API shows it: the clinic's local time with its offset."""
        from zoneinfo import ZoneInfo

        return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=ZoneInfo("Africa/Cairo")).isoformat()
