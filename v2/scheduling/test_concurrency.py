"""T5 (and T3's two-threads case): real threads against PostgreSQL, each with its own connections."""

import datetime as dt
import threading
import uuid

from django.db import connections
from django.test import TransactionTestCase

from billing.models import Invoice, Payment
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.models import Appointment
from scheduling.testing import add_schedule, services_of, summaries, today


class ConcurrencyTests(TransactionTestCase):
    databases = "__all__"

    def setUp(self):
        helper = ShardTestCase()
        self.a = helper.clinic("cc", "shard_1")
        add_schedule(self.a, "owner")
        add_schedule(self.a, "doctor", starts="10:00", ends="14:00", mode="slots", slot_minutes=20)
        self.services = services_of(self.a)
        self.owner_id = self.a.membership("owner").id
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                granted=["billing.refund", "billing.discount"])

    def run_together(self, jobs):
        """Run each callable in its own thread, released at once by a barrier. Returns their results in order."""
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait()
                results[index] = job()
            except Exception as error:  # surfaced by the assertions below
                results[index] = error
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def booking(self, client, **body):
        body.setdefault("doctor_id", self.owner_id)
        body.setdefault("service_id", self.services["consultation"].id)
        body.setdefault("new_patient", new_patient("Concurrent Patient"))
        return lambda: call(client, "POST", "/api/v1/appointments", body)

    def clients(self, n):
        return [self.a.client("assistant") for _ in range(n)]

    def test_eight_bookings_of_one_slot(self):
        from zoneinfo import ZoneInfo

        slot = dt.datetime.combine(today(self.a), dt.time(11, 0), tzinfo=ZoneInfo("Africa/Cairo")).isoformat()
        with self.assertNoLogs("core.api", "WARNING"):
            results = self.run_together([self.booking(c, doctor_id=self.a.membership("doctor").id, starts_at=slot)
                                         for c in self.clients(8)])
        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes, [201] + [409] * 7)
        self.assertTrue(all(r.json()["error"]["code"] == "slot_taken" for r in results if r.status_code == 409))
        _, doctors = summaries(self.a, today(self.a))
        self.assertEqual(doctors[self.a.membership("doctor").id]["booked"], 1)

    def test_eight_turns_for_one_doctor_day(self):
        with self.assertNoLogs("core.api", "WARNING"):
            results = self.run_together([self.booking(c) for c in self.clients(8)])
        self.assertEqual([r.status_code for r in results], [201] * 8)
        self.assertEqual(sorted(r.json()["appointment"]["queue_no"] for r in results), list(range(1, 9)))
        self.assertEqual(sorted(r.json()["appointment"]["patient"]["file_no"] for r in results), list(range(1, 9)))

    def test_concurrent_first_payments_leave_no_invoice_number_gap(self):
        desk = self.a.client("assistant")
        ids = [call(desk, "POST", "/api/v1/appointments", {"doctor_id": self.owner_id,
                                                           "service_id": self.services["consultation"].id,
                                                           "new_patient": new_patient(full_name=f"P{i}")}).json()["appointment"]["id"]
               for i in range(8)]
        fee = self.services["consultation"].price
        # Six first payments, one that is refused (over the balance), and a second first payment on visit 0.
        jobs = [lambda i=i, c=c: call(c, "POST", f"/api/v1/appointments/{ids[i]}/payments",
                                      {"amount": fee + 1 if i == 6 else 100, "method": "cash"})
                for i, c in enumerate(self.clients(7))]
        jobs.append(lambda c=self.a.client("assistant"): call(c, "POST", f"/api/v1/appointments/{ids[0]}/payments",
                                                              {"amount": 100, "method": "cash"}))
        results = self.run_together(jobs)
        self.assertEqual(sorted(r.status_code for r in results), [201] * 7 + [409])
        with clinic_transaction(self.a.id, self.a.shard):
            numbers = sorted(Invoice.objects.filter(clinic_id=self.a.id).values_list("number", flat=True))
            first_visit = Invoice.objects.get(clinic_id=self.a.id, appointment_id=ids[0])
        self.assertEqual(numbers, list(range(1, 7)))  # one invoice per visit, numbers without a gap
        self.assertEqual(first_visit.paid, 200)

    def test_payment_against_cancel_never_leaves_a_paid_cancelled_visit(self):
        desk = self.a.client("assistant")
        for round_ in range(6):
            appt = call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": self.owner_id, "service_id": self.services["consultation"].id,
                "new_patient": new_patient(full_name=f"Race {round_}")}).json()["appointment"]
            pay_client, cancel_client = self.clients(2)
            pay, cancel = self.run_together([
                lambda: call(pay_client, "POST", f"/api/v1/appointments/{appt['id']}/payments",
                             {"amount": 100, "method": "cash"}),
                lambda: call(cancel_client, "POST", f"/api/v1/appointments/{appt['id']}/cancel", {"status": "booked"}),
            ])
            self.assertEqual(sorted([pay.status_code, cancel.status_code]), [200, 409] if cancel.status_code == 200
                             else [201, 409])
            with clinic_transaction(self.a.id, self.a.shard):
                status = Appointment.objects.get(clinic_id=self.a.id, id=appt["id"]).status
                paid = sum(Invoice.objects.filter(clinic_id=self.a.id, appointment_id=appt["id"]).values_list("paid",
                                                                                                              flat=True))
            self.assertFalse(status == "cancelled" and paid > 0, (status, paid))

    def test_crossing_reschedules_dont_deadlock(self):
        desk = self.a.client("assistant")
        day, tomorrow = today(self.a), today(self.a) + dt.timedelta(days=1)
        doctor = self.a.membership("doctor").id
        for round_ in range(5):
            # A on (today, owner) moves to (tomorrow, owner); B on (tomorrow, owner) moves to (today, owner).
            a = call(desk, "POST", "/api/v1/appointments", {"doctor_id": self.owner_id, "day": day.isoformat(),
                     "service_id": self.services["consultation"].id, "new_patient": new_patient(full_name="Patient A")}).json()
            b = call(desk, "POST", "/api/v1/appointments", {"doctor_id": self.owner_id, "day": tomorrow.isoformat(),
                     "service_id": self.services["consultation"].id, "new_patient": new_patient(full_name="Patient B")}).json()
            ca, cb = self.clients(2)
            with self.assertNoLogs("core.api", "WARNING"):
                results = self.run_together([
                    lambda: call(ca, "POST", f"/api/v1/appointments/{a['appointment']['id']}/reschedule",
                                 {"status": "booked", "doctor_id": self.owner_id, "day": tomorrow.isoformat()}),
                    lambda: call(cb, "POST", f"/api/v1/appointments/{b['appointment']['id']}/reschedule",
                                 {"status": "booked", "doctor_id": self.owner_id, "day": day.isoformat()}),
                ])
            self.assertEqual([getattr(r, "status_code", r) for r in results], [200, 200], [getattr(r, "content", r) for r in results])
        # Slot swaps on one doctor-day go one at a time too.
        from zoneinfo import ZoneInfo

        def slot(hhmm):
            return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=ZoneInfo("Africa/Cairo")).isoformat()

        a = call(desk, "POST", "/api/v1/appointments", {"doctor_id": doctor, "starts_at": slot("12:00"),
                 "service_id": self.services["consultation"].id, "new_patient": new_patient(full_name="Patient A")}).json()["appointment"]
        b = call(desk, "POST", "/api/v1/appointments", {"doctor_id": doctor, "starts_at": slot("12:20"),
                 "service_id": self.services["consultation"].id, "new_patient": new_patient(full_name="Patient B")}).json()["appointment"]
        ca, cb = self.clients(2)
        with self.assertNoLogs("core.api", "WARNING"):
            results = self.run_together([
                lambda: call(ca, "POST", f"/api/v1/appointments/{a['id']}/reschedule",
                             {"status": "booked", "doctor_id": doctor, "day": day.isoformat(), "starts_at": slot("12:20")}),
                lambda: call(cb, "POST", f"/api/v1/appointments/{b['id']}/reschedule",
                             {"status": "booked", "doctor_id": doctor, "day": day.isoformat(), "starts_at": slot("12:00")}),
            ])
        self.assertEqual(sorted(r.status_code for r in results), [409, 409])  # each slot was taken when it was asked for
        _, doctors = summaries(self.a, tomorrow)
        self.assertEqual(doctors[self.owner_id]["booked"], 5)

    def test_two_call_ins_for_one_doctor_put_one_patient_in_the_room(self):
        """A doctor has one patient in the room at a time (Saged, 2026-11-13): two desks calling two patients at
        once for the same doctor get one 200 and one 409 doctor_busy, never two patients in the room."""
        desk = self.a.client("assistant")
        waiting = [call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.owner_id, "service_id": self.services["consultation"].id, "arrive": True,
            "new_patient": new_patient(full_name=f"Caller {i}")}).json()["appointment"]["id"] for i in range(2)]
        ca, cb = self.clients(2)
        with self.assertNoLogs("core.api", "WARNING"):
            results = self.run_together([
                lambda: call(ca, "POST", f"/api/v1/appointments/{waiting[0]}/call", {"status": "waiting"}),
                lambda: call(cb, "POST", f"/api/v1/appointments/{waiting[1]}/call", {"status": "waiting"}),
            ])
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        self.assertEqual([r.json()["error"]["code"] for r in results if r.status_code == 409], ["doctor_busy"])
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Appointment.objects.filter(clinic_id=self.a.id, status="in_room").count(), 1)

    def test_book_collect_print_against_finish_with_payment_on_one_doctor_day(self):
        desk = self.a.client("assistant")
        in_room = []
        for i in range(4):
            appt = call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": self.owner_id, "service_id": self.services["consultation"].id, "arrive": True,
                "new_patient": new_patient(full_name=f"Room {i}")}).json()["appointment"]
            in_room.append(appt["id"])
        # Four in the room for one doctor at once can't be made through "call" (one at a time, Saged 2026-11-13):
        # the race under test is finish against booking, so they are put there directly.
        with clinic_transaction(self.a.id, self.a.shard):
            Appointment.objects.filter(clinic_id=self.a.id, id__in=in_room).update(
                status="in_room", called_at=dt.datetime.now(dt.UTC))
        clients = self.clients(8)
        jobs = []
        for i in range(4):
            jobs.append(self.booking(clients[i], arrive=True, payment={"amount": 100, "method": "cash"}))
            jobs.append(lambda c=clients[4 + i], a=in_room[i]: call(c, "POST", f"/api/v1/appointments/{a}/finish",
                                                                    {"status": "in_room",
                                                                     "payment": {"amount": 100, "method": "card"}}))
        with self.assertNoLogs("core.api", "WARNING"):
            results = self.run_together(jobs)
        self.assertEqual(sorted(r.status_code for r in results), [200] * 4 + [201] * 4)
        clinic, doctors = summaries(self.a, today(self.a))
        self.assertEqual((clinic["collected"], clinic["done"], doctors[self.owner_id]["booked"]), (800, 4, 8))
        with clinic_transaction(self.a.id, self.a.shard):
            numbers = sorted(Invoice.objects.filter(clinic_id=self.a.id).values_list("number", flat=True))
        self.assertEqual(numbers, list(range(1, 9)))

    def test_a_payment_a_refund_and_a_discount_on_one_invoice(self):
        desk = self.a.client("assistant")
        fee = self.services["consultation"].price
        appt = call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.owner_id, "service_id": self.services["consultation"].id,
            "new_patient": new_patient(full_name="Busy"), "payment": {"amount": 10000, "method": "cash"}}).json()["appointment"]
        invoice = call(desk, "GET", f"/api/v1/invoices/{appt['invoice']['id']}").json()["invoice"]
        payment_id = invoice["payments"][0]["id"]
        c1, c2, c3 = self.clients(3)
        results = self.run_together([
            lambda: call(c1, "POST", f"/api/v1/appointments/{appt['id']}/payments", {"amount": 5000, "method": "cash"}),
            lambda: call(c2, "POST", f"/api/v1/payments/{payment_id}/refund", {"amount": 10000}),  # all, before going in
            lambda: call(c3, "PUT", f"/api/v1/invoices/{invoice['id']}/discount",
                         {"discount": fee - 15000, "expected_discount": 0}),
        ])
        self.assertTrue(all(r.status_code in (200, 201, 409) for r in results), [r.content for r in results])
        with clinic_transaction(self.a.id, self.a.shard):
            inv = Invoice.objects.get(clinic_id=self.a.id, id=invoice["id"])
            rows = list(Payment.objects.filter(clinic_id=self.a.id, invoice_id=inv.id).values_list("kind", "amount"))
        self.assertEqual(inv.paid, sum(a if k == "payment" else -a for k, a in rows))
        self.assertLessEqual(inv.paid, inv.total)

    def test_two_threads_with_one_key_give_one_booking_and_two_identical_answers(self):
        key = uuid.uuid4()
        body = {"doctor_id": self.owner_id, "service_id": self.services["consultation"].id,
                "new_patient": new_patient(full_name="Twice"), "payment": {"amount": 100, "method": "cash"}}
        results = self.run_together([lambda c=c: call(c, "POST", "/api/v1/appointments", body, key=key)
                                     for c in self.clients(2)])
        self.assertEqual([r.status_code for r in results], [201, 201])
        self.assertEqual(results[0].json(), results[1].json())
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Appointment.objects.filter(clinic_id=self.a.id).count(), 1)
            self.assertEqual(Payment.objects.filter(clinic_id=self.a.id).count(), 1)


