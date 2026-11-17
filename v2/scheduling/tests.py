import datetime as dt
from zoneinfo import ZoneInfo

from django.db import connections
from django.test.utils import CaptureQueriesContext

from clinics.models import Clinic, Membership, Schedule
from core.routing import clinic_transaction
from core.testing import call, new_patient
from scheduling import rules
from scheduling.jobs import recompute_clinic, sweep_clinic
from scheduling.models import Appointment
from scheduling.testing import M1TestCase, free_rooms, frozen, summaries, today

CAIRO = ZoneInfo("Africa/Cairo")


def cairo(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=CAIRO)


class BookingTests(M1TestCase):
    def test_turns_count_up_per_doctor_day_and_a_new_patient_gets_a_file_number(self):
        first = self.book().json()["appointment"]
        second = self.book(new_patient=new_patient(full_name="Sara Ali")).json()["appointment"]
        self.assertEqual((first["queue_no"], second["queue_no"]), (1, 2))
        self.assertEqual((first["patient"]["file_no"], second["patient"]["file_no"]), (1, 2))
        self.assertTrue(first["is_new"])
        # One open visit per patient and doctor a day (Saged, 2026-11-17): the first is cancelled, then booked again.
        self.assertEqual(self.act(first["id"], "cancel", "booked").status_code, 200)
        again = self.book(patient_id=first["patient"]["id"]).json()["appointment"]
        self.assertEqual((again["queue_no"], again["patient"]["file_no"]), (3, 1))
        clinic, doctors = summaries(self.a, today(self.a))
        owner = self.a.membership("owner").id
        self.assertEqual((doctors[owner]["last_queue_no"], doctors[owner]["booked"]), (3, 2))
        self.assertEqual(clinic["version"], 4)

    def test_booking_answers_the_row_and_the_version(self):
        r = self.book(payment={"amount": 5000, "method": "cash"})
        body = r.json()
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertIn("Idempotency-Key", r)
        appt = body["appointment"]
        self.assertEqual(appt["invoice"]["number"], 1)
        self.assertEqual(appt["due"], self.services_a["consultation"].price - 5000)
        self.assertEqual(body["version"], 1)
        # The same shape as a read of the row.
        self.assertEqual(call(self.desk, "GET", f"/api/v1/appointments/{appt['id']}").json(), body)

    def test_another_clinics_patient_doctor_or_service_in_the_body_is_404_and_writes_nothing(self):
        b_patient = self.book(fixture=self.b).json()["appointment"]["patient"]["id"]
        before = summaries(self.a, today(self.a))
        for body in ({"patient_id": b_patient}, {"doctor_id": self.b.membership("owner").id},
                     {"service_id": self.services_b["consultation"].id}):
            r = self.book(expect=404, **body)
            self.assertEqual(r.json()["error"]["code"], "not_found", body)
        self.assertEqual(summaries(self.a, today(self.a)), before)

    def test_assistant_is_not_a_doctor(self):
        r = self.book(expect=404, doctor_id=self.a.membership("assistant").id)
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_a_doctor_who_doesnt_work_that_day_is_refused(self):
        with clinic_transaction(self.a.id, self.a.shard):
            Schedule.objects.filter(clinic_id=self.a.id, doctor_id=self.a.membership("owner").id,
                                    weekday=today(self.a).weekday()).delete()
        self.assertEqual(self.book(expect=409).json()["error"]["code"], "doctor_off")

    def test_slot_doctors_need_a_time_on_their_grid_and_one_booking_per_slot(self):
        day = today(self.a)
        doctor = self.a.membership("doctor").id
        self.assertEqual(self.book(expect=400, doctor_id=doctor).json()["error"]["code"], "time_required")
        self.assertEqual(self.book(expect=400, doctor_id=doctor, starts_at=self.slot_iso(self.a, day, "10:10"))
                         .json()["error"]["code"], "off_grid")
        slot = self.slot_iso(self.a, day, "10:20")
        first = self.book(doctor_id=doctor, starts_at=slot).json()["appointment"]
        self.assertEqual((first["starts_at"], first["queue_no"]), (slot, 1))
        self.assertEqual(self.book(expect=409, doctor_id=doctor, starts_at=slot).json()["error"]["code"], "slot_taken")
        extra = self.book(doctor_id=doctor, starts_at=slot, extra=True).json()["appointment"]
        self.assertTrue(extra["is_extra"])
        # The refused booking's turn was rolled back with it: no gap.
        self.assertEqual(extra["queue_no"], 2)
        slots = call(self.desk, "GET", f"/api/v1/doctors/{doctor}/slots?day={day}").json()
        self.assertEqual(len(slots["slots"]), 12)
        self.assertEqual([s["starts_at"] for s in slots["slots"] if s["taken"]], [slot])

    def test_max_bookings_fills_the_day(self):
        with clinic_transaction(self.a.id, self.a.shard):
            Schedule.objects.filter(clinic_id=self.a.id, doctor_id=self.a.membership("owner").id).update(max_bookings=2)
        self.book()
        self.book()
        self.assertEqual(self.book(expect=409).json()["error"]["code"], "day_full")
        _, doctors = summaries(self.a, today(self.a))
        self.assertEqual(doctors[self.a.membership("owner").id]["booked"], 2)

    def test_a_cancel_frees_a_place_under_max_bookings(self):
        with clinic_transaction(self.a.id, self.a.shard):
            Schedule.objects.filter(clinic_id=self.a.id, doctor_id=self.a.membership("owner").id).update(max_bookings=1)
        first = self.book().json()["appointment"]
        self.assertEqual(self.act(first["id"], "cancel", "booked").status_code, 200)
        self.assertEqual(self.book().json()["appointment"]["queue_no"], 2)

    def test_payment_inside_a_booking_only_for_today_and_arrive_only_today(self):
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()
        r = self.book(expect=400, day=tomorrow, payment={"amount": 100, "method": "cash"})
        self.assertEqual(r.json()["error"]["code"], "payment_not_today")
        self.assertEqual(self.book(expect=409, day=tomorrow, arrive=True).json()["error"]["code"], "not_today")
        self.assertEqual(self.book(day=tomorrow).json()["appointment"]["day"], tomorrow)

    def test_the_day_must_be_today_up_to_a_year_ahead(self):
        for day in (today(self.a) - dt.timedelta(days=1), today(self.a) + dt.timedelta(days=367)):
            self.assertEqual(self.book(expect=400, day=day.isoformat()).json()["error"]["fields"], {"day": "range"})

    def test_payment_above_the_fee_is_refused(self):
        fee = self.services_a["consultation"].price
        self.assertEqual(self.book(expect=409, payment={"amount": fee + 1, "method": "cash"}).json()["error"]["code"],
                         "over_balance")

    def test_body_codes(self):
        from clinics.models import Membership

        def revoke(code):
            with clinic_transaction(self.a.id, self.a.shard):
                members = Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id)
                members.update(revoked=[*members.get().revoked, code])

        patient = self.book().json()["appointment"]["patient"]["id"]
        revoke("patients.edit")
        self.assertEqual(self.book(expect=403).status_code, 403)
        self.book(patient_id=patient, day=(today(self.a) + dt.timedelta(days=1)).isoformat())
        revoke("billing.collect")
        self.book(expect=403, patient_id=patient, payment={"amount": 100, "method": "cash"})
        revoke("appointments.queue")
        self.book(expect=403, patient_id=patient, arrive=True)

    def test_bad_input_is_a_clear_400(self):
        for body in ({"doctor_id": "x"}, {"doctor_id": 2**70}, {"day": "2026-13-01"}, {"source": "fax"},
                     {"payment": {"amount": "abc", "method": "cash"}}, {"payment": {"amount": 100, "method": "gold"}},
                     {"starts_at": "10:00"}, {"new_patient": {"full_name": "x y", "phone": "01099990009", "sex": "m", "age": 30}},
                     {"new_patient": new_patient(full_name="Ali", phone="123")}, {"new_patient": new_patient(sex="")},
                     {"new_patient": new_patient(age=None)}, {"new_patient": new_patient(phone="")},
                     {"new_patient": new_patient(phone="0221234567")},
                     {"new_patient": new_patient(phone_owner="guardian")}, {"patient_id": 1, "new_patient": {}}):
            r = self.book(expect=400, **body)
            self.assertEqual(r.json()["error"]["code"] in ("invalid", "off_grid"), True, (body, r.content))


