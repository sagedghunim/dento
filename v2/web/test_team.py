"""Team & settings (design G1, G3), a doctor's own visits on Today (G4) and the walk-in dialog (G5)."""

import json
import re

from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call, new_patient
from scheduling.testing import M1TestCase


def fragment(client, url):
    return client.get(url, headers={"X-Membership": str(client.membership_id)})


def set_codes(fixture, role, granted=(), revoked=()):
    with clinic_transaction(fixture.id, fixture.shard):
        Membership.objects.filter(clinic_id=fixture.id, id=fixture.membership(role).id).update(
            granted=list(granted), revoked=list(revoked))


def team_data(html):
    found = re.search(r'<script id="team-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(found.group(1)) if found else None


class TeamPageTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def test_the_owner_gets_the_table_the_members_and_the_settings(self):
        r = self.a.client("owner").get("/team")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn('href="/team" aria-current="page"', html)
        for marker in ("data-matrix-card", "data-add-member", 'id="member-dialog"', 'id="add-dialog"',
                       "data-profile-form", 'id="profile-data"'):
            self.assertIn(marker, html, marker)
        data = team_data(html)
        self.assertEqual([g["key"] for g in data["groups"]],
                         ["today", "patients", "visit", "prices", "suggest", "money", "stock", "reports", "settings", "team"])
        self.assertTrue(all(g["label"] and g["description"] for g in data["groups"]))
        self.assertTrue(data["is_owner"])
        me = next(m for m in data["members"] if m["is_me"])
        self.assertEqual(me["role"], "owner")
        self.assertFalse(me["editable"])  # nobody changes their own permissions
        self.assertTrue(next(m for m in data["members"] if m["role"] == "assistant")["editable"])

    def test_no_team_or_settings_code_is_refused(self):
        self.assertEqual(self.a.client("assistant").get("/team").status_code, 403)
        # The doctor sees Team for the follow-ups after a procedure only (Saged, 2026-11-19).
        doctor = self.a.client("doctor")
        page = doctor.get("/team").content.decode()
        self.assertIn("data-recipes", page)
        self.assertNotIn("data-discount-card", page)
        self.assertNotIn("data-booking-card", page)

    def test_settings_only_shows_the_settings_card(self):
        set_codes(self.a, "doctor", granted=["settings.manage"])
        r = self.a.client("doctor").get("/team")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("data-profile-form", html)
        self.assertNotIn("team-data", html)
        self.assertNotIn("data-matrix-card", html)

    def test_team_only_shows_the_table_without_the_settings(self):
        set_codes(self.a, "doctor", granted=["team.manage"])
        html = self.a.client("doctor").get("/team").content.decode()
        data = team_data(html)
        self.assertFalse(data["is_owner"])
        self.assertNotIn("data-profile-form", html)
        owner = next(m for m in data["members"] if m["role"] == "owner")
        self.assertFalse(owner["editable"])
        self.assertFalse(data["places"])  # places are the owner's only

    def test_names_are_escaped(self):
        assistant = self.a.membership("assistant")
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=assistant.id).update(display_name="<img src=x onerror=1>")
        html = self.a.client("owner").get("/team").content.decode()
        self.assertNotIn("<img src=x", html)
        self.assertEqual(next(m for m in team_data(html)["members"] if m["id"] == assistant.id)["name"],
                         "<img src=x onerror=1>")

    def test_a_switch_saved_through_the_api_shows_on_the_next_load(self):
        owner = self.a.client("owner")
        assistant = next(m for m in team_data(owner.get("/team").content.decode())["members"] if m["role"] == "assistant")
        self.assertEqual(assistant["groups"]["stock"], "partial")
        r = call(owner, "PUT", f"/api/v1/team/members/{assistant['id']}/permissions",
                 {"version": assistant["version"], "groups": {"stock": True}})
        self.assertEqual(r.status_code, 200, r.content)
        again = next(m for m in team_data(owner.get("/team").content.decode())["members"] if m["id"] == assistant["id"])
        self.assertEqual(again["groups"]["stock"], "on")
        stale = call(owner, "PUT", f"/api/v1/team/members/{assistant['id']}/permissions",
                     {"version": assistant["version"], "groups": {"stock": False}})
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "stale"))


