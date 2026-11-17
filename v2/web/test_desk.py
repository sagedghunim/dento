"""The front desk review (docs/v2/reviews/desk-workflow.md): every status's pill and actions, the queue order and
the folded finished rows, the earlier balance, "didn't come" by hand with its undo, and tappable phones."""

import datetime as dt
import re
from zoneinfo import ZoneInfo

from django.template.loader import render_to_string
from django.utils import translation

from audit.models import AuditChange
from clinical.testing import M2TestCase
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import call, new_patient
from scheduling.models import STATUSES, Appointment
from scheduling.testing import frozen, today
from summaries.models import DoctorDay
from web import format as fmt
from web.tests import fragment

PERMS = {"book": True, "queue": True, "view_patients": True, "collect": True, "finances": False}


def row(status, **extra):
    a = {"id": 1, "status": status, "patient": {"id": 2, "name": "Mona", "phone": "01012345678", "file_no": 3},
         "doctor_id": 4, "section_id": 5, "due": 0, "fee": 100_00, "invoice": None, "day": "2026-11-13", "queue_no": 1,
         "starts_at": None, "service": {"name": "Consultation"}, "earlier": 0, "started": True, "auto_closed": False,
         "arrived_at": None, **extra}
    with translation.override("en"):
        return render_to_string("web/_row.html", {"a": a, "perms": PERMS, "is_today": True, "features": {}})


def acts(html):
    return re.findall(r'data-act="([\w-]+)"', html)


class RowTemplateTests(M2TestCase):
    def test_every_status_has_its_own_pill_and_only_no_show_says_didnt_come(self):
        pills = {"booked": "Booked", "waiting": "Waiting", "with_assistant": "With the assistant",
                 "ready": "Ready for the doctor", "in_room": "With the doctor", "done": "Done", "cancelled": "Cancelled",
                 "no_show": "Didn't come"}
        self.assertEqual(sorted(pills), sorted(STATUSES))
        for status in STATUSES:
            html = row(status)
            side = html.split('class="appt-side"')[1].split('class="appt-actions"')[0]
            self.assertIn(pills[status], side, status)
            self.assertEqual("Didn't come" in side, status == "no_show", status)

    def test_each_status_offers_its_next_step(self):
        self.assertIn("arrive", acts(row("booked")))
        self.assertIn("call", acts(row("waiting")))
        self.assertIn("call", acts(row("ready")))  # the bug: ready showed no call-in
        self.assertIn("ready", acts(row("with_assistant")))
        self.assertIn("finish", acts(row("in_room")))
        self.assertIn("undo-no-show", acts(row("no_show")))
        self.assertNotIn("undo-no-show", acts(row("no_show", auto_closed=True)))  # the night's sweep stays

    def test_the_activity_feed_names_every_stage(self):
        events = [{"at": "2026-11-13T10:00:00+02:00", "action": action, "patient_name": "Mona", "doctor": ""}
                  for action in ("with_assistant", "ready")]
        with translation.override("en"):
            html = render_to_string("web/_activity.html", {"recent": events})
        self.assertIn("With the assistant:", html)
        self.assertIn("Ready for the doctor:", html)
        self.assertNotIn("cancelled", html)

    def test_next_visit_and_didnt_come_in_the_menu(self):
        self.assertIn("next", acts(row("done")))
        self.assertNotIn("next", acts(row("booked")))
        self.assertIn("no-show", acts(row("booked")))
        self.assertIn('data-act="no-show" data-after="" hidden', row("booked", started=False))

    def test_phones_call_and_whatsapp(self):
        html = row("waiting")
        self.assertIn('href="tel:01012345678"', html)
        self.assertIn('href="https://wa.me/201012345678"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_whatsapp_numbers(self):
        for given, wanted in (("01012345678", "201012345678"), ("+201012345678", "201012345678"),
                              ("00201012345678", "201012345678"), ("1012345678", "201012345678"),
                              ("٠١٠١٢٣٤٥٦٧٨", "201012345678"), ("+966501234567", "966501234567"),
                              ("0223456789", ""), ("", ""), (None, "")):
            self.assertEqual(fmt.whatsapp(given), wanted, given)


class QueueOrderTests(M2TestCase):
    def ids_in(self, html, name):
        part = html.split(f'data-rows="{name}"')[1].split("</ol>")[0]
        return [int(i) for i in re.findall(r'<li class="appt [\w]+" data-id="(\d+)"', part)]

    def test_who_needs_the_desk_comes_first_and_finished_and_paid_rows_fold(self):
        owner = self.a.membership("owner").id

        def visit(**body):
            return self.book(doctor_id=owner, new_patient=new_patient(full_name=f"P {len(self.made)}"), **body).json()["appointment"]["id"]

        self.made = []
        paid = visit(arrive=True, payment={"amount": 1, "method": "cash"})
        self.made.append(paid)
        self.act(paid, "call", "waiting")
        fee = self.appointment(paid).fee
        self.act(paid, "finish", "in_room", payment={"amount": fee - 1, "method": "cash"})
        owing = visit(arrive=True)
        self.made.append(owing)
        self.act(owing, "call", "waiting")
        self.act(owing, "finish", "in_room")
        booked = visit()
        self.made.append(booked)
        waiting = visit(arrive=True)
        self.made.append(waiting)
        ready = visit(arrive=True)
        self.made.append(ready)
        self.act(ready, "ready", "waiting")
        in_room = visit(arrive=True)
        self.act(in_room, "call", "waiting")
        html = fragment(self.desk).content.decode()
        self.assertEqual(self.ids_in(html, "open"), [in_room, ready, waiting, owing])
        self.assertEqual(self.ids_in(html, "booked"), [booked])  # never among the waiting (Saged, 2026-11-13)
        self.assertEqual(self.ids_in(html, "done"), [paid])
        self.assertIn('<span data-fold-count>1</span>', html)
        self.assertIn("خلصوا", html)


class EarlierBalanceTests(M2TestCase):
    def setUp(self):
        super().setUp()
        old = self.owed_visit()
        self.patient = old["patient"]["id"]
        self.fee = self.appointment(old["id"]).fee
        with clinic_transaction(self.a.id, self.a.shard):  # the owed visit was yesterday
            Appointment.objects.filter(clinic_id=self.a.id, id=old["id"]).update(
                day=today(self.a) - dt.timedelta(days=1))
        self.visit = self.in_room(self.patient)["id"]

    def test_the_row_and_the_search_show_what_is_owed_from_before(self):
        html = fragment(self.desk).content.decode()
        self.assertIn(f'data-earlier="{self.fee}"', html)
        self.assertIn("متبقي من زيارات سابقة", html)
        found = fragment(self.desk, "/patients/search?pick=1&q=Mohamed").content.decode()
        self.assertIn(f'data-owed="{self.fee}"', found)

    def test_only_who_sees_money_sees_it(self):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["money.view"])  # who sees money: money.view (Saged, 2026-11-16)
        self.assertIn('data-earlier="0"', fragment(self.desk).content.decode())
        self.assertNotIn("باقي من قبل", fragment(self.desk).content.decode())
        self.assertIn('data-owed="0"', fragment(self.desk, "/patients/search?pick=1&q=Mohamed").content.decode())

    def test_finishing_collects_it_in_the_same_request(self):
        r = self.act(self.visit, "finish", "in_room", earlier={"amount": self.fee, "method": "cash"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["balance"]["owed"], self.fee)  # only today's visit is left owing
        self.assertIn('data-earlier="0"', fragment(self.desk).content.decode())


def cairo(day, hhmm):
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), tzinfo=ZoneInfo("Africa/Cairo"))