class FollowUpTests(M1TestCase):
    def setUp(self):
        super().setUp()
        self.consult = self.services_a["consultation"]
        self.follow = self.services_a["follow_up"]
        from clinics.models import Service

        with clinic_transaction(self.a.id, self.a.shard):
            Service.objects.filter(clinic_id=self.a.id, id=self.follow.id).update(price=10000)

    def _visit(self, day, arrive=False, **extra):
        r = self.book(day=day.isoformat(), arrive=arrive, **extra)
        return r.json()["appointment"]

    def test_a_consultation_within_the_window_after_an_arrived_one_takes_the_follow_up_price(self):
        first = self._visit(today(self.a), arrive=True)
        second = self._visit(today(self.a) + dt.timedelta(days=14), patient_id=first["patient"]["id"])
        self.assertEqual((second["fee"], second["is_follow_up"], second["is_new"]), (10000, True, False))
        late = self._visit(today(self.a) + dt.timedelta(days=15), patient_id=first["patient"]["id"])
        self.assertEqual((late["fee"], late["is_follow_up"]), (self.consult.price, False))

    def test_only_with_the_same_doctor_and_a_follow_up_starts_no_window(self):
        first = self._visit(today(self.a), arrive=True)
        pid = first["patient"]["id"]
        other = self.book(patient_id=pid, doctor_id=self.a.membership("doctor").id,
                          starts_at=self.slot_iso(self.a, today(self.a) + dt.timedelta(days=3), "10:00"),
                          day=(today(self.a) + dt.timedelta(days=3)).isoformat()).json()["appointment"]
        self.assertFalse(other["is_follow_up"])
        self._done(first)  # one open visit a day with a doctor (Saged, 2026-11-17)
        follow = self.book(patient_id=pid, service_id=self.follow.id, arrive=True).json()["appointment"]
        self.assertTrue(follow["is_follow_up"])
        # The quote agrees with the rule.
        q = call(self.desk, "GET", f"/api/v1/appointments/quote?patient={pid}&doctor={self.a.membership('owner').id}"
                                   f"&service={self.consult.id}&day={today(self.a) + dt.timedelta(days=10)}").json()
        self.assertEqual((q["fee"], q["is_follow_up"]), (10000, True))

    def test_a_paid_consultation_counts_and_an_unpaid_no_show_doesnt(self):
        unpaid = self._visit(today(self.a))
        pid = unpaid["patient"]["id"]
        self.assertFalse(self._visit(today(self.a) + dt.timedelta(days=2), patient_id=pid)["is_follow_up"])
        call(self.desk, "POST", f"/api/v1/appointments/{unpaid['id']}/payments", {"amount": 100, "method": "cash"})
        self.assertTrue(self._visit(today(self.a) + dt.timedelta(days=3), patient_id=pid)["is_follow_up"])

    def test_zero_days_turns_the_rule_off(self):
        call(self.owner, "PUT", "/api/v1/settings/clinic", {"changes": {"follow_up_days": 0}, "expected": {}})
        first = self._visit(today(self.a), arrive=True)
        self._done(first)
        self.assertFalse(self._visit(today(self.a), patient_id=first["patient"]["id"])["is_follow_up"])

    def _done(self, visit):
        free_rooms(self.a)
        self.assertEqual(self.act(visit["id"], "call", "waiting").status_code, 200)
        self.assertEqual(self.act(visit["id"], "finish", "in_room").status_code, 200)

    def test_the_rule_is_checked_again_at_arrival_only_while_there_is_no_invoice(self):
        # Three days, one visit a day (Saged, 2026-11-17: one open visit a day with a doctor), all booked on day 1.
        with frozen(cairo("2026-11-10T12:00")):
            desk = self.a.client("assistant")
            first = self.book(client=desk, day="2026-11-10").json()["appointment"]
            pid = first["patient"]["id"]
            second = self.book(client=desk, day="2026-11-11", patient_id=pid).json()["appointment"]
            third = self.book(client=desk, day="2026-11-12", patient_id=pid).json()["appointment"]
            self.assertFalse(second["is_follow_up"])
            call(desk, "POST", f"/api/v1/appointments/{third['id']}/invoice")
            self.assertEqual(self.act(first["id"], "arrive", "booked", client=desk).status_code, 200)
        with frozen(cairo("2026-11-11T12:00")):
            arrived = self.act(second["id"], "arrive", "booked", client=self.a.client("assistant")).json()["appointment"]
        self.assertEqual((arrived["fee"], arrived["is_follow_up"]), (10000, True))
        with frozen(cairo("2026-11-12T12:00")):
            kept = self.act(third["id"], "arrive", "booked", client=self.a.client("assistant")).json()["appointment"]
        self.assertEqual((kept["fee"], kept["is_follow_up"], kept["invoice"]["total"]), (self.consult.price, False,
                                                                                         self.consult.price))


