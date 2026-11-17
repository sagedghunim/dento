"""Booking and Today in a center (design D1): the section comes from the service, Today filters by section."""

import datetime as dt
from zoneinfo import ZoneInfo

from clinics.models import Section, Service
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.testing import add_schedule, summaries, today

CENTER = [{"specialty": "dental"}, {"specialty": "pediatrics"}, {"specialty": "dermatology"}]


class CenterBookingTests(ShardTestCase):
    """A center on shard 2: the owner works dental (queue); the doctor works pediatrics (slots) and dermatology (queue)."""

    def setUp(self):
        super().setUp()
        self.c = self.clinic("c", "shard_2", sections=CENTER)
        self.a = self.clinic("a", "shard_1")
        with clinic_transaction(self.c.id, self.c.shard):
            self.dental, self.kids, self.skin = Section.objects.filter(clinic_id=self.c.id).order_by("sort", "id")
            self.services = {}
            for s in Service.objects.filter(clinic_id=self.c.id).order_by("-sort", "-id"):
                self.services[s.section_id, s.kind] = s  # the first of each kind per section wins
        self.owner, self.doctor = self.c.membership("owner").id, self.c.membership("doctor").id
        add_schedule(self.c, "owner", section=self.dental.id, starts="09:00", ends="23:00")
        add_schedule(self.c, "doctor", section=self.kids.id, starts="10:00", ends="14:00", mode="slots", slot_minutes=20)
        add_schedule(self.c, "doctor", section=self.skin.id, starts="16:00", ends="22:00")
        self.desk = self.c.client("assistant")
        self.day = today(self.c)

    def book(self, section, doctor, expect=201, **body):
        body = {"doctor_id": doctor, "service_id": self.services[section.id, "consultation"].id,
                "new_patient": new_patient(full_name="Center Patient"), **body}
        r = call(self.desk, "POST", "/api/v1/appointments", body)
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def slot(self, hhmm, day=None):
        return dt.datetime.combine(day or self.day, dt.time.fromisoformat(hhmm), tzinfo=ZoneInfo("Africa/Cairo")).isoformat()

    def test_the_booking_takes_its_section_from_the_service(self):
        dental = self.book(self.dental, self.owner)["appointment"]
        skin = self.book(self.skin, self.doctor)["appointment"]
        kids = self.book(self.kids, self.doctor, starts_at=self.slot("10:20"))["appointment"]
        self.assertEqual((dental["section_id"], skin["section_id"], kids["section_id"]),
                         (self.dental.id, self.skin.id, self.kids.id))
        # One doctor-day across sections: the doctor's turns run on.
        self.assertEqual((skin["queue_no"], kids["queue_no"]), (1, 2))
        read = call(self.desk, "GET", f"/api/v1/appointments/{kids['id']}").json()["appointment"]
        self.assertEqual(read["section_id"], self.kids.id)

    def test_a_doctor_without_a_session_in_the_services_section_is_refused(self):
        before = summaries(self.c, self.day)
        r = self.book(self.skin, self.owner, expect=409)
        self.assertEqual((r["error"]["code"], r["error"]["fields"]), ("wrong_section", {"doctor_id": "wrong_section"}))
        # The pediatrics grid isn't offered for a dermatology service, and a queue turn isn't offered in pediatrics.
        r = self.book(self.skin, self.doctor, expect=400, starts_at=self.slot("10:20"))
        self.assertEqual(r["error"]["code"], "off_grid")
        r = self.book(self.kids, self.doctor, expect=400)
        self.assertEqual(r["error"]["code"], "time_required")
        self.assertEqual(summaries(self.c, self.day), before)

    def test_quote_and_slots_follow_the_section(self):
        q = call(self.desk, "GET", f"/api/v1/appointments/quote?doctor={self.owner}"
                                   f"&service={self.services[self.skin.id, 'consultation'].id}").json()
        self.assertEqual((q["doctor_works"], q["section_id"]), (False, self.skin.id))
        q = call(self.desk, "GET", f"/api/v1/appointments/quote?doctor={self.doctor}"
                                   f"&service={self.services[self.skin.id, 'consultation'].id}").json()
        self.assertTrue(q["doctor_works"])
        kids = call(self.desk, "GET", f"/api/v1/doctors/{self.doctor}/slots?section={self.kids.id}").json()
        skin = call(self.desk, "GET", f"/api/v1/doctors/{self.doctor}/slots?section={self.skin.id}").json()
        self.assertEqual((len(kids["slots"]), kids["queue"]), (12, False))
        # A queue session offers times too, at the clinic's booking step (Saged, 2026-11-13), and keeps "next turn".
        self.assertEqual(skin["queue"], True)
        self.assertGreater(len(skin["slots"]), 0)
        with clinic_transaction(self.a.id, self.a.shard):
            foreign = Section.objects.get(clinic_id=self.a.id).id
        self.assertEqual(call(self.desk, "GET", f"/api/v1/doctors/{self.doctor}/slots?section={foreign}").status_code, 404)

    def test_reschedule_keeps_the_section(self):
        skin = self.book(self.skin, self.doctor)["appointment"]
        tomorrow = (self.day + dt.timedelta(days=1)).isoformat()
        r = call(self.desk, "POST", f"/api/v1/appointments/{skin['id']}/reschedule",
                 {"status": "booked", "doctor_id": self.owner, "day": tomorrow})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "wrong_section"))
        moved = call(self.desk, "POST", f"/api/v1/appointments/{skin['id']}/reschedule",
                     {"status": "booked", "doctor_id": self.doctor, "day": tomorrow}).json()["appointment"]
        self.assertEqual((moved["day"], moved["section_id"]), (tomorrow, self.skin.id))

    def test_today_carries_the_sections_and_filters_by_one(self):
        dental = self.book(self.dental, self.owner)["appointment"]
        skin = self.book(self.skin, self.doctor)["appointment"]
        full = call(self.desk, "GET", "/api/v1/today")
        body = full.json()
        self.assertEqual(body["kind"], "center")
        self.assertEqual([s["id"] for s in body["sections"]], [self.dental.id, self.kids.id, self.skin.id])
        self.assertIsNone(body["section_id"])
        rows = [a for q in body["queues"] for a in q["appointments"]]
        self.assertEqual({a["id"]: a["section_id"] for a in rows}, {dental["id"]: self.dental.id, skin["id"]: self.skin.id})
        doctor_queue = next(q for q in body["queues"] if q["doctor"]["id"] == self.doctor)
        self.assertEqual({s["section_id"] for s in doctor_queue["doctor"]["sessions"]}, {self.kids.id, self.skin.id})

        only = call(self.desk, "GET", f"/api/v1/today?section={self.skin.id}")
        self.assertNotEqual(only["ETag"], full["ETag"])  # the filter is part of the ETag
        self.assertTrue(only["ETag"].startswith(f'"{self.c.id}.{self.day.isoformat()}.'))
        body = only.json()
        self.assertEqual(body["section_id"], self.skin.id)
        self.assertEqual([a["id"] for q in body["queues"] for a in q["appointments"]], [skin["id"]])
        self.assertEqual([q["doctor"]["id"] for q in body["queues"]], [self.doctor])
        self.assertEqual({s["section_id"] for s in body["queues"][0]["doctor"]["sessions"]}, {self.skin.id})
        same = self.desk.get(f"/api/v1/today?section={self.skin.id}",
                             headers={"X-Membership": str(self.desk.membership_id), "If-None-Match": only["ETag"]})
        self.assertEqual(same.status_code, 304)
        other = self.desk.get(f"/api/v1/today?section={self.kids.id}",
                              headers={"X-Membership": str(self.desk.membership_id), "If-None-Match": only["ETag"]})
        self.assertEqual(other.status_code, 200)

    def test_today_refuses_another_places_section_and_every_refusal_carries_poll_after(self):
        with clinic_transaction(self.a.id, self.a.shard):
            foreign = Section.objects.get(clinic_id=self.a.id).id
        r = call(self.desk, "GET", f"/api/v1/today?section={foreign}")
        self.assertEqual((r.status_code, r["Poll-After"]), (404, "5"))
        r = call(self.desk, "GET", "/api/v1/today?section=abc")
        self.assertEqual((r.status_code, r["Poll-After"]), (400, "5"))
        r = call(self.desk, "GET", "/api/v1/today", membership=self.a.membership("assistant").id)  # another tab's clinic
        self.assertEqual((r.status_code, r.json()["error"]["code"], r["Poll-After"]), (409, "clinic_changed", "5"))
        from django.test import Client

        r = call(Client(), "GET", "/api/v1/today")
        self.assertEqual((r.status_code, r["Poll-After"]), (401, "5"))
        from clinics.models import Membership

        with clinic_transaction(self.c.id, self.c.shard):
            Membership.objects.filter(clinic_id=self.c.id, id=self.c.membership("assistant").id).update(
                revoked=["appointments.book", "appointments.queue"])
        r = call(self.desk, "GET", "/api/v1/today")
        self.assertEqual((r.status_code, r["Poll-After"]), (403, "5"))
        # Other endpoints don't carry it.
        self.assertNotIn("Poll-After", call(self.desk, "GET", "/api/v1/clinic"))