class TodayOwnVisitsTests(M1TestCase):
    """A doctor's Today is their own queue only; the desk's is everyone's (Saged, 2026-11-13)."""

    def setUp(self):
        super().setUp()
        self.owner_visit = self.book().json()["appointment"]["id"]
        self.act(self.owner_visit, "arrive", "booked")
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in",
                 {"doctor_id": self.a.membership("doctor").id, "new_patient": new_patient(full_name="Own Patient", phone="01099999999")})
        self.assertEqual(r.status_code, 201, r.content)
        self.own_visit = r.json()["appointment"]["id"]

    @staticmethod
    def row(html, appointment_id):
        found = re.search(rf'<li class="appt [^"]*" data-id="{appointment_id}".*?</li>', html, re.S)
        return found.group(0) if found else ""

    def test_a_doctors_today_and_its_poll_show_only_their_own_rows(self):
        doctor = self.a.client("doctor")
        html = doctor.get("/").content.decode()
        self.assertEqual(self.row(html, self.owner_visit), "")
        self.assertIn('data-act="call"', self.row(html, self.own_visit))
        self.assertNotIn("Mohamed Ahmed", html)
        queue = fragment(doctor, "/today/queue")
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(self.row(queue.content.decode(), self.owner_visit), "")
        self.assertIn('data-act="call"', self.row(queue.content.decode(), self.own_visit))
        # The ETag is the doctor's own: unchanged data answers 304; any change in the place moves the version.
        etag = queue["ETag"]
        self.assertNotEqual(etag, fragment(self.desk, "/today/queue")["ETag"])
        again = doctor.get("/today/queue", headers={"X-Membership": str(doctor.membership_id), "If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.act(self.owner_visit, "call", "waiting")  # the desk calls the owner's patient in
        changed = doctor.get("/today/queue", headers={"X-Membership": str(doctor.membership_id), "If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.row(changed.content.decode(), self.owner_visit), "")

    def test_the_desk_acts_on_every_row(self):
        html = self.desk.get("/").content.decode()
        self.assertIn('data-act="call"', self.row(html, self.owner_visit))
        self.assertIn('data-act="call"', self.row(html, self.own_visit))

    def test_the_walk_in_button_and_its_defaults(self):
        html = self.desk.get("/").content.decode()
        self.assertIn("data-open-walkin", html)
        self.assertIn('id="walkin-dialog"', html)
        r = fragment(self.desk, "/today/walk-in")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["doctor_id"], {self.a.membership("owner").id, self.a.membership("doctor").id})
        self.assertIsNotNone(body["service_id"])
        # A doctor who works today takes their own walk-ins (they book only for themselves).
        self.assertEqual(fragment(self.a.client("doctor"), "/today/walk-in").json()["doctor_id"],
                         self.a.membership("doctor").id)
        self.assertEqual(self.desk.get("/today/walk-in").status_code, 409)  # without X-Membership, as the API

    def test_no_walk_in_without_the_queue_code(self):
        set_codes(self.a, "assistant", revoked=["appointments.queue"])
        desk = self.a.client("assistant")
        html = desk.get("/").content.decode()
        self.assertNotIn("data-open-walkin", html)
        self.assertNotIn('id="walkin-dialog"', html)
        self.assertEqual(fragment(desk, "/today/walk-in").status_code, 403)

    def test_a_walk_in_from_the_dialogs_body(self):
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in",
                 {"new_patient": new_patient(full_name="Walk In Now", phone="01011111111")})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["appointment"]["status"], "waiting")
        self.assertIn("ahead", r.json())


class OverviewTests(M1TestCase):
    """Who works where (design G1, G2): the owner sees each person's places, role and groups there."""

    def setUp(self):
        super().setUp()
        from directory.flows import create_clinic

        owner = self.a.user("owner")
        self.far = create_clinic(name="Far branch", specialty_code="dental", owner=owner, shard="shard_2")
        self.near = create_clinic(name="Near branch", specialty_code="dental", owner=owner, shard="shard_1")
        assistant = self.a.membership("assistant").id
        with self.captureOnCommitCallbacks(using=self.a.shard, execute=True):
            r = call(self.owner, "PUT", f"/api/v1/team/members/{assistant}/places",
                     {"places": [{"clinic_id": self.far.id}, {"clinic_id": self.near.id, "role": "doctor"}]})
        self.assertEqual(r.status_code, 200, r.content)

    def person(self, client, role="assistant"):
        data = team_data(client.get("/team").content.decode())
        return next(p for p in data["overview"]["people"] if p["member_id"] == self.a.membership(role).id)

    def test_the_owner_sees_every_place_with_its_role_and_groups(self):
        person = self.person(self.owner)
        places = {p["clinic_id"]: p for p in person["places"]}
        self.assertEqual(set(places), {self.a.id, self.far.id, self.near.id})
        self.assertTrue(places[self.a.id]["current"])
        self.assertEqual((places[self.far.id]["role"], places[self.near.id]["role"]), ("assistant", "doctor"))
        self.assertEqual(places[self.a.id]["groups"]["today"], "on")  # book and queue: the whole group
        self.assertEqual(places[self.near.id]["groups"]["visit"], "on")  # a doctor there: the whole visit group
        self.assertEqual(places[self.far.id]["groups"]["team"], "off")
        self.assertNotIn(self.a.membership("owner").id, [p["member_id"] for p in team_data(
            self.owner.get("/team").content.decode())["overview"]["people"]])  # the owner has everything

    def test_someone_else_with_team_manage_sees_this_place_only(self):
        set_codes(self.a, "doctor", granted=["team.manage"])
        person = self.person(self.a.client("doctor"))
        self.assertEqual([p["clinic_id"] for p in person["places"]], [self.a.id])
