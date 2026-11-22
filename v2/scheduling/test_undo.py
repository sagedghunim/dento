"""Undo on the desk (design D4, requirements 7.8): undo-arrive and undo-call."""

import datetime as dt
import threading
import uuid

from django.db import connections
from django.test import TransactionTestCase

from audit.models import AuditChange
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.models import Appointment
from scheduling.testing import M1TestCase, add_schedule, free_rooms, services_of, summaries, today

UNDO = {"undo-arrive": ("waiting", "booked", "arrived_at"), "undo-call": ("in_room", "waiting", "called_at")}
STATUSES = ["booked", "waiting", "in_room", "done", "cancelled", "no_show"]


def run_together(jobs):
    """Each callable in its own thread (its own connections), released at once by a barrier."""
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


def audits(fixture, action):
    with clinic_transaction(fixture.id, fixture.shard):
        return list(AuditChange.objects.filter(clinic_id=fixture.id, action=action).order_by("id").values(
            "target_id", "details", "membership_id"))


class UndoTests(M1TestCase):
    def test_undo_arrive_goes_back_to_booked_keeps_the_turn_and_is_logged(self):
        appt = self.book(arrive=True).json()["appointment"]
        before = summaries(self.a, today(self.a))
        r = self.act(appt["id"], "undo-arrive", "waiting")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["appointment"]["status"], body["appointment"]["arrived_at"]), ("booked", None))
        self.assertEqual(body["appointment"]["queue_no"], appt["queue_no"])
        after = summaries(self.a, today(self.a))
        self.assertEqual(after[0]["version"], before[0]["version"] + 1)
        self.assertEqual(body["version"], after[0]["version"])
        self.assertTrue(after[0]["changed_since_recompute"])
        self.assertEqual(after[1], before[1])  # no counter changes (design 5.3)
        [entry] = audits(self.a, "appointment.undo_arrive")
        self.assertEqual(entry["target_id"], appt["id"])
        self.assertEqual(entry["membership_id"], self.a.membership("assistant").id)
        self.assertEqual(entry["details"]["before"]["status"], "waiting")
        self.assertIsNotNone(entry["details"]["before"]["arrived_at"])
        self.assertEqual(entry["details"]["after"], {"status": "booked", "arrived_at": None})
        # It can arrive again, and Today shows it as booked meanwhile.
        self.assertEqual(self.act(appt["id"], "arrive", "booked").json()["appointment"]["status"], "waiting")

    def test_undo_call_goes_back_to_waiting_and_keeps_the_arrival(self):
        appt = self.book(arrive=True).json()["appointment"]
        self.act(appt["id"], "call", "waiting")
        r = self.act(appt["id"], "undo-call", "in_room")
        self.assertEqual(r.status_code, 200, r.content)
        row = self.appointment(appt["id"])
        self.assertEqual((row.status, row.called_at, row.queue_no), ("waiting", None, appt["queue_no"]))
        self.assertIsNotNone(row.arrived_at)
        self.assertEqual(len(audits(self.a, "appointment.undo_call")), 1)
        # Finishing now needs a new call.
        r = self.act(appt["id"], "finish", "in_room")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "status_changed"))

    def test_matrix_every_undo_against_every_status(self):
        day = today(self.a)
        for action, (source, target, _column) in UNDO.items():
            for status in STATUSES:
                with self.subTest(action=action, status=status):
                    appointment_id = self._in_status(status)
                    before = summaries(self.a, day)
                    logged = len(audits(self.a, f"appointment.{action.replace('-', '_')}"))
                    r = self.act(appointment_id, action, source)
                    after = summaries(self.a, day)
                    current = self.appointment(appointment_id).status
                    now_logged = len(audits(self.a, f"appointment.{action.replace('-', '_')}"))
                    if status == source:
                        self.assertEqual((r.status_code, current), (200, target), r.content)
                        self.assertEqual(after[0]["version"], before[0]["version"] + 1)
                        self.assertEqual(now_logged, logged + 1)
                    elif status == target:
                        self.assertEqual((r.status_code, current), (200, target), r.content)
                        self.assertEqual((after, now_logged), (before, logged))  # a harmless repeat
                    else:
                        self.assertEqual((r.status_code, r.json()["error"]["code"], current),
                                         (409, "status_changed", status))
                        self.assertEqual((after, now_logged), (before, logged))

    def _in_status(self, status):
        appt = self.book().json()["appointment"]
        now = dt.datetime.now(dt.UTC)
        times = {"booked": {}, "waiting": {"arrived_at": now}, "in_room": {"arrived_at": now, "called_at": now},
                 "done": {"arrived_at": now, "called_at": now, "finished_at": now}, "cancelled": {"cancelled_at": now},
                 "no_show": {}}[status]
        with clinic_transaction(self.a.id, self.a.shard):
            Appointment.objects.filter(clinic_id=self.a.id, id=appt["id"]).update(status=status, **times)
        return appt["id"]

    def test_the_seen_status_must_be_the_one_right_after_the_step(self):
        appt = self.book(arrive=True).json()["appointment"]
        for action, seen in (("undo-arrive", "booked"), ("undo-arrive", "in_room"), ("undo-call", "waiting")):
            r = self.act(appt["id"], action, seen)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_transition"), action)
        self.assertEqual(self.act(appt["id"], "undo-arrive", "lost").status_code, 400)
        self.assertEqual(self.appointment(appt["id"]).status, "waiting")

    def test_a_repeat_with_the_same_key_replays(self):
        appt = self.book(arrive=True).json()["appointment"]
        key = uuid.uuid4()
        first = call(self.desk, "POST", f"/api/v1/appointments/{appt['id']}/undo-arrive", {"status": "waiting"}, key=key)
        again = call(self.desk, "POST", f"/api/v1/appointments/{appt['id']}/undo-arrive", {"status": "waiting"}, key=key)
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(first.json(), again.json())
        self.assertEqual(len(audits(self.a, "appointment.undo_arrive")), 1)

    def test_needs_the_queue_code_and_another_clinics_visit_is_404(self):
        from clinics.models import Membership

        appt = self.book(arrive=True).json()["appointment"]
        foreign = self.book(fixture=self.b, arrive=True).json()["appointment"]
        self.assertEqual(self.act(foreign["id"], "undo-arrive", "waiting").status_code, 404)
        self.assertEqual(self.appointment(foreign["id"], self.b).status, "waiting")
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["appointments.queue"])
        self.assertEqual(self.act(appt["id"], "undo-arrive", "waiting").status_code, 403)


