"""Booking with a calendar and times (Saged, 2026-11-13): queue sessions offer times at the clinic's step, the month
hints, a booking is "booked" in the feed and its own list, and the row shows the wait, the turn and old debt clearly."""

import datetime as dt
from zoneinfo import ZoneInfo

from django.template.loader import render_to_string
from django.utils import translation

from clinics.models import Clinic
from core.routing import clinic_transaction
from core.testing import call
from web import format as fmt

from .testing import M1TestCase, add_schedule, frozen, today


class QueueTimesTests(M1TestCase):
    def setUp(self):
        super().setUp()
        self.day = today(self.a) + dt.timedelta(days=1)
        self.owner_id = self.a.membership("owner").id

    def slots(self, doctor=None):
        return call(self.desk, "GET", f"/api/v1/doctors/{doctor or self.owner_id}/slots?day={self.day}").json()

    def test_a_queue_session_offers_hourly_times_and_next_turn(self):
        found = self.slots()
        self.assertTrue(found["queue"])
        self.assertEqual(len(found["slots"]), 14)  # 09:00 to 23:00, every hour
        self.assertEqual(found["slots"][0]["starts_at"], self.slot_iso(self.a, self.day, "09:00"))
        self.assertFalse(found["slots"][0]["passed"])

    def test_the_step_is_a_clinic_setting(self):
        with clinic_transaction(self.a.id, self.a.shard):
            clinic = Clinic.objects.get(id=self.a.id)
            clinic.settings = {**clinic.settings, "booking_step_minutes": 30}
            clinic.save(update_fields=["settings"])
        self.assertEqual(len(self.slots()["slots"]), 28)
        # A slot session keeps its own slot length.
        self.assertEqual(len(self.slots(self.a.membership("doctor").id)["slots"]), 12)

    def test_a_queue_time_is_held_once_extra_and_next_turn_still_work(self):
        at = self.slot_iso(self.a, self.day, "12:00")
        first = self.book(day=self.day.isoformat(), starts_at=at).json()["appointment"]
        self.assertEqual((first["status"], first["starts_at"]), ("booked", at))
        r = self.book(day=self.day.isoformat(), starts_at=at, new_patient={"full_name": "Second"}, expect=409)
        self.assertEqual(r.json()["error"]["code"], "slot_taken")
        self.book(day=self.day.isoformat(), starts_at=at, extra=True, new_patient={"full_name": "Extra"})
        self.book(day=self.day.isoformat(), new_patient={"full_name": "Next turn"})  # no time, a queue turn
        taken = [s for s in self.slots()["slots"] if s["taken"]]
        self.assertEqual([s["starts_at"] for s in taken], [at])
        r = self.book(day=self.day.isoformat(), starts_at=self.slot_iso(self.a, self.day, "12:30"), expect=400)
        self.assertEqual(r.json()["error"]["code"], "off_grid")

    def test_times_that_have_ended_are_marked_passed(self):
        day = today(self.a)
        noon = dt.datetime.combine(day, dt.time(12, 30), tzinfo=ZoneInfo("Africa/Cairo"))
        with frozen(noon):
            found = call(self.desk, "GET", f"/api/v1/doctors/{self.owner_id}/slots?day={day}").json()["slots"]
        self.assertEqual([s["passed"] for s in found[:4]], [True, True, True, False])  # 9, 10, 11 ended; 12 runs


class MonthTests(M1TestCase):
    def month(self, doctor, month, expect=200):
        r = call(self.desk, "GET", f"/api/v1/doctors/{doctor}/month?month={month}")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def test_working_days_free_times_and_days_off(self):
        doctor = self.a.membership("doctor").id
        first = today(self.a).replace(day=1) + dt.timedelta(days=40)
        first = first.replace(day=1)  # next month: every day is bookable
        found = {d["day"]: d for d in self.month(doctor, first.strftime("%Y-%m"))["days"]}
        some = first.isoformat()
        self.assertEqual((found[some]["works"], found[some]["free"], found[some]["full"]), (True, 12, False))
        self.book(doctor_id=doctor, day=some, starts_at=self.slot_iso(self.a, first, "10:00"))
        found = {d["day"]: d for d in self.month(doctor, first.strftime("%Y-%m"))["days"]}
        self.assertEqual(found[some]["free"], 11)

    def test_days_off_past_days_and_bad_input(self):
        add_schedule(self.b, "doctor", weekdays=[0])  # B's doctor works Mondays only
        doctor_b = self.b.membership("doctor").id
        r = call(self.b.client("assistant"), "GET",
                 f"/api/v1/doctors/{doctor_b}/month?month={today(self.b).strftime('%Y-%m')}").json()
        for d in r["days"]:
            day = dt.date.fromisoformat(d["day"])
            self.assertEqual(d["works"], day.weekday() == 0 and day >= today(self.b), d)
        self.month(self.a.membership("owner").id, "2026-13", expect=400)
        self.month(doctor_b, today(self.a).strftime("%Y-%m"), expect=404)  # another clinic's doctor

    def test_a_year_ahead_is_the_limit(self):
        later = today(self.a) + dt.timedelta(days=400)
        days = self.month(self.a.membership("owner").id, later.strftime("%Y-%m"))["days"]
        self.assertFalse(any(d["works"] for d in days if d["day"] >= later.isoformat()))


