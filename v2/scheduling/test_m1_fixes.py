"""The chief judge's M1 decision, scheduling side: R2 (finish after finish), R4 (stranded and overlapping bookings), R6."""

import datetime as dt
import threading
from zoneinfo import ZoneInfo

from django.db import IntegrityError, connections, transaction
from django.test import TransactionTestCase

from billing.models import Invoice, Payment
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from scheduling.models import Appointment
from scheduling.testing import M1TestCase, add_schedule, services_of, today

CAIRO = ZoneInfo("Africa/Cairo")


def run_together(jobs):
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(index, job):
        try:
            barrier.wait()
            results[index] = job()
        except Exception as error:  # surfaced by the assertions
            results[index] = error
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def payments_of(fixture, appointment_id):
    with clinic_transaction(fixture.id, fixture.shard):
        invoice = Invoice.objects.filter(clinic_id=fixture.id, appointment_id=appointment_id, status="open").first()
        paid = invoice.paid if invoice else 0
        rows = Payment.objects.filter(clinic_id=fixture.id, invoice_id=invoice.id).count() if invoice else 0
    return paid, rows


class FinishAfterFinishTests(M1TestCase):
    """R2: a finish with a payment on a visit someone already finished is 409 already_finished, never a 200."""

    def in_room(self):
        appt = self.book(arrive=True).json()["appointment"]
        self.act(appt["id"], "call", "waiting")
        return appt

    def test_a_finish_with_a_payment_after_another_finish_is_409_and_records_nothing(self):
        appt = self.in_room()
        self.assertEqual(self.act(appt["id"], "finish", "in_room").status_code, 200)
        doctor = self.owner  # the visit's doctor
        r = self.act(appt["id"], "finish", "in_room", client=doctor, payment={"amount": 5000, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_finished"), r.content)
        current = r.json()["error"]["appointment"]
        self.assertEqual((current["id"], current["status"], current["invoice"]["paid"]), (appt["id"], "done", 0))
        self.assertEqual(payments_of(self.a, appt["id"]), (0, 0))
        # Without a payment, a second finish is still the harmless repeat it was.
        self.assertEqual(self.act(appt["id"], "finish", "in_room", client=doctor).status_code, 200)
        # The money goes through the payments endpoint instead.
        r = call(doctor, "POST", f"/api/v1/appointments/{appt['id']}/payments", {"amount": 5000, "method": "cash"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(payments_of(self.a, appt["id"]), (5000, 1))

    def test_a_finish_with_a_payment_on_a_visit_in_another_status_is_still_status_changed(self):
        appt = self.book(arrive=True).json()["appointment"]
        r = self.act(appt["id"], "finish", "in_room", payment={"amount": 100, "method": "cash"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "status_changed"))


class FinishRaceTests(TransactionTestCase):
    databases = "__all__"

    def test_two_desks_finish_at_once_and_the_cash_is_never_lost(self):
        a = ShardTestCase().clinic("fr", "shard_1")
        add_schedule(a, "owner")
        services = services_of(a)
        desk = a.client("assistant")
        for round_ in range(5):
            appt = call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": a.membership("owner").id, "service_id": services["consultation"].id, "arrive": True,
                "new_patient": {"full_name": f"Race {round_}"}}).json()["appointment"]
            call(desk, "POST", f"/api/v1/appointments/{appt['id']}/call", {"status": "waiting"})
            c1, c2 = a.client("assistant"), a.client("owner")
            plain, paying = run_together([
                lambda: call(c1, "POST", f"/api/v1/appointments/{appt['id']}/finish", {"status": "in_room"}),
                lambda: call(c2, "POST", f"/api/v1/appointments/{appt['id']}/finish",
                             {"status": "in_room", "payment": {"amount": 3000, "method": "cash"}}),
            ])
            paid, rows = payments_of(a, appt["id"])
            self.assertEqual(plain.status_code, 200)
            if paying.status_code == 200:
                self.assertEqual((paid, rows), (3000, 1))
            else:
                self.assertEqual((paying.status_code, paying.json()["error"]["code"]), (409, "already_finished"))
                self.assertEqual((paid, rows), (0, 0))


class OutOfRangeTests(M1TestCase):
    """R6: dates at the edges of the calendar are a 400, never a 500."""

    def test_boundaries(self):
        owner, consult = self.a.membership("owner").id, self.services_a["consultation"].id
        patient = self.book().json()["appointment"]["patient"]["id"]
        for starts_at in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
            r = self.book(expect=None, doctor_id=self.a.membership("doctor").id, starts_at=starts_at)
            self.assertEqual(r.status_code, 400, (starts_at, r.content))
        appt = self.book().json()["appointment"]
        r = call(self.desk, "POST", f"/api/v1/appointments/{appt['id']}/reschedule",
                 {"status": "booked", "doctor_id": self.a.membership("doctor").id, "day": appt["day"],
                  "starts_at": "0001-01-01T00:00:00+05:00"})
        self.assertEqual(r.status_code, 400, r.content)
        for day in ("0001-01-01", "0001-01-02", "9999-12-31", (today(self.a) - dt.timedelta(days=1)).isoformat()):
            r = call(self.desk, "GET", f"/api/v1/appointments/quote?doctor={owner}&service={consult}&day={day}"
                                       f"&patient={patient}")
            self.assertEqual(r.status_code, 400, (day, r.content))
            for path in (f"/api/v1/doctors/{owner}/slots?day={day}", f"/api/v1/today?day={day}"):
                self.assertIn(call(self.desk, "GET", path).status_code, (200, 400), (path, day))
        for cursor in ("0001-01-01.1", "9999-12-31.9223372036854775807", "9999-12-31.99999999999999999999",
                       "-62135596800000000.1", "253402300800000000.1", "99999999999999999999999.1"):
            for path in (f"/api/v1/patients/{patient}/visits?after={cursor}",
                         f"/api/v1/patients/{patient}/payments?after={cursor}"):
                self.assertIn(call(self.owner, "GET", path).status_code, (200, 400), (path, cursor))


class StrandRaceTests(TransactionTestCase):
    """R4: a booking (or a move) paused after it validated its slot, while a schedule change runs.

    The change must wait for the booking, see it and refuse. Before R4 both answered 2xx and the
    booking was left outside every session.
    """

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("sr", "shard_1")
        add_schedule(self.a, "owner")
        add_schedule(self.a, "doctor", starts="10:00", ends="14:00", mode="slots", slot_minutes=20)
        self.services = services_of(self.a)
        self.day = today(self.a) + dt.timedelta(days=1)
        self.owner = self.a.client("owner")

    def session_of(self, role):
        items = call(self.owner, "GET", f"/api/v1/schedules?doctor={self.a.membership(role).id}").json()["items"]
        return next(s for s in items if s["weekday"] == self.day.weekday())

    def slot(self, hhmm):
        return dt.datetime.combine(self.day, dt.time.fromisoformat(hhmm), tzinfo=CAIRO).isoformat()

    def paused(self, action, change):
        """Run `action` paused right after rules.resolve_slot, and `change` in that window."""
        from unittest import mock

        from scheduling import rules

        validated, go = threading.Event(), threading.Event()
        original = rules.resolve_slot

        def pausing(*args, **kwargs):
            found = original(*args, **kwargs)
            if threading.current_thread().name == "action":
                validated.set()
                go.wait(1.0)  # with R4 the change waits for us and can't release us: we go on after 1 s
            return found

        results = {}

        def run(name, job):
            try:
                results[name] = job()
            except Exception as error:
                results[name] = error
            finally:
                connections.close_all()

        def changing():
            validated.wait(5)
            try:
                return change()
            finally:
                go.set()

        with mock.patch.object(rules, "resolve_slot", pausing):
            threads = [threading.Thread(target=run, args=("action", action), name="action"),
                       threading.Thread(target=run, args=("change", changing), name="change")]
            [t.start() for t in threads]
            [t.join() for t in threads]
        return results["action"], results["change"]

    def assert_refused(self, change, booking_id):
        self.assertEqual((change.status_code, change.json()["error"]["code"]), (409, "schedule_conflict"), change.content)
        self.assertIn(booking_id, [b["id"] for b in change.json()["error"]["bookings"]])

    def test_a_session_deleted_while_a_booking_is_in_flight(self):
        session = self.session_of("owner")
        desk = self.a.client("assistant")
        booking, change = self.paused(
            lambda: call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": self.a.membership("owner").id, "service_id": self.services["consultation"].id,
                "day": self.day.isoformat(), "new_patient": {"full_name": "In Flight"}}),
            lambda: call(self.owner, "DELETE", f"/api/v1/schedules/{session['id']}"))
        self.assertEqual(booking.status_code, 201, booking.content)
        self.assert_refused(change, booking.json()["appointment"]["id"])

    def test_a_session_moved_while_a_slot_booking_is_in_flight(self):
        session = self.session_of("doctor")
        desk = self.a.client("assistant")
        booking, change = self.paused(
            lambda: call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": self.a.membership("doctor").id, "service_id": self.services["consultation"].id,
                "day": self.day.isoformat(), "starts_at": self.slot("11:00"), "new_patient": {"full_name": "In Flight"}}),
            lambda: call(self.owner, "PUT", f"/api/v1/schedules/{session['id']}",
                         {**session, "starts": "15:00", "ends": "19:00", "expected": session}))
        self.assertEqual(booking.status_code, 201, booking.content)
        self.assert_refused(change, booking.json()["appointment"]["id"])

    def test_a_session_moved_while_a_reschedule_is_in_flight(self):
        desk = self.a.client("assistant")
        appt = call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.a.membership("owner").id, "service_id": self.services["consultation"].id,
            "day": self.day.isoformat(), "new_patient": {"full_name": "Moving"}}).json()["appointment"]
        session = self.session_of("doctor")
        moved, change = self.paused(
            lambda: call(desk, "POST", f"/api/v1/appointments/{appt['id']}/reschedule", {
                "status": "booked", "doctor_id": self.a.membership("doctor").id, "day": self.day.isoformat(),
                "starts_at": self.slot("11:00")}),
            lambda: call(self.owner, "PUT", f"/api/v1/schedules/{session['id']}",
                         {**session, "starts": "15:00", "ends": "19:00", "expected": session}))
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assert_refused(change, appt["id"])