class UndoRaceTests(TransactionTestCase):
    """An undo racing the next step: exactly one of them takes effect, and the summaries agree."""

    databases = "__all__"

    def setUp(self):
        helper = ShardTestCase()
        self.a = helper.clinic("ur", "shard_1")
        add_schedule(self.a, "owner")
        self.services = services_of(self.a)
        self.owner_id = self.a.membership("owner").id

    def visit(self, desk, status):
        appt = call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.owner_id, "service_id": self.services["consultation"].id, "arrive": True,
            "new_patient": new_patient(full_name="Race Patient")}).json()["appointment"]
        if status == "in_room":
            call(desk, "POST", f"/api/v1/appointments/{appt['id']}/call", {"status": "waiting"})
        return appt["id"]

    def race(self, start_status, undo, other, other_seen):
        desk = self.a.client("assistant")
        outcomes = []
        for _round in range(6):
            if other == "call":
                free_rooms(self.a)  # one patient in the room per doctor (Saged, 2026-11-13): each round starts free
            appointment_id = self.visit(desk, start_status)
            version = summaries(self.a, today(self.a))[0]["version"]
            c1, c2 = self.a.client("assistant"), self.a.client("assistant")
            undo_r, other_r = run_together([
                lambda: call(c1, "POST", f"/api/v1/appointments/{appointment_id}/{undo}", {"status": start_status}),
                lambda: call(c2, "POST", f"/api/v1/appointments/{appointment_id}/{other}", {"status": other_seen}),
            ])
            codes = (undo_r.status_code, other_r.status_code)
            self.assertIn(codes, [(200, 409), (409, 200)], (undo_r.content, other_r.content))
            loser = undo_r if codes[0] == 409 else other_r
            self.assertEqual(loser.json()["error"]["code"], "status_changed")
            with clinic_transaction(self.a.id, self.a.shard):
                status = Appointment.objects.get(clinic_id=self.a.id, id=appointment_id).status
            winner = UNDO[undo][1] if codes[0] == 200 else {"call": "in_room", "finish": "done"}[other]
            self.assertEqual(status, winner)
            self.assertEqual(summaries(self.a, today(self.a))[0]["version"], version + 1)  # one change, one bump
            outcomes.append(codes)
        return outcomes

    def test_undo_call_against_finish(self):
        self.race("in_room", "undo-call", "finish", "in_room")
        clinic, doctors = summaries(self.a, today(self.a))
        with clinic_transaction(self.a.id, self.a.shard):
            done = Appointment.objects.filter(clinic_id=self.a.id, status="done").count()
            undone = AuditChange.objects.filter(clinic_id=self.a.id, action="appointment.undo_call").count()
            waiting = Appointment.objects.filter(clinic_id=self.a.id, status="waiting").count()
        self.assertEqual((clinic["done"], doctors[self.owner_id]["done"]), (done, done))
        self.assertEqual(undone, waiting)
        self.assertEqual(done + waiting, 6)

    def test_undo_arrive_against_call(self):
        self.race("waiting", "undo-arrive", "call", "waiting")
        with clinic_transaction(self.a.id, self.a.shard):
            booked = Appointment.objects.filter(clinic_id=self.a.id, status="booked", arrived_at__isnull=True).count()
            undone = AuditChange.objects.filter(clinic_id=self.a.id, action="appointment.undo_arrive").count()
        self.assertEqual(booked, undone)