class FeedAndRowTests(M1TestCase):
    def test_a_booking_is_booked_in_the_feed_and_never_arrived(self):
        day = today(self.a)
        at = self.slot_iso(self.a, day, "22:00")
        self.book(day=day.isoformat(), starts_at=at, new_patient={"full_name": "Later"})
        self.book(arrive=True, new_patient={"full_name": "Here now"})
        recent = call(self.desk, "GET", "/api/v1/today").json()["recent"]
        later = [(e["action"], e["starts_at"]) for e in recent if e["patient_name"] == "Later"]
        here = [e["action"] for e in recent if e["patient_name"] == "Here now"]
        self.assertEqual(later, [("booked", at)])
        self.assertEqual(here, ["arrived"])
        with translation.override("ar"):
            html = render_to_string("web/_activity.html", {"recent": recent})
        self.assertIn("حجز:", html)

    def test_wait_text(self):
        with translation.override("en"):
            self.assertEqual([fmt.wait_text(m) for m in (0, 25, 60, 80, 125, 180, 300)],
                             ["under a minute", "25 min", "1 h", "1 h 20 min", "2 h 5 min", "3 h", "over 4 h"])
        with translation.override("ar"):
            self.assertEqual(fmt.wait_text(80), "ساعة و20 د")


def row(status, **extra):
    a = {"id": 1, "status": status, "patient": {"id": 2, "name": "Mona", "phone": "01012345678", "file_no": 3},
         "doctor_id": 4, "section_id": 5, "due": 300_00, "fee": 300_00, "invoice": None, "day": "2026-11-13",
         "queue_no": 10, "starts_at": None, "service": {"name": "Consultation"}, "earlier": 300_00, "started": True,
         "auto_closed": False, "arrived_at": None, **extra}
    perms = {"book": True, "queue": True, "view_patients": True, "collect": True}
    with translation.override("ar"):
        return render_to_string("web/_row.html", {"a": a, "perms": perms, "is_today": True, "features": {}})


class RowClarityTests(M1TestCase):
    def test_a_no_show_row_is_minimal(self):
        html = row("no_show", starts_at="2026-11-13T15:00:00+02:00", arrived_at="2026-11-13T06:41:00+02:00")
        self.assertIn("لم يحضر", html)
        self.assertIn('data-act="undo-no-show"', html)
        for gone in ("مطلوب", "متبقي من زيارات سابقة", "data-wait-since", "الدور", "data-act=\"pay\""):
            self.assertNotIn(gone, html)

    def test_the_wait_shows_only_while_waiting_or_ready(self):
        self.assertIn("data-wait-since", row("waiting", arrived_at="2026-11-13T10:00:00+02:00", wait="25 د"))
        self.assertIn("data-wait-since", row("ready", arrived_at="2026-11-13T10:00:00+02:00", wait="25 د"))
        for status in ("booked", "done", "in_room"):
            self.assertNotIn("data-wait-since", row(status, arrived_at="2026-11-13T10:00:00+02:00"))

    def test_the_turn_shows_only_without_a_time_and_old_debt_says_so(self):
        timed = row("booked", starts_at="2026-11-13T15:00:00+02:00")
        self.assertNotIn("الدور", timed)
        self.assertIn("الدور", row("booked"))
        self.assertIn("متبقي من زيارات سابقة", timed)
        self.assertIn("حاجز", timed)

    def test_the_page_language_is_used_on_the_row(self):
        html = row("waiting", arrived_at="2026-11-13T10:00:00+02:00", wait="25 د")
        for english in ("Owes", "Waiting", "Turn", "Didn't come", "From earlier"):
            self.assertNotIn(english, html)
