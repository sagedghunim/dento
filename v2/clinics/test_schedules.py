"""Session endpoints (design 5.1, D2), guarded by settings.manage, and the day settings' directory copy (I6)."""

import datetime as dt
import uuid

from audit.models import AuditChange
from clinics.models import Clinic, Membership, Schedule, Section
from core.routing import clinic_transaction
from core.testing import call
from directory.models import DirectoryClinic
from scheduling.testing import M1TestCase, summaries, today


class ScheduleTests(M1TestCase):
    def setUp(self):
        super().setUp()
        with clinic_transaction(self.a.id, self.a.shard):
            self.section = Section.objects.get(clinic_id=self.a.id).id
        self.doctor = self.a.membership("doctor").id
        self.weekday = (today(self.a) + dt.timedelta(days=1)).weekday()

    def session(self, **changes):
        body = {"doctor_id": self.doctor, "section_id": self.section, "weekday": self.weekday, "starts": "16:00",
                "ends": "20:00", "mode": "queue"}
        body.update(changes)
        return body

    def doctors_session(self, weekday=None):
        """The slot doctor's session (10:00-14:00, 20-minute slots) on that weekday, as GET answers it."""
        items = call(self.owner, "GET", f"/api/v1/schedules?doctor={self.doctor}").json()["items"]
        return next(s for s in items if s["weekday"] == (self.weekday if weekday is None else weekday))

    def test_create_list_update_and_delete(self):
        version = summaries(self.a, today(self.a))[0]
        r = call(self.owner, "POST", "/api/v1/schedules", self.session(max_bookings=12))
        self.assertEqual(r.status_code, 201, r.content)
        created = r.json()["schedule"]
        self.assertEqual({k: created[k] for k in ("doctor_id", "section_id", "weekday", "starts", "ends", "mode",
                                                  "slot_minutes", "max_bookings")},
                         {**self.session(), "slot_minutes": None, "max_bookings": 12})
        self.assertEqual(r.json()["version"], (version["version"] if version else 0) + 1)  # today's version moves
        listed = call(self.owner, "GET", f"/api/v1/schedules?section={self.section}&doctor={self.doctor}").json()["items"]
        self.assertIn(created, listed)
        updated = call(self.owner, "PUT", f"/api/v1/schedules/{created['id']}",
                       {**self.session(ends="21:00", mode="slots", slot_minutes=15), "expected": created})
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual((updated.json()["schedule"]["ends"], updated.json()["schedule"]["slot_minutes"]), ("21:00", 15))
        gone = call(self.owner, "DELETE", f"/api/v1/schedules/{created['id']}")
        self.assertEqual((gone.status_code, gone.json()["deleted"]), (200, created["id"]))
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/schedules/{created['id']}").status_code, 404)
        with clinic_transaction(self.a.id, self.a.shard):
            actions = list(AuditChange.objects.filter(clinic_id=self.a.id, target_type="schedule").order_by("id")
                           .values_list("action", flat=True))
        self.assertEqual(actions, ["schedule.create", "schedule.update", "schedule.delete"])

    def test_bad_input_is_a_clear_400(self):
        for changes in ({"weekday": 7}, {"weekday": "x"}, {"starts": "4pm"}, {"starts": "16:00:30"}, {"ends": "16:00"},
                        {"mode": "walk-in"}, {"mode": "slots"}, {"mode": "slots", "slot_minutes": 3},
                        {"slot_minutes": 20}, {"max_bookings": 0}, {"doctor_id": "me"}, {"section_id": None},
                        {"starts": "23:00", "ends": "05:00"}):
            r = call(self.owner, "POST", "/api/v1/schedules", self.session(**changes))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid"), changes)
        # Past midnight is fine while the session ends before the day does (04:00).
        r = call(self.owner, "POST", "/api/v1/schedules", self.session(starts="22:00", ends="02:00"))
        self.assertEqual(r.status_code, 201, r.content)

    def test_a_doctor_cant_be_in_two_sessions_at_once(self):
        # The slot doctor works 10:00-14:00 every day.
        r = call(self.owner, "POST", "/api/v1/schedules", self.session(starts="13:00", ends="15:00"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "session_overlap"))
        self.assertEqual(call(self.owner, "POST", "/api/v1/schedules", self.session(starts="14:00", ends="15:00")).status_code,
                         201)

    def test_another_places_doctor_or_section_or_session_is_404(self):
        with clinic_transaction(self.b.id, self.b.shard):
            b_section = Section.objects.get(clinic_id=self.b.id).id
            b_session = Schedule.objects.filter(clinic_id=self.b.id).first().id
        for changes in ({"doctor_id": self.b.membership("owner").id}, {"section_id": b_section},
                        {"doctor_id": self.a.membership("assistant").id}):
            self.assertEqual(call(self.owner, "POST", "/api/v1/schedules", self.session(**changes)).status_code, 404,
                             changes)
        mine = self.doctors_session()
        self.assertEqual(call(self.owner, "PUT", f"/api/v1/schedules/{b_session}",
                              {**self.session(), "expected": mine}).status_code, 404)
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/schedules/{b_session}").status_code, 404)
        self.assertEqual(call(self.owner, "PUT", f"/api/v1/schedules/{mine['id']}",
                              {**self.session(section_id=b_section), "expected": mine}).status_code, 404)
        listed = call(self.owner, "GET", "/api/v1/schedules").json()["items"]
        self.assertNotIn(b_session, [s["id"] for s in listed])
        with clinic_transaction(self.b.id, self.b.shard):
            self.assertTrue(Schedule.objects.filter(clinic_id=self.b.id, id=b_session).exists())

    def test_every_method_needs_settings_manage(self):
        mine = self.doctors_session()
        requests = [("GET", "/api/v1/schedules", None), ("POST", "/api/v1/schedules", self.session()),
                    ("PUT", f"/api/v1/schedules/{mine['id']}", {**self.session(), "expected": mine}),
                    ("DELETE", f"/api/v1/schedules/{mine['id']}", None)]
        for method, path, body in requests:
            self.assertEqual(call(self.desk, method, path, body).status_code, 403, method)
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                granted=["settings.manage"])
        self.assertEqual(call(self.desk, "GET", "/api/v1/schedules").status_code, 200)
        self.assertEqual(call(self.desk, "POST", "/api/v1/schedules", self.session()).status_code, 201)

    def test_a_stale_expected_value_is_409_with_the_current_session(self):
        mine = self.doctors_session()
        r = call(self.owner, "PUT", f"/api/v1/schedules/{mine['id']}",
                 {**self.session(mode="slots", slot_minutes=20, starts="10:00", ends="15:00"),
                  "expected": {**mine, "ends": "13:00"}})
        self.assertEqual((r.status_code, r.json()["error"]["code"], r.json()["error"]["fields"]), (409, "stale", {"ends": "changed"}))
        self.assertEqual(r.json()["error"]["current"], mine)
        self.assertEqual(call(self.owner, "PUT", f"/api/v1/schedules/{mine['id']}", self.session()).status_code, 400)

    def test_a_change_that_would_strand_bookings_is_refused_with_the_list(self):
        tomorrow = today(self.a) + dt.timedelta(days=1)
        booked = self.book(doctor_id=self.doctor, day=tomorrow.isoformat(),
                           starts_at=self.slot_iso(self.a, tomorrow, "10:20")).json()["appointment"]
        mine = self.doctors_session()
        cases = [("PUT", {**self.session(mode="slots", slot_minutes=30, starts="10:00", ends="14:00"), "expected": mine}),
                 ("PUT", {**self.session(mode="slots", slot_minutes=20, starts="11:00", ends="14:00"), "expected": mine}),
                 ("PUT", {**self.session(weekday=(self.weekday + 1) % 7, mode="slots", slot_minutes=20, starts="15:00",
                                         ends="19:00"), "expected": mine}),
                 ("DELETE", None)]
        for method, body in cases:
            r = call(self.owner, method, f"/api/v1/schedules/{mine['id']}", body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "schedule_conflict"), body)
            self.assertEqual(r.json()["error"]["count"], 1)
            self.assertEqual(r.json()["error"]["bookings"],
                             [{"id": booked["id"], "day": tomorrow.isoformat(), "queue_no": 1,
                               "starts_at": self.slot_iso(self.a, tomorrow, "10:20")}])
        self.assertEqual(self.doctors_session(), mine)  # nothing changed
        # A change that keeps the slot on the grid is fine (10:00-15:00 still has 10:20).
        r = call(self.owner, "PUT", f"/api/v1/schedules/{mine['id']}",
                 {**self.session(mode="slots", slot_minutes=20, starts="10:00", ends="15:00"), "expected": mine})
        self.assertEqual(r.status_code, 200, r.content)
        self.act(booked["id"], "cancel", "booked")
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/schedules/{mine['id']}").status_code, 200)

    def test_a_queue_booking_needs_a_queue_session_left_that_weekday(self):
        tomorrow = today(self.a) + dt.timedelta(days=1)
        self.book(day=tomorrow.isoformat())  # the owner's queue, 09:00-23:00
        owner_session = next(s for s in call(self.owner, "GET", f"/api/v1/schedules?doctor={self.a.membership('owner').id}")
                             .json()["items"] if s["weekday"] == self.weekday)
        r = call(self.owner, "DELETE", f"/api/v1/schedules/{owner_session['id']}")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "schedule_conflict"))
        # Shorter hours keep a queue session, so the turn is still covered.
        r = call(self.owner, "PUT", f"/api/v1/schedules/{owner_session['id']}",
                 {**owner_session, "ends": "21:00", "expected": owner_session})
        self.assertEqual(r.status_code, 200, r.content)

    def test_repeats_replay_and_a_reused_key_is_refused(self):
        key = uuid.uuid4()
        first = call(self.owner, "POST", "/api/v1/schedules", self.session(), key=key)
        again = call(self.owner, "POST", "/api/v1/schedules", self.session(), key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(first.json(), again.json())
        reused = call(self.owner, "POST", "/api/v1/schedules", self.session(ends="21:00"), key=key)
        self.assertEqual((reused.status_code, reused.json()["error"]["code"]), (422, "request_key_reused"))
        created = first.json()["schedule"]
        key = uuid.uuid4()
        gone = call(self.owner, "DELETE", f"/api/v1/schedules/{created['id']}", key=key)
        self.assertEqual(call(self.owner, "DELETE", f"/api/v1/schedules/{created['id']}", key=key).json(), gone.json())
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditChange.objects.filter(clinic_id=self.a.id, action="schedule.create").count(), 1)