class TransitionMatrixTests(M1TestCase):
    """T4: every action against every status. Repeats leave the counters unchanged."""

    ACTIONS = {"arrive": ("booked", "waiting"), "call": ("waiting", "in_room"), "finish": ("in_room", "done"),
               "cancel": ("booked", "cancelled")}
    STATUSES = ["booked", "waiting", "in_room", "done", "cancelled", "no_show"]

    def _in_status(self, status):
        free_rooms(self.a)  # one patient in the room per doctor: each subtest starts with the room free
        appt = self.book().json()["appointment"]
        now = dt.datetime.now(dt.UTC)
        times = {"booked": {}, "waiting": {"arrived_at": now}, "in_room": {"arrived_at": now, "called_at": now},
                 "done": {"arrived_at": now, "called_at": now, "finished_at": now}, "cancelled": {"cancelled_at": now},
                 "no_show": {}}[status]
        with clinic_transaction(self.a.id, self.a.shard):
            Appointment.objects.filter(clinic_id=self.a.id, id=appt["id"]).update(status=status, **times)
        return appt["id"]

    def test_matrix(self):
        day = today(self.a)
        for action, (source, target) in self.ACTIONS.items():
            for status in self.STATUSES:
                with self.subTest(action=action, status=status):
                    appointment_id = self._in_status(status)
                    before = summaries(self.a, day)
                    r = self.act(appointment_id, action, source)
                    after = summaries(self.a, day)
                    current = self.appointment(appointment_id).status
                    if status == source:
                        self.assertEqual((r.status_code, current), (200, target), r.content)
                        self.assertEqual(after[0]["version"], before[0]["version"] + 1)
                    elif status == target:
                        self.assertEqual((r.status_code, current), (200, target), r.content)
                        self.assertEqual(after, before)  # a repeat changes nothing
                    else:
                        self.assertEqual((r.status_code, r.json()["error"]["code"], current),
                                         (409, "status_changed", status))
                        self.assertEqual(r.json()["error"]["fields"], {"status": status})
                        self.assertEqual(after, before)

    def test_counters_follow_the_counters_table(self):
        day = today(self.a)
        owner = self.a.membership("owner").id
        appt = self.book(arrive=True).json()["appointment"]
        self.act(appt["id"], "call", "waiting")
        self.act(appt["id"], "finish", "in_room")
        self.act(appt["id"], "finish", "in_room")  # a repeat
        clinic, doctors = summaries(self.a, day)
        self.assertEqual((doctors[owner]["done"], clinic["done"], doctors[owner]["booked"]), (1, 1, 1))
        other = self.book().json()["appointment"]
        self.act(other["id"], "cancel", "booked", reason="Travelling")
        self.act(other["id"], "cancel", "booked")
        _, doctors = summaries(self.a, day)
        self.assertEqual(doctors[owner]["booked"], 1)
        self.assertEqual(self.appointment(other["id"]).cancel_reason, "Travelling")

    def test_the_seen_status_must_be_the_actions_source(self):
        appt = self.book().json()["appointment"]
        r = self.act(appt["id"], "arrive", "waiting")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_transition"))
        self.assertEqual(self.act(appt["id"], "arrive", "lost").status_code, 400)

    def test_an_unknown_or_foreign_appointment_is_404(self):
        foreign = self.book(fixture=self.b).json()["appointment"]["id"]
        for appointment_id in (foreign, 999, 2**63):
            r = self.act(appointment_id, "arrive", "booked")
            self.assertEqual(r.status_code, 404, appointment_id)
        self.assertEqual(self.appointment(foreign, self.b).status, "booked")


