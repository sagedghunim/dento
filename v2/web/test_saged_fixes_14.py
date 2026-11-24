"""Saged's fixes, part 14 (2026-11-24).

- The day ends at a fixed time, whatever the drawer; whoever is still waiting or in the room moves to the new day,
  the moment anyone looks at it (no wait for the nightly sweep).
- The two-step code: "123 456" works; a code just used on another phone says so and costs no try.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

from django.test import Client

from accounts import totp
from clinical.testing import M2TestCase
from core.testing import ShardTestCase
from scheduling.testing import frozen, summaries


def cairo(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Africa/Cairo"))


def post(client, url, body):
    return client.post(url, json.dumps(body), content_type="application/json")


class CarryOverTests(M2TestCase):
    def test_last_nights_patient_in_the_room_is_on_the_new_day_with_a_new_turn(self):
        with frozen(cairo("2026-11-10T19:00")):
            visit = self.in_room()
        self.assertEqual(visit["day"], "2026-11-10")
        with frozen(cairo("2026-11-11T09:00")):
            r = self.doc.get("/")  # the doctor's first look at the new day: the visit is still theirs, in the room
            self.assertEqual((r.status_code, r["Location"]), (302, f"/visits/{visit['id']}"))
        row = self.appointment(visit["id"])
        self.assertEqual((row.status, str(row.day), row.queue_no), ("in_room", "2026-11-11", 1))
        doctor = self.a.membership("doctor").id
        self.assertEqual(summaries(self.a, dt.date(2026, 11, 10))[1][doctor]["booked"], 0)
        self.assertEqual(summaries(self.a, dt.date(2026, 11, 11))[1][doctor]["booked"], 1)

    def test_the_desk_sees_last_nights_waiting_patient_on_today_and_a_booking_stays_behind(self):
        with frozen(cairo("2026-11-10T19:00")):
            waiting = self.in_room()  # called in, then back to waiting is not a flow: finish the room, book a waiting one
            self.assertEqual(self.act(waiting["id"], "finish", "in_room", client=self.doc).status_code, 200)
            body = {"service_id": self.services_a["procedure"].id, "doctor_id": self.a.membership("doctor").id,
                    "new_patient": {"full_name": "Sara Ali Hassan", "phone": "01011112222", "phone_owner": "self",
                                    "sex": "f", "age": 30}}
            from core.testing import call

            r = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
            self.assertEqual(r.status_code, 201, r.content)
            left = r.json()["appointment"]
        with frozen(cairo("2026-11-11T04:05")):
            page = self.desk.get("/today")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Sara Ali Hassan", page.content.decode())
        row = self.appointment(left["id"])
        self.assertEqual((row.status, str(row.day)), ("waiting", "2026-11-11"))
        self.assertEqual(self.appointment(waiting["id"]).day, dt.date(2026, 11, 10))  # the finished one stays


class CodeStepTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.secret = self.a.user("owner").plain_totp_secret

    def sign_in(self):
        client = Client()
        r = post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        self.assertEqual(r.json()["next"], "code")
        return client

    def test_a_code_with_a_space_or_a_dash_works(self):
        code = totp.code_at(self.secret, totp.current_step())
        client = self.sign_in()
        self.assertEqual(post(client, "/api/v1/auth/code", {"code": f"{code[:3]} {code[3:]}"}).status_code, 200)
        self.assertEqual(totp.clean("١٢٣-٤٥٦"), "123456")

    def test_a_code_just_used_on_another_phone_says_so_and_costs_no_try(self):
        code = totp.code_at(self.secret, totp.current_step())
        self.assertEqual(post(self.sign_in(), "/api/v1/auth/code", {"code": code}).status_code, 200)
        other = self.sign_in()
        answers = [post(other, "/api/v1/auth/code", {"code": code}) for _ in range(6)]
        self.assertEqual({(r.status_code, r.json()["error"]["code"]) for r in answers}, {(400, "code_used")})
        # Still in the half sign-in: a wrong code is the first strike, not the sixth.
        self.assertEqual(post(other, "/api/v1/auth/code", {"code": "000000"}).json()["error"]["code"], "invalid_code")


class PatientListStatusTests(M2TestCase):
    """The patient list said "Booked today · Turn 6" for a patient the doctor had already seen (Saged, 2026-11-24)."""

    def search(self, name):
        r = self.desk.get("/patients/search", {"q": name}, headers={"X-Membership": str(self.desk.membership_id)})
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.content.decode()

    def badge(self, name):
        html = self.search(name)
        return html.split('data-today-status="')[1].split('"')[0] if 'data-today-status="' in html else None

    def test_the_list_shows_what_todays_visit_is_now(self):
        visit = self.in_room()
        name = visit["patient"]["name"]
        self.assertEqual(self.badge(name), "in_room")
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=self.doc).status_code, 200)
        self.assertEqual(self.badge(name), "done")
        html = self.search(name)
        self.assertNotIn("Booked today", html)