class StrandedCheckTests(M1TestCase):
    def setUp(self):
        super().setUp()
        self.doctor = self.a.membership("doctor").id
        self.day = today(self.a) + dt.timedelta(days=1)

    def session(self):
        items = call(self.owner, "GET", f"/api/v1/schedules?doctor={self.doctor}").json()["items"]
        return next(s for s in items if s["weekday"] == self.day.weekday())

    def test_every_booking_is_checked_not_only_the_first_500(self):
        from clinics.models import Clinic
        from core import sql
        from scheduling.jobs import system_ctx

        add_schedule(self.a, "doctor", starts="15:00", ends="23:00", weekdays=[self.day.weekday()])
        patient = self.book().json()["appointment"]["patient"]["id"]
        consult = self.services_a["consultation"]
        with clinic_transaction(self.a.id, self.a.shard):
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            sql.run(ctx, """INSERT INTO "scheduling_appointment" (clinic_id, patient_id, doctor_id, service_id, section_id,
                            day, queue_no, is_extra, status, source, fee, is_follow_up, is_new, auto_closed, note,
                            cancel_reason, created_at, updated_at)
                            SELECT %s, %s, %s, %s, %s, %s, g, false, 'booked', 'clinic', 100, false, false, false, '', '',
                                   now(), now() FROM generate_series(1, 500) g""",
                    [self.a.id, patient, self.doctor, consult.id, consult.section_id, self.day])
        later = self.day + dt.timedelta(days=7)
        last = self.book(patient_id=patient, doctor_id=self.doctor, day=later.isoformat(),
                         starts_at=self.slot_iso(self.a, later, "10:00")).json()["appointment"]
        session = self.session()
        r = call(self.owner, "PUT", f"/api/v1/schedules/{session['id']}",
                 {**session, "starts": "09:00", "ends": "10:00", "expected": session})
        self.assertEqual((r.status_code, r.json()["error"]["code"], r.json()["error"]["count"]),
                         (409, "schedule_conflict", 1), r.content)
        self.assertEqual([b["id"] for b in r.json()["error"]["bookings"]], [last["id"]])

    def test_a_shorter_slot_that_starts_at_the_same_time_is_refused(self):
        booked = self.book(doctor_id=self.doctor, day=self.day.isoformat(),
                           starts_at=self.slot_iso(self.a, self.day, "10:00")).json()["appointment"]
        session = self.session()
        r = call(self.owner, "PUT", f"/api/v1/schedules/{session['id']}", {**session, "slot_minutes": 15, "expected": session})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "schedule_conflict"), r.content)
        self.assertEqual([b["id"] for b in r.json()["error"]["bookings"]], [booked["id"]])

    def test_the_database_refuses_an_overlapping_slot(self):
        booked = self.book(doctor_id=self.doctor, day=self.day.isoformat(),
                           starts_at=self.slot_iso(self.a, self.day, "10:00")).json()["appointment"]
        row = self.appointment(booked["id"])
        fields = {name: getattr(row, name) for name in ("clinic_id", "patient_id", "doctor_id", "service_id",
                                                        "section_id", "day", "fee")}
        with clinic_transaction(self.a.id, self.a.shard):
            with self.assertRaises(IntegrityError) as caught, transaction.atomic(using=self.a.shard):
                Appointment.objects.create(**fields, queue_no=99, starts_at=row.starts_at + dt.timedelta(minutes=15),
                                           ends_at=row.ends_at + dt.timedelta(minutes=15))
            self.assertIn("appointment_no_overlap", str(caught.exception))
            # An extra visit on purpose, and the next slot, are fine.
            Appointment.objects.create(**fields, queue_no=98, starts_at=row.starts_at, ends_at=row.ends_at, is_extra=True)
            Appointment.objects.create(**fields, queue_no=97, starts_at=row.ends_at,
                                       ends_at=row.ends_at + dt.timedelta(minutes=20))