class RecomputeRaceTests(TransactionTestCase):
    """T5: the recompute of a past day against live payments on that day's visits."""

    databases = "__all__"

    def test_recompute_against_live_writes(self):
        from zoneinfo import ZoneInfo

        from scheduling.jobs import expected_summaries, recompute_day, system_ctx
        from scheduling.testing import frozen

        from clinics.models import Clinic

        helper = ShardTestCase()
        a = helper.clinic("rr", "shard_1")
        add_schedule(a, "owner")
        services = services_of(a)
        past = dt.date(2026, 11, 10)
        with frozen(dt.datetime(2026, 11, 10, 19, 0, tzinfo=ZoneInfo("Africa/Cairo"))):
            desk = a.client("assistant")
            visits = [call(desk, "POST", "/api/v1/appointments", {
                "doctor_id": a.membership("owner").id, "service_id": services["consultation"].id,
                "new_patient": new_patient(full_name=f"Past {i}")}).json()["appointment"]["id"] for i in range(6)]

        def pay(client, appointment_id):
            # A payment on a past visit touches the past day's clinic_day (its version) while the recompute runs.
            return call(client, "POST", f"/api/v1/appointments/{appointment_id}/payments", {"amount": 100, "method": "cash"})

        # One frozen clock for every thread (patching the clock from several threads at once isn't safe).
        with frozen(dt.datetime(2026, 11, 11, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))):
            clients = [a.client("assistant") for _ in range(6)]
            jobs = [lambda c=c, v=v: pay(c, v) for c, v in zip(clients, visits)]
            jobs += [lambda: recompute_day(a.id, a.shard, past) for _ in range(2)]
            with self.assertNoLogs("core.api", "WARNING"):
                results = ConcurrencyTests.run_together(self, jobs)
        self.assertEqual([r.status_code for r in results[:6]], [201] * 6)
        self.assertEqual(results[6:], [[], []])  # nothing to fix: the live deltas and the recompute agree
        with clinic_transaction(a.id, a.shard):
            ctx = system_ctx(Clinic.objects.get(id=a.id), a.shard)
            for day in (past, dt.date(2026, 11, 11)):
                doctors, clinic = expected_summaries(ctx, day)
                current, rows = summaries(a, day)
                self.assertEqual({k: current[k] for k in clinic}, clinic)
                for doctor_id, want in doctors.items():
                    self.assertEqual({k: rows[doctor_id][k] for k in ("booked", "done", "collected", "refunded")},
                                     {k: want[k] for k in ("booked", "done", "collected", "refunded")})