class NoShowTests(M2TestCase):
    def no_shows(self, doctor_id):
        with clinic_transaction(self.a.id, self.a.shard):
            return DoctorDay.objects.get(clinic_id=self.a.id, day=today(self.a), doctor_id=doctor_id).no_shows

    def test_a_queue_booking_is_marked_logged_counted_and_undone(self):
        appt = self.book().json()["appointment"]
        owner = self.a.membership("owner").id
        r = self.act(appt["id"], "no-show", "booked")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["appointment"]["status"], "no_show")
        self.assertEqual(self.no_shows(owner), 1)
        self.assertEqual(self.act(appt["id"], "no-show", "booked").status_code, 200)  # a repeat changes nothing
        self.assertEqual(self.no_shows(owner), 1)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditChange.objects.filter(clinic_id=self.a.id, action="appointment.no_show",
                                                        target_id=appt["id"]).count(), 1)
        r = self.act(appt["id"], "undo-no-show", "no_show")
        self.assertEqual((r.status_code, r.json()["appointment"]["status"]), (200, "booked"))
        self.assertEqual(self.no_shows(owner), 0)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditChange.objects.filter(clinic_id=self.a.id, action="appointment.undo_no_show").count(), 1)
        self.assertEqual(self.act(appt["id"], "arrive", "booked").status_code, 200)  # and can still arrive

    def test_only_a_booked_visit_of_today_whose_time_has_passed(self):
        day = today(self.a)
        doctor = self.a.membership("doctor").id
        with frozen(cairo(day, "09:00")):
            slot = self.book(doctor_id=doctor, starts_at=self.slot_iso(self.a, day, "13:40")).json()["appointment"]
            r = self.act(slot["id"], "no-show", "booked")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_yet"))
        with frozen(cairo(day, "13:45")):
            self.assertEqual(self.act(slot["id"], "no-show", "booked").status_code, 200)
        with frozen(cairo(day, "09:05")):  # the freed slot is booked again, so the undo can't take it back
            self.book(doctor_id=doctor, starts_at=self.slot_iso(self.a, day, "13:40"),
                      new_patient=new_patient(full_name="Second Patient"))
            r = self.act(slot["id"], "undo-no-show", "no_show")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "slot_taken"))
        tomorrow = self.book(day=(day + dt.timedelta(days=1)).isoformat()).json()["appointment"]
        r = self.act(tomorrow["id"], "no-show", "booked")
        self.assertEqual(r.status_code, 409, r.content)
        waiting = self.book(arrive=True).json()["appointment"]
        r = self.act(waiting["id"], "no-show", "booked")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "status_changed"))
        self.assertEqual(self.act(waiting["id"], "no-show", "waiting").status_code, 400)

    def test_a_doctor_cant_mark_another_doctors_visit(self):
        appt = self.book().json()["appointment"]  # the owner's queue
        r = call(self.doc, "POST", f"/api/v1/appointments/{appt['id']}/no-show", {"status": "booked"})
        self.assertEqual(r.status_code, 404)


class DialogDefaultTests(M2TestCase):
    def test_a_booking_starts_unticked_and_a_walk_in_collects(self):
        html = self.desk.get("/").content.decode()
        book = html.split('id="book-dialog"')[1].split("</dialog>")[0]
        self.assertIn('name="arrive">', book)
        self.assertIn('name="collect">', book)
        walkin = html.split('id="walkin-dialog"')[1].split("</dialog>")[0]
        self.assertIn('name="collect" checked>', walkin)