class DaySettingsTests(M1TestCase):
    def put(self, changes, expected=None, client=None):
        return call(client or self.owner, "PUT", "/api/v1/settings/clinic", {"changes": changes, "expected": expected or {}})

    def test_the_time_zone_and_day_end_change_only_before_any_booking_and_reach_the_directory(self):
        with self.captureOnCommitCallbacks(using=self.b.shard, execute=True):
            r = self.put({"timezone": "Asia/Riyadh", "day_ends_at": "02:30"}, client=self.b.client("owner"))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["settings"]["timezone"], r.json()["settings"]["day_ends_at"]), ("Asia/Riyadh", "02:30"))
        entry = DirectoryClinic.objects.get(id=self.b.id)
        self.assertEqual((entry.timezone, entry.day_ends_at), ("Asia/Riyadh", dt.time(2, 30)))
        with clinic_transaction(self.b.id, self.b.shard):
            self.assertEqual(Clinic.objects.get(id=self.b.id).timezone, "Asia/Riyadh")
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.b.id, action="settings.change").exists())
        self.book()
        r = self.put({"day_ends_at": "03:00"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "has_bookings"))
        self.assertEqual(DirectoryClinic.objects.get(id=self.a.id).day_ends_at, dt.time(4, 0))
        # Other settings still change, and the same values again are no change at all.
        self.assertEqual(self.put({"follow_up_days": 7, "day_ends_at": "04:00"}).status_code, 200)

    def test_bad_or_stale_values(self):
        for changes in ({"timezone": "Mars/Base"}, {"timezone": ""}, {"timezone": 3}, {"day_ends_at": "07:00"},
                        {"day_ends_at": "4"}, {"day_ends_at": "04:00:30"}):
            r = self.put(changes)
            self.assertEqual(r.status_code, 400, changes)
        r = self.put({"day_ends_at": "03:00"}, expected={"day_ends_at": "05:00"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "stale"))
        self.assertEqual(self.put({"day_ends_at": "03:00"}, client=self.desk).status_code, 403)


class RouteTests(M1TestCase):
    def test_every_section_and_session_route_declares_its_permission(self):
        """Sections for anyone who can book, sessions for settings.manage (D1, D2); none is left unguarded."""
        from django.urls import get_resolver

        found = {}
        for pattern in get_resolver().url_patterns:
            if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") != "clinics.urls":
                continue
            for route in pattern.url_patterns:
                views = getattr(route.callback, "api_views", None) or {route.callback.api_meta["method"]: route.callback}
                for method, view in views.items():
                    found[method, str(route.pattern)] = view.api_meta["perm"]
        from clinics.test_team import RouteTests  # the full table, with the team and names routes (design G)

        self.assertEqual(found, RouteTests.EXPECTED)