class CancelTests(M1TestCase):
    def test_refund_first_while_money_is_paid(self):
        appt = self.book(payment={"amount": 5000, "method": "cash"}).json()["appointment"]
        r = self.act(appt["id"], "cancel", "booked")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "refund_first"))
        self.assertEqual(self.appointment(appt["id"]).status, "booked")

    def test_an_unpaid_open_invoice_is_voided_with_the_cancel(self):
        from audit.models import AuditChange
        from billing.models import Invoice

        appt = self.book().json()["appointment"]
        invoice = call(self.desk, "POST", f"/api/v1/appointments/{appt['id']}/invoice").json()["invoice"]
        self.assertEqual(self.act(appt["id"], "cancel", "booked").status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Invoice.objects.get(clinic_id=self.a.id, id=invoice["id"]).status, "void")
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.a.id, action="invoice.void").exists())


class RescheduleTests(M1TestCase):
    def move(self, appointment_id, client=None, **body):
        return call(client or self.desk, "POST", f"/api/v1/appointments/{appointment_id}/reschedule",
                    {"status": "booked", **body})

    def test_the_same_target_changes_nothing(self):
        appt = self.book().json()["appointment"]
        version = summaries(self.a, today(self.a))[0]["version"]
        r = self.move(appt["id"], doctor_id=appt["doctor_id"], day=appt["day"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(summaries(self.a, today(self.a))[0]["version"], version)

    def test_another_day_takes_a_new_turn_and_the_old_number_stays_a_gap(self):
        day, tomorrow = today(self.a), today(self.a) + dt.timedelta(days=1)
        owner = self.a.membership("owner").id
        first = self.book().json()["appointment"]
        self.book()
        self.book(day=tomorrow.isoformat())
        moved = self.move(first["id"], doctor_id=owner, day=tomorrow.isoformat()).json()
        self.assertEqual((moved["appointment"]["day"], moved["appointment"]["queue_no"]), (tomorrow.isoformat(), 2))
        old, new = summaries(self.a, day), summaries(self.a, tomorrow)
        self.assertEqual((old[1][owner]["booked"], old[1][owner]["last_queue_no"]), (1, 2))
        self.assertEqual((new[1][owner]["booked"], new[1][owner]["last_queue_no"]), (2, 2))
        self.assertEqual(moved["version"], new[0]["version"])

    def test_a_slot_move_within_the_doctor_day_keeps_the_turn(self):
        day = today(self.a)
        doctor = self.a.membership("doctor").id
        first = self.book(doctor_id=doctor, starts_at=self.slot_iso(self.a, day, "10:00")).json()["appointment"]
        self.book(doctor_id=doctor, starts_at=self.slot_iso(self.a, day, "10:40"))
        r = self.move(first["id"], doctor_id=doctor, day=day.isoformat(), starts_at=self.slot_iso(self.a, day, "10:40"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "slot_taken"))
        moved = self.move(first["id"], doctor_id=doctor, day=day.isoformat(),
                          starts_at=self.slot_iso(self.a, day, "11:00")).json()["appointment"]
        self.assertEqual((moved["queue_no"], moved["starts_at"]), (1, self.slot_iso(self.a, day, "11:00")))

    def test_only_booked_visits_move_and_never_to_another_clinics_doctor(self):
        appt = self.book(arrive=True).json()["appointment"]
        r = self.move(appt["id"], doctor_id=appt["doctor_id"], day=appt["day"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "status_changed"))
        booked = self.book().json()["appointment"]
        r = self.move(booked["id"], doctor_id=self.b.membership("owner").id, day=booked["day"])
        self.assertEqual(r.status_code, 404)


class TodayTests(M1TestCase):
    def test_etag_304_and_poll_after(self):
        self.book()
        first = call(self.desk, "GET", "/api/v1/today")
        self.assertEqual(first.status_code, 200)
        etag = first["ETag"]
        self.assertTrue(etag.startswith(f'"{self.a.id}.{today(self.a).isoformat()}.1.'))
        self.assertEqual(first["Poll-After"], "5")
        same = self.desk.get("/api/v1/today", headers={"X-Membership": str(self.desk.membership_id),
                                                       "If-None-Match": etag})
        self.assertEqual((same.status_code, same["Poll-After"], same["ETag"], same.content), (304, "5", etag, b""))
        self.assertEqual(same["Cache-Control"], "no-store")
        self.book()
        changed = self.desk.get("/api/v1/today", headers={"X-Membership": str(self.desk.membership_id),
                                                          "If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], etag)

    def test_the_etag_differs_by_codes_and_language(self):
        doctor_etag = call(self.a.client("doctor"), "GET", "/api/v1/today")["ETag"]
        self.assertNotEqual(doctor_etag, call(self.desk, "GET", "/api/v1/today")["ETag"])

    def test_queues_counts_and_money_only_with_finances_view(self):
        appt = self.book(arrive=True, payment={"amount": 5000, "method": "cash"}).json()["appointment"]
        self.book(new_patient=new_patient(full_name="Huda"))
        body = call(self.desk, "GET", "/api/v1/today").json()
        self.assertNotIn("totals", body)
        queue = next(q for q in body["queues"] if q["doctor"]["id"] == self.a.membership("owner").id)
        self.assertNotIn("collected", queue)
        self.assertEqual(queue["counts"]["waiting"], 1)
        self.assertEqual([a["queue_no"] for a in queue["appointments"]], [1, 2])
        self.assertEqual(body["recent"][0]["appointment_id"], appt["id"])
        owner = call(self.owner, "GET", "/api/v1/today").json()
        self.assertEqual(owner["totals"]["collected"], 5000)  # the place's money, though the owner sees their own queue
        self.assertEqual([q["doctor"]["id"] for q in owner["queues"]], [self.a.membership("owner").id])
        # The slot doctor works today, with no bookings yet.
        self.assertIn(self.a.membership("doctor").id, [q["doctor"]["id"] for q in body["queues"]])

    def test_another_clinics_rows_never_show(self):
        self.book(fixture=self.b, new_patient=new_patient(full_name="Secret Person"))
        body = call(self.desk, "GET", "/api/v1/today").content.decode()
        self.assertNotIn("Secret Person", body)

    def test_needs_book_or_queue(self):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["appointments.book"])
        self.assertEqual(call(self.desk, "GET", "/api/v1/today").status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["appointments.book", "appointments.queue"])
        self.assertEqual(call(self.desk, "GET", "/api/v1/today").status_code, 403)

    def test_another_day(self):
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()
        self.book(day=tomorrow)
        body = call(self.desk, "GET", f"/api/v1/today?day={tomorrow}").json()
        self.assertEqual(body["day"], tomorrow)
        self.assertEqual(sum(len(q["appointments"]) for q in body["queues"]), 1)
        self.assertEqual(call(self.desk, "GET", "/api/v1/today?day=tomorrow").status_code, 400)


class TimeTests(M1TestCase):
    """T8: the business day decides arrive and the sweep; a clinic in another time zone."""

    def test_arrive_belongs_to_the_session_until_the_day_ends(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            appt = self.book(client=desk).json()["appointment"]
            self.assertEqual(appt["day"], "2026-11-10")
        with frozen(cairo("2026-11-11T03:59")):
            desk = self.a.client("assistant")
            self.assertEqual(self.act(appt["id"], "arrive", "booked", client=desk).status_code, 200)
            late = self.book(client=desk, day="2026-11-10").json()["appointment"]
        with frozen(cairo("2026-11-11T04:00")):
            desk = self.a.client("assistant")
            r = self.act(late["id"], "arrive", "booked", client=desk)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_today"))
            self.assertEqual(self.book(client=desk).json()["appointment"]["day"], "2026-11-11")

    def test_the_sweep_waits_30_minutes_after_the_day_ends(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            booked = self.book(client=desk).json()["appointment"]
            waiting = self.book(client=desk, arrive=True).json()["appointment"]
            done = self.book(client=desk, arrive=True).json()["appointment"]
            self.act(done["id"], "call", "waiting", client=desk)
            self.act(done["id"], "finish", "in_room", client=desk)
            in_room = self.book(client=desk, arrive=True).json()["appointment"]
            self.act(in_room["id"], "call", "waiting", client=desk)  # one patient in the room at a time
        self.assertEqual(sweep_clinic(self.a.id, self.a.shard, cairo("2026-11-11T04:29")), {})
        self.assertEqual(self.appointment(booked["id"]).status, "booked")
        counts = sweep_clinic(self.a.id, self.a.shard, cairo("2026-11-11T04:30"))
        self.assertEqual(counts, {"no_show": 1, "left": 1, "auto_done": 1})
        rows = {i: self.appointment(i) for i in (booked["id"], waiting["id"], in_room["id"], done["id"])}
        self.assertEqual([(rows[i].status, rows[i].auto_closed) for i in rows],
                         [("no_show", False), ("no_show", True), ("done", True), ("done", False)])
        self.assertIsNotNone(rows[waiting["id"]].arrived_at)
        clinic, doctors = summaries(self.a, dt.date(2026, 11, 10))
        owner = self.a.membership("owner").id
        self.assertEqual((clinic["done"], doctors[owner]["done"], doctors[owner]["booked"]), (2, 2, 4))
        from billing.models import Invoice

        with clinic_transaction(self.a.id, self.a.shard):
            self.assertTrue(Invoice.objects.filter(clinic_id=self.a.id, appointment_id=in_room["id"], status="open").exists())
        # A second run finds nothing to close.
        self.assertEqual(sweep_clinic(self.a.id, self.a.shard, cairo("2026-11-11T05:00")), {})

    def test_a_clinic_in_another_time_zone(self):
        with clinic_transaction(self.b.id, self.b.shard):
            Clinic.objects.filter(id=self.b.id).update(timezone="Asia/Riyadh")
        with frozen(dt.datetime(2026, 11, 10, 1, 30, tzinfo=ZoneInfo("Asia/Riyadh"))):
            appt = self.book(fixture=self.b, client=self.b.client("assistant")).json()["appointment"]
        self.assertEqual(appt["day"], "2026-11-09")
        self.assertTrue(appt["arrived_at"] is None)

    def test_slot_grid_across_midnight_and_a_clock_change(self):
        clinic = Clinic(id=1, timezone="Africa/Cairo", day_ends_at=dt.time(4, 0))
        doctor = rules.DoctorDay(1, "x", [rules.Session(dt.time(22, 0), dt.time(2, 0), "slots", 20, None)])
        normal = rules.grid(clinic, dt.date(2026, 11, 12), doctor)
        self.assertEqual(len(normal), 12)
        self.assertEqual(normal[-1][1].astimezone(CAIRO).replace(tzinfo=None), dt.datetime(2026, 11, 13, 2, 0))
        # Egypt moves its clocks forward at midnight on the last Friday of April: the night is an hour shorter.
        spring = rules.grid(clinic, dt.date(2026, 4, 23), doctor)
        start = dt.datetime(2026, 4, 23, 22, 0, tzinfo=CAIRO)
        end = dt.datetime(2026, 4, 24, 2, 0, tzinfo=CAIRO)
        self.assertEqual(len(spring), int((end.astimezone(dt.UTC) - start.astimezone(dt.UTC)).total_seconds() // 1200))
        self.assertEqual(len(spring), 9)
        self.assertEqual(len({s for s, _ in spring}), len(spring))


class RecomputeTests(M1TestCase):
    def test_the_recompute_agrees_with_the_services_and_fixes_drift(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            first = self.book(client=desk, arrive=True, payment={"amount": 5000, "method": "cash"}).json()["appointment"]
            self.act(first["id"], "call", "waiting", client=desk)
            self.act(first["id"], "finish", "in_room", client=desk)
            other = self.book(client=desk).json()["appointment"]
            self.act(other["id"], "cancel", "booked", client=desk)
        before = summaries(self.a, dt.date(2026, 11, 10))
        self.assertEqual(recompute_clinic(self.a.id, self.a.shard, dt.date(2026, 11, 11)), [])
        after = summaries(self.a, dt.date(2026, 11, 10))
        self.assertFalse(after[0]["changed_since_recompute"])
        self.assertEqual(after[1], before[1])
        # Drift is found, fixed and the day's version moves.
        from summaries.models import DoctorDay

        with clinic_transaction(self.a.id, self.a.shard):
            DoctorDay.objects.filter(clinic_id=self.a.id, day=dt.date(2026, 11, 10)).update(collected=1, booked=9)
            from summaries.models import ClinicDay

            ClinicDay.objects.filter(clinic_id=self.a.id, day=dt.date(2026, 11, 10)).update(changed_since_recompute=True)
        found = recompute_clinic(self.a.id, self.a.shard, dt.date(2026, 11, 11))
        self.assertEqual(len(found), 1)
        fixed = summaries(self.a, dt.date(2026, 11, 10))
        self.assertEqual(fixed[1], before[1])
        self.assertEqual(fixed[0]["version"], after[0]["version"] + 1)

    def test_today_is_left_alone(self):
        self.book()
        self.assertEqual(recompute_clinic(self.a.id, self.a.shard), [])
        self.assertTrue(summaries(self.a, today(self.a))[0]["changed_since_recompute"])


class QueryCountTests(M1TestCase):
    """T14: the same number of statements with a few rows or many."""

    def count(self, fn):
        with CaptureQueriesContext(connections[self.a.shard]) as shard:
            fn()
        # The test's outer transaction turns the request's BEGIN/COMMIT into SAVEPOINT/RELEASE; they aren't work.
        return len([q for q in shard.captured_queries if "SAVEPOINT" not in q["sql"]])

    def _bulk_appointments(self, n):
        """n more booked appointments today, inserted directly (summaries kept in step)."""
        from core import sql
        from scheduling.jobs import system_ctx

        day = today(self.a)
        owner = self.a.membership("owner").id
        patient = self.book().json()["appointment"]["patient"]["id"]
        with clinic_transaction(self.a.id, self.a.shard):
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            start = sql.row(ctx, 'SELECT last_queue_no FROM "summaries_doctor_day" WHERE clinic_id = %s AND day = %s '
                                 "AND doctor_id = %s", [self.a.id, day, owner])[0]
            sql.run(ctx, """INSERT INTO "scheduling_appointment" (clinic_id, patient_id, doctor_id, service_id, section_id,
                            day, queue_no, is_extra, status, source, fee, is_follow_up, is_new, auto_closed, note,
                            cancel_reason, created_at, updated_at)
                            SELECT %s, %s, %s, %s, %s, %s, g, false, 'booked', 'clinic', 100, false, false, false, '', '',
                                   now(), now() FROM generate_series(%s, %s) g""",
                    [self.a.id, patient, owner, self.services_a["consultation"].id,
                     self.services_a["consultation"].section_id, day, start + 1, start + n])
            sql.run(ctx, 'UPDATE "summaries_doctor_day" SET last_queue_no = last_queue_no + %s, booked = booked + %s '
                         "WHERE clinic_id = %s AND day = %s AND doctor_id = %s", [n, n, self.a.id, day, owner])
        return patient

    def test_today_booking_and_status_changes_dont_grow_with_the_day(self):
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()  # today stays free for the measured booking
        patient = self.book(day=tomorrow).json()["appointment"]["patient"]["id"]
        procedure = self.services_a["procedure"].id

        def measure():
            today_full = self.count(lambda: call(self.desk, "GET", "/api/v1/today"))
            etag = call(self.desk, "GET", "/api/v1/today")["ETag"]
            today_304 = self.count(lambda: self.desk.get("/api/v1/today", headers={
                "X-Membership": str(self.desk.membership_id), "If-None-Match": etag}))
            booked = []
            booking = self.count(lambda: booked.append(self.book(patient_id=patient, arrive=True, service_id=procedure,
                                                                 payment={"amount": 100, "method": "cash"})))
            appt = booked[0].json()["appointment"]  # the patient's one open visit today (Saged, 2026-11-17)
            status = self.count(lambda: self.act(appt["id"], "call", "waiting"))
            self.act(appt["id"], "finish", "in_room")  # the room is free for the next measure
            return today_full, today_304, booking, status

        small = measure()
        self._bulk_appointments(300)
        large = measure()
        self.assertEqual(small, large)
        # Shard statements, request plumbing included (design 10): 304 = set_config, membership, version.
        self.assertEqual(small[1], 3)
        self.assertLessEqual(small[0], 6)
        # 12 plus the FOR SHARE on the doctor's row, which must be its own statement (chief judge M1, R4), plus the
        # money-day upsert of a paid booking (F5: one statement per summary table, design 5.0), plus the open
        # cash shift taken before the doctor-day (F1, 8a: cash opens a shift when none is, and that INSERT can
        # wait on another, so it must come before any summary row). Fixed: it never grows with the day.
        # Plus the lock on (patient, day) and the read of the patient's open visits that day (Saged, 2026-11-17).
        self.assertLessEqual(small[2], 17)


class FinishTests(M1TestCase):
    def test_finish_without_a_payment_still_ensures_the_invoice(self):
        """Design 5.3: ensure_invoice runs on finish, so a done visit is always owed or paid on an invoice."""
        appt = self.book(arrive=True).json()["appointment"]
        self.act(appt["id"], "call", "waiting")
        done = self.act(appt["id"], "finish", "in_room").json()["appointment"]
        self.assertIsNotNone(done["invoice"])
        self.assertEqual((done["invoice"]["number"], done["invoice"]["total"], done["due"]),
                         (1, appt["fee"], appt["fee"]))