class SameDayRaceTests(TransactionTestCase):
    """Two desks at once (Saged, 2026-11-17): one own number is one file, one open visit per patient and doctor a
    day, and a procedure once on a visit. Real threads against PostgreSQL, released together by a barrier."""

    databases = "__all__"

    def setUp(self):
        ConcurrencyTests.setUp(self)

    def together(self, jobs):
        with self.assertNoLogs("core.api", "WARNING"):
            return ConcurrencyTests.run_together(self, jobs)

    def test_two_registrations_of_one_own_number_make_one_file(self):
        for round_ in range(4):
            phone = f"0115555000{round_}"
            results = self.together([
                lambda c=c, n=n: call(c, "POST", "/api/v1/patients", new_patient(n, phone=phone))
                for c, n in zip([self.a.client("assistant") for _ in range(2)], ("Mona Samir Ali", "Sara Samir Ali"))])
            self.assertEqual(sorted(r.status_code for r in results), [201, 409], [r.content for r in results])
            self.assertEqual([r.json()["error"]["code"] for r in results if r.status_code == 409], ["phone_taken"])
        with clinic_transaction(self.a.id, self.a.shard):
            from patients.models import Patient

            self.assertEqual(Patient.objects.filter(clinic_id=self.a.id).count(), 4)

    def test_eight_desks_booking_one_patient_with_one_doctor_book_once(self):
        desk = self.a.client("assistant")
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()
        patient = call(desk, "POST", "/api/v1/appointments", {
            "doctor_id": self.owner_id, "service_id": self.services["consultation"].id, "day": tomorrow,
            "new_patient": new_patient("Race Same Day")}).json()["appointment"]["patient"]["id"]
        jobs = [lambda c=c: call(c, "POST", "/api/v1/appointments", {
                    "doctor_id": self.owner_id, "service_id": self.services["consultation"].id, "patient_id": patient})
                for c in [self.a.client("assistant") for _ in range(4)]]
        jobs += [lambda c=c: call(c, "POST", "/api/v1/appointments/walk-in", {"patient_id": patient,
                                                                             "doctor_id": self.owner_id})
                 for c in [self.a.client("assistant") for _ in range(4)]]
        results = self.together(jobs)
        self.assertEqual(sorted(r.status_code for r in results), [201] + [409] * 7, [r.content for r in results])
        self.assertTrue(all(r.json()["error"]["code"] == "already_booked" for r in results if r.status_code == 409))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Appointment.objects.filter(clinic_id=self.a.id, patient_id=patient, day=today(self.a)).count(), 1)
        _, doctors = summaries(self.a, today(self.a))
        self.assertEqual(doctors[self.owner_id]["booked"], 1)

    def test_two_taps_of_one_procedure_record_it_once(self):
        owner = self.a.client("owner")
        proc = next(p for p in call(owner, "GET", "/api/v1/procedures").json()["items"]
                    if p["name_en"] == "Scaling and polishing")
        visit = call(owner, "POST", "/api/v1/appointments/walk-in", {
            "doctor_id": self.owner_id, "new_patient": new_patient("Procedure Race"),
            "service_id": self.services["procedure"].id}).json()["appointment"]
        self.assertEqual(call(owner, "POST", f"/api/v1/appointments/{visit['id']}/call", {"status": "waiting"}).status_code, 200)
        results = self.together([lambda c=c: call(c, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                                                  {"procedure_id": proc["id"]})
                                 for c in [self.a.client("owner") for _ in range(2)]])
        self.assertEqual(sorted(r.status_code for r in results), [201, 409], [r.content for r in results])
        from billing.models import InvoiceLine

        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(InvoiceLine.objects.filter(clinic_id=self.a.id, kind="procedure").count(), 1)