class AskTheDoctorTests(M1TestCase):
    """The desk asks the doctor to take a patient; the doctor lets them in or says wait (Saged, 2026-11-22)."""

    def test_the_doctor_says_wait_and_the_desk_can_ask_again(self):
        doctor = self.a.membership("owner")  # book() books with the owner as the doctor
        appt = self.book(arrive=True).json()["appointment"]
        self.assertEqual(self.act(appt["id"], "ready", "waiting").status_code, 200)  # the desk asks
        r = self.act(appt["id"], "wait", "ready", client=self.a.client("owner"))
        self.assertEqual(r.status_code, 200, r.content)
        row = self.appointment(appt["id"])
        self.assertEqual((row.status, row.ready_at, row.wait_at is not None), ("waiting", None, True))
        self.assertEqual(audits(self.a, "appointment.wait")[0]["membership_id"], doctor.id)
        # Asking again clears the doctor's "wait"; calling in clears it too.
        self.assertEqual(self.act(appt["id"], "ready", "waiting").status_code, 200)
        self.assertIsNone(self.appointment(appt["id"]).wait_at)
        self.assertEqual(self.act(appt["id"], "call", "ready", client=self.a.client("owner")).status_code, 200)
        self.assertEqual(self.appointment(appt["id"]).status, "in_room")


class TreatmentCourseTests(M1TestCase):
    """Visits of one treatment are numbered 1, 2, 3… with no count planned; a new treatment starts at 1
    (Saged, 2026-11-22)."""

    def test_a_visit_that_continues_a_treatment_takes_the_next_number(self):
        first = self.book().json()["appointment"]
        self.assertEqual(first["course_no"], 1)
        patient = first["patient"]["id"]
        second = self.book(patient_id=patient, continues_id=first["id"], day=self.later(7)).json()["appointment"]
        third = self.book(patient_id=patient, continues_id=second["id"], day=self.later(14)).json()["appointment"]
        self.assertEqual((second["course_no"], third["course_no"]), (2, 3))
        self.assertEqual(third["continues_id"], first["id"])  # every visit points at the first one
        fresh = self.book(patient_id=patient, day=self.later(21)).json()["appointment"]
        self.assertEqual((fresh["course_no"], fresh["continues_id"]), (1, None))
        courses = call(self.desk, "GET", f"/api/v1/patients/{patient}/courses").json()["items"]
        self.assertEqual([(c["id"], c["last_no"]) for c in courses][-1], (first["id"], 3))
        # Another patient's visit can't be continued.
        other = self.book().json()["appointment"]
        self.book(patient_id=patient, continues_id=other["id"], day=self.later(28), expect=404)

    def later(self, days):
        return (today(self.a) + dt.timedelta(days=days)).isoformat()
