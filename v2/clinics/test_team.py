"""The team (design G1, G2): the permission table, escalation rules, adding people, sections and places."""

import uuid
from unittest import mock

from django.db import OperationalError
from django.test import SimpleTestCase

from accounts.models import User
from audit.models import AuditChange
from clinics import permissions as perms
from clinics.models import Membership, Section
from core.routing import clinic_transaction
from core.testing import call, new_patient
from directory.flows import create_clinic
from directory.models import Invitation, UserClinic
from scheduling.testing import M1TestCase, add_schedule, services_of


class GroupTests(SimpleTestCase):
    def test_groups_split_every_code_exactly_once(self):
        listed = [code for *_rest, codes in perms.GROUPS for code in codes]
        self.assertEqual(sorted(listed), sorted(perms.CODES))
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(len(perms.GROUPS), 10)  # the eight sections and the two money switches

    def test_wanted_codes_switches_groups_then_codes(self):
        wanted = perms.wanted_codes({"reports.view"}, {"stock": True, "reports": False}, {"inventory.manage": False})
        self.assertEqual(wanted, {"inventory.use"})
        self.assertEqual(perms.lists_for("assistant", perms.TEMPLATES["assistant"] - {"cash.shift"}), ([], ["cash.shift"]))

    def test_the_retired_others_code_is_gone_and_ignored_when_stored(self):
        # A doctor never sees or acts on another doctor's patients (Saged, 2026-11-13).
        self.assertNotIn("appointments.others", perms.CODES)
        self.assertNotIn("appointments.others", perms.effective_codes("owner"))
        self.assertNotIn("appointments.others", perms.effective_codes("doctor", granted=["appointments.others"]))


class TeamCase(M1TestCase):
    def setUp(self):
        super().setUp()
        self.ids = {role: self.a.membership(role).id for role in ("owner", "doctor", "assistant")}

    def member(self, role, fixture=None):
        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            return Membership.objects.get(clinic_id=fixture.id, id=fixture.membership(role).id)

    def grant(self, role, *codes, fixture=None):
        """The owner gives `role` these codes through the API."""
        fixture = fixture or self.a
        target = self.member(role, fixture)
        client = self.owner if fixture is self.a else fixture.client("owner")
        r = call(client, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version, "codes": {code: True for code in codes}})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["member"]


class TableTests(TeamCase):
    def test_the_table_has_groups_as_rows_and_members_as_columns(self):
        r = call(self.owner, "GET", "/api/v1/team")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([g["key"] for g in body["groups"]][:2], ["today", "patients"])
        assistant = next(m for m in body["members"] if m["id"] == self.ids["assistant"])
        self.assertEqual(assistant["groups"]["today"], "on")  # book and queue
        self.assertEqual(assistant["groups"]["team"], "off")
        self.assertEqual(assistant["groups"]["prices"], "on")  # sees money, collects, the register
        self.assertEqual(assistant["groups"]["suggest"], "off")
        self.assertEqual(assistant["groups"]["money"], "off")
        doctor = next(m for m in body["members"] if m["id"] == self.ids["doctor"])
        self.assertEqual((doctor["groups"]["prices"], doctor["groups"]["suggest"]), ("off", "off"))
        owner = next(m for m in body["members"] if m["id"] == self.ids["owner"])
        self.assertTrue(all(state == "on" for state in owner["groups"].values()))
        self.assertFalse(owner["editable"])
        self.assertTrue(assistant["editable"])
        self.assertTrue(any(c["owner_only"] for g in body["groups"] for c in g["codes"]))

    def test_without_team_manage_every_team_route_is_403(self):
        for method, path in [("GET", "/api/v1/team"), ("POST", "/api/v1/team/members"),
                             ("PATCH", f"/api/v1/team/members/{self.ids['doctor']}"),
                             ("PUT", f"/api/v1/team/members/{self.ids['doctor']}/permissions"),
                             ("GET", f"/api/v1/team/members/{self.ids['doctor']}/places"),
                             ("PUT", f"/api/v1/team/members/{self.ids['doctor']}/places"),
                             ("POST", f"/api/v1/team/members/{self.ids['doctor']}/temporary-password")]:
            self.assertEqual(call(self.desk, method, path, {}).status_code, 403, (method, path))

    def test_a_member_of_another_place_is_404(self):
        other = self.b.membership("assistant")
        r = call(self.owner, "PUT", f"/api/v1/team/members/{other.id}/permissions",
                 {"version": 1, "groups": {"stock": False}})
        self.assertEqual(r.status_code, 404)
        r = call(self.owner, "PATCH", f"/api/v1/team/members/{other.id}", {"version": 1, "is_active": False})
        self.assertEqual(r.status_code, 404)


class GrantTests(TeamCase):
    def test_owner_switches_a_group_off_and_one_code_back_on(self):
        target = self.member("assistant")
        r = call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version, "groups": {"prices": False}, "codes": {"billing.collect": True}})
        self.assertEqual(r.status_code, 200, r.content)
        member = r.json()["member"]
        self.assertIn("billing.collect", member["codes"])
        self.assertIn("money.view", member["codes"])  # a money code brings seeing money back
        self.assertNotIn("cash.collect", member["codes"])
        self.assertEqual(member["groups"]["prices"], "partial")
        after = self.member("assistant")
        self.assertEqual(after.version, target.version + 1)
        self.assertIn("cash.collect", after.revoked)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.a.id, action="team.permissions").exists())

    def test_the_change_applies_at_once(self):
        target = self.member("assistant")
        call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions",
             {"version": target.version, "groups": {"today": False}})
        self.assertEqual(call(self.desk, "GET", "/api/v1/sections").status_code, 403)

    def test_a_stale_version_is_409_and_changes_nothing(self):
        target = self.member("assistant")
        r = call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version + 5, "groups": {"stock": False}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "stale")
        self.assertEqual(self.member("assistant").revoked, [])

    def test_a_double_click_with_one_key_applies_once(self):
        target = self.member("assistant")
        key = uuid.uuid4()
        body = {"version": target.version, "codes": {"reports.view": True}}
        first = call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions", body, key=key)
        second = call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions", body, key=key)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(self.member("assistant").version, target.version + 1)

    def test_unknown_codes_and_groups_are_400(self):
        target = self.member("assistant")
        for body in ({"codes": {"root.all": True}}, {"groups": {"everything": True}}, {"codes": {"reports.view": 1}}):
            r = call(self.owner, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                     {"version": target.version, **body})
            self.assertEqual(r.status_code, 400, body)


class EscalationTests(TeamCase):
    """A doctor the owner made a team manager (design 5.8)."""

    def setUp(self):
        super().setUp()
        self.grant("doctor", "team.manage")
        self.manager = self.a.client("doctor")

    def test_a_manager_cant_give_an_owner_only_code(self):
        target = self.member("assistant")
        r = call(self.manager, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version, "codes": {"finances.view": True}})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "owner_only_code")

    def test_a_manager_cant_give_a_code_they_dont_hold(self):
        target = self.member("assistant")
        r = call(self.manager, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version, "codes": {"inventory.manage": True}})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "not_held")

    def test_a_manager_can_give_what_they_hold(self):
        target = self.member("assistant")
        r = call(self.manager, "PUT", f"/api/v1/team/members/{target.id}/permissions",
                 {"version": target.version, "codes": {"clinical.view": True}})
        self.assertEqual(r.status_code, 200, r.content)

    def test_nobody_edits_their_own_permissions_or_status(self):
        me = self.member("doctor")
        r = call(self.manager, "PUT", f"/api/v1/team/members/{me.id}/permissions",
                 {"version": me.version, "codes": {"reports.view": True}})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_self"))
        r = call(self.manager, "PATCH", f"/api/v1/team/members/{me.id}", {"version": me.version, "role": "assistant"})
        self.assertEqual(r.status_code, 403)

    def test_a_person_may_change_their_own_names(self):
        me = self.member("doctor")
        r = call(self.manager, "PATCH", f"/api/v1/team/members/{me.id}",
                 {"version": me.version, "display_name_en": "Dr. Sara"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.member("doctor").display_name_en, "Dr. Sara")

    def test_the_owner_is_fixed(self):
        owner = self.member("owner")
        for body in ({"is_active": False}, {"role": "doctor"}):
            r = call(self.manager, "PATCH", f"/api/v1/team/members/{owner.id}", {"version": owner.version, **body})
            self.assertEqual(r.status_code, 403, body)
        r = call(self.owner, "PATCH", f"/api/v1/team/members/{owner.id}", {"version": owner.version, "is_active": False})
        self.assertEqual(r.status_code, 403)

    def test_a_manager_cant_deactivate_someone_with_codes_they_dont_hold(self):
        self.grant("assistant", "inventory.manage")
        target = self.member("assistant")
        r = call(self.manager, "PATCH", f"/api/v1/team/members/{target.id}", {"version": target.version, "is_active": False})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_held"))

    def test_owner_deactivates_and_the_person_is_out_at_once(self):
        target = self.member("assistant")
        with self.captureOnCommitCallbacks(using=self.a.shard, execute=True):
            r = call(self.owner, "PATCH", f"/api/v1/team/members/{target.id}",
                     {"version": target.version, "is_active": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(call(self.desk, "GET", "/api/v1/clinic").status_code, 403)
        self.assertFalse(UserClinic.objects.get(user=self.a.user("assistant"), clinic_id=self.a.id).is_active)

    def test_a_role_change_resets_to_the_new_template(self):
        self.grant("assistant", "reports.view")
        target = self.member("assistant")
        r = call(self.owner, "PATCH", f"/api/v1/team/members/{target.id}", {"version": target.version, "role": "doctor"})
        self.assertEqual(r.status_code, 200, r.content)
        after = self.member("assistant")
        self.assertEqual((after.role, after.granted, after.revoked), ("doctor", [], []))


class AddMemberTests(TeamCase):
    def body(self, **extra):
        return {"mode": "new", "username": "nour", "full_name": "Nour Ali", "role": "assistant",
                "display_name": "نور", "display_name_en": "Nour", **extra}

    def test_a_new_account_gets_a_temporary_password_once(self):
        key = uuid.uuid4()
        r = call(self.owner, "POST", "/api/v1/team/members", self.body(), key=key)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["account"]["temporary_password"])
        user = User.objects.get(username="nour")
        self.assertTrue(user.must_change_password)
        self.assertEqual(user.created_for_clinic, self.a.id)
        replay = call(self.owner, "POST", "/api/v1/team/members", self.body(), key=key)
        self.assertEqual(replay.status_code, 201)
        self.assertIsNone(replay.json()["account"]["temporary_password"])
        self.assertTrue(replay.json()["account"]["can_reissue"])
        self.assertTrue(UserClinic.objects.filter(user=user, clinic_id=self.a.id, is_active=True).exists())

    def test_a_retry_after_a_failed_shard_step_reuses_only_its_own_account(self):
        from clinics import team

        key = uuid.uuid4()
        with mock.patch.object(team, "_insert_member", side_effect=OperationalError("shard down")):
            with self.assertRaises(OperationalError):
                call(self.owner, "POST", "/api/v1/team/members", self.body(), key=key)
        self.assertTrue(User.objects.filter(username="nour").exists())  # the directory step committed
        r = call(self.owner, "POST", "/api/v1/team/members", self.body(), key=key)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIsNone(r.json()["account"]["temporary_password"])
        # Another request (a new key) never attaches that account.
        other = call(self.owner, "POST", "/api/v1/team/members", self.body(), key=uuid.uuid4())
        self.assertEqual(other.status_code, 409)

    def test_an_existing_account_is_never_changed_or_attached(self):
        stranger = self.user("stranger", "Someone Else")
        before = (stranger.password, stranger.full_name)
        r = call(self.owner, "POST", "/api/v1/team/members", self.body(username="stranger"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "username_taken"))
        stranger.refresh_from_db()
        self.assertEqual((stranger.password, stranger.full_name), before)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertFalse(Membership.objects.filter(clinic_id=self.a.id, user_id=stranger.id).exists())

    def test_invitations_answer_the_same_and_the_invitee_accepts(self):
        stranger = self.user("stranger2", "Someone")
        known = call(self.owner, "POST", "/api/v1/team/members", self.body(mode="invite", username="stranger2"))
        unknown = call(self.owner, "POST", "/api/v1/team/members", self.body(mode="invite", username="nobody9"))
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(Invitation.objects.filter(user=stranger).count(), 1)
        client = self.client_for_account(stranger)
        listed = call(client, "GET", "/api/v1/me/invitations").json()["items"]
        self.assertEqual([i["clinic"]["id"] for i in listed], [self.a.id])
        r = call(client, "POST", f"/api/v1/me/invitations/{listed[0]['id']}/accept", {})
        self.assertEqual(r.status_code, 200, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            member = Membership.objects.get(clinic_id=self.a.id, user_id=stranger.id)
        self.assertEqual((member.role, member.is_active), ("assistant", True))
        self.assertTrue(UserClinic.objects.filter(user=stranger, clinic_id=self.a.id).exists())

    def test_a_manager_cant_add_a_role_with_codes_they_dont_hold(self):
        self.grant("assistant", "team.manage")
        r = call(self.desk, "POST", "/api/v1/team/members", self.body(role="doctor"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_held"))

    def test_reissue_only_for_an_account_this_place_made_that_never_signed_in(self):
        r = call(self.owner, "POST", "/api/v1/team/members", self.body())
        member_id = r.json()["member"]["id"]
        again = call(self.owner, "POST", f"/api/v1/team/members/{member_id}/temporary-password", {})
        self.assertEqual(again.status_code, 200, again.content)
        self.assertTrue(again.json()["account"]["temporary_password"])
        r = call(self.owner, "POST", f"/api/v1/team/members/{self.ids['doctor']}/temporary-password", {})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_eligible"))

    def client_for_account(self, user):
        from django.test import Client

        client = Client()
        client.force_login(user)
        return client


class SectionLimitTests(TeamCase):
    """An assistant of a center limited to one section (design G1)."""

    def setUp(self):
        super().setUp()
        self.c = self.clinic("c", "shard_1", sections=[{"specialty": "dental", "name": "Teeth"},
                                                       {"specialty": "pediatrics", "name": "Kids"}])
        with clinic_transaction(self.c.id, self.c.shard):
            self.teeth, self.kids = Section.objects.filter(clinic_id=self.c.id).order_by("sort")
        add_schedule(self.c, "owner", section=self.teeth.id)
        add_schedule(self.c, "doctor", section=self.kids.id)
        with clinic_transaction(self.c.id, self.c.shard):
            from clinics.models import Service

            self.svc = {s.section_id: s for s in Service.objects.filter(clinic_id=self.c.id, kind="consultation")}
        self.c_owner = self.c.client("owner")
        self.c_desk = self.c.client("assistant")
        self.kid_visit = self.book_c(self.c.membership("doctor").id, self.kids.id)
        self.teeth_visit = self.book_c(self.c.membership("owner").id, self.teeth.id)
        target = self.member("assistant", self.c)
        r = call(self.c_owner, "PATCH", f"/api/v1/team/members/{target.id}",
                 {"version": target.version, "sections": [self.teeth.id]})
        self.assertEqual(r.status_code, 200, r.content)

    def book_c(self, doctor_id, section_id, client=None, expect=201):
        r = call(client or self.c_owner, "POST", "/api/v1/appointments", {
            "doctor_id": doctor_id, "service_id": self.svc[section_id].id,
            "new_patient": new_patient(full_name="Kid Patient", sex="m", age=5)})
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()["appointment"]["id"] if expect == 201 else r

    def test_today_shows_only_their_sections(self):
        body = call(self.c_desk, "GET", "/api/v1/today").json()
        self.assertEqual([s["id"] for s in body["sections"]], [self.teeth.id])
        ids = [a["id"] for q in body["queues"] for a in q["appointments"]]
        self.assertEqual(ids, [self.teeth_visit])
        self.assertEqual(call(self.c_desk, "GET", f"/api/v1/today?section={self.kids.id}").status_code, 404)

    def test_another_sections_visit_is_404(self):
        self.assertEqual(call(self.c_desk, "GET", f"/api/v1/appointments/{self.kid_visit}").status_code, 404)
        r = call(self.c_desk, "POST", f"/api/v1/appointments/{self.kid_visit}/arrive", {"status": "booked"})
        self.assertEqual(r.status_code, 404)
        r = call(self.c_desk, "POST", f"/api/v1/appointments/{self.teeth_visit}/arrive", {"status": "booked"})
        self.assertEqual(r.status_code, 200, r.content)

    def test_another_sections_bill_is_404(self):
        pay = {"amount": 100, "method": "cash"}
        self.assertEqual(call(self.c_desk, "POST", f"/api/v1/appointments/{self.kid_visit}/payments", pay).status_code,
                         404)
        r = call(self.c_desk, "POST", f"/api/v1/appointments/{self.teeth_visit}/payments", pay)
        self.assertEqual(r.status_code, 201, r.content)

    def test_booking_and_lists_follow_the_sections(self):
        r = self.book_c(self.c.membership("doctor").id, self.kids.id, client=self.c_desk, expect=404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        self.assertEqual(call(self.c_desk, "GET", f"/api/v1/sections/{self.kids.id}/services").status_code, 404)
        self.assertEqual([s["id"] for s in call(self.c_desk, "GET", "/api/v1/sections").json()["items"]],
                         [self.teeth.id])

    def test_only_assistants_and_only_this_places_sections(self):
        doctor = self.member("doctor", self.c)
        r = call(self.c_owner, "PATCH", f"/api/v1/team/members/{doctor.id}",
                 {"version": doctor.version, "sections": [self.kids.id]})
        self.assertEqual(r.status_code, 400)
        with clinic_transaction(self.a.id, self.a.shard):
            other = Section.objects.filter(clinic_id=self.a.id).values_list("id", flat=True).first()
        target = self.member("assistant", self.c)
        r = call(self.c_owner, "PATCH", f"/api/v1/team/members/{target.id}",
                 {"version": target.version, "sections": [other]})
        self.assertEqual(r.status_code, 404)


class PlacesTests(TeamCase):
    """One owner with three places: a (shard 1), a branch on shard 2 and one on shard 1 (design G2)."""

    def setUp(self):
        super().setUp()
        owner = self.a.user("owner")
        self.far = create_clinic(name="Far branch", specialty_code="dental", owner=owner, shard="shard_2")
        self.near = create_clinic(name="Near branch", specialty_code="dental", owner=owner, shard="shard_1")
        self.assistant = self.a.user("assistant")

    def places(self, body, key=None):
        return call(self.owner, "PUT", f"/api/v1/team/members/{self.ids['assistant']}/places", body, key=key)

    def membership_in(self, entry):
        with clinic_transaction(entry.id, entry.shard):
            return Membership.objects.filter(clinic_id=entry.id, user_id=self.assistant.id).first()

    def test_the_owner_sees_their_places_and_ticks_two_for_the_same_account(self):
        listed = call(self.owner, "GET", f"/api/v1/team/members/{self.ids['assistant']}/places").json()["items"]
        self.assertEqual({p["clinic_id"] for p in listed}, {self.a.id, self.far.id, self.near.id})
        self.assertIsNone(next(p for p in listed if p["clinic_id"] == self.far.id)["member"])
        with self.captureOnCommitCallbacks(using=self.a.shard, execute=True):  # the same shard's picker row
            r = self.places({"places": [{"clinic_id": self.far.id}, {"clinic_id": self.near.id, "role": "doctor"}]})
        self.assertEqual(r.status_code, 200, r.content)
        far, near = self.membership_in(self.far), self.membership_in(self.near)
        self.assertEqual((far.role, near.role), ("assistant", "doctor"))
        self.assertEqual(User.objects.filter(id=self.assistant.id).count(), 1)
        links = {link.clinic_id for link in UserClinic.objects.filter(user=self.assistant, is_active=True)}
        self.assertEqual(links, {self.a.id, self.far.id, self.near.id})
        with clinic_transaction(self.far.id, self.far.shard):
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.far.id, action="team.place").exists())

    def test_copy_applies_this_places_role_and_codes(self):
        self.grant("assistant", "reports.view")
        r = self.places({"places": [self.far.id], "copy": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("reports.view", self.membership_in(self.far).granted)

    def test_unticking_deactivates_and_a_repeat_changes_nothing(self):
        self.places({"places": [self.far.id, self.near.id]})
        r = self.places({"places": [self.near.id]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.membership_in(self.far).is_active)
        again = self.places({"places": [self.near.id]})
        self.assertTrue(all(p["status"] == "unchanged" for p in again.json()["places"]))

    def test_a_failed_shard_is_503_and_the_same_key_finishes_the_job(self):
        from clinics import team

        real = team._apply_place
        calls = {"n": 0}

        def flaky(ctx, entry, source, wanted):
            if entry.shard == "shard_2" and calls["n"] == 0:
                calls["n"] += 1
                raise OperationalError("shard 2 is down")
            return real(ctx, entry, source, wanted)

        key = uuid.uuid4()
        with mock.patch.object(team, "_apply_place", side_effect=flaky):
            r = self.places({"places": [self.far.id, self.near.id]}, key=key)
        self.assertEqual(r.status_code, 503)
        self.assertEqual({p["clinic_id"]: p["status"] for p in r.json()["error"]["places"]}[self.far.id], "failed")
        self.assertIsNone(self.membership_in(self.far))
        r = self.places({"places": [self.far.id, self.near.id]}, key=key)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self.membership_in(self.far).is_active)
        self.assertTrue(self.membership_in(self.near).is_active)

    def test_only_the_owner_and_only_their_places(self):
        self.grant("doctor", "team.manage")
        r = call(self.a.client("doctor"), "PUT", f"/api/v1/team/members/{self.ids['assistant']}/places",
                 {"places": [self.far.id]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "owner_only"))
        r = self.places({"places": [self.b.id]})
        self.assertEqual(r.status_code, 404)
        r = call(self.owner, "PUT", f"/api/v1/team/members/{self.ids['owner']}/places", {"places": [self.far.id]})
        self.assertEqual(r.status_code, 403)


class EnglishTests(TeamCase):
    def english(self, role="owner"):
        user = self.a.user(role)
        user.language = "en"
        user.save(update_fields=["language"])
        return self.a.client(role)

    def test_catalogue_rows_and_sections_read_in_english(self):
        client = self.english()
        body = call(client, "GET", "/api/v1/clinic").json()
        consult = next(s for s in body["services"] if s["kind"] == "consultation")
        self.assertEqual((consult["name"], consult["name_ar"], consult["name_en"]), ("Consultation", "كشف", "Consultation"))
        self.assertEqual(body["sections"][0]["name"], "Dental")
        medicines = call(client, "GET", "/api/v1/medicines").json()["items"]
        brufen = next(m for m in medicines if m["name"] == "Brufen")
        self.assertFalse(any("؀" <= ch <= "ۿ" for ch in brufen["default_dose"]))
        self.assertTrue(brufen["default_dose_ar"])
        arabic = call(self.desk, "GET", "/api/v1/clinic").json()
        self.assertEqual(next(s for s in arabic["services"] if s["kind"] == "consultation")["name"], "كشف")

    def test_names_are_editable_and_fall_back(self):
        sections = call(self.owner, "GET", "/api/v1/sections").json()["items"]
        r = call(self.owner, "PATCH", f"/api/v1/sections/{sections[0]['id']}",
                 {"changes": {"name_en": "Teeth"}, "expected": {"name_en": sections[0]["name_en"]}})
        self.assertEqual(r.status_code, 200, r.content)
        r = call(self.owner, "PUT", "/api/v1/settings/profile", {"changes": {"name_en": "Smile Clinic"}, "expected": {}})
        self.assertEqual(r.status_code, 200, r.content)
        client = self.english()
        self.assertEqual(call(client, "GET", "/api/v1/sections").json()["items"][0]["name"], "Teeth")
        self.assertEqual(call(client, "GET", "/api/v1/clinic").json()["clinic"]["name"], "Smile Clinic")
        doctor = self.member("doctor")  # no English name: the typed one shows
        team_body = call(client, "GET", "/api/v1/team").json()
        self.assertEqual(next(m for m in team_body["members"] if m["id"] == doctor.id)["name"], doctor.display_name)

    def test_every_arabic_catalogue_phrase_has_english(self):
        from clinics.specialties import SPECIALTIES

        def arabic(text):
            return any("؀" <= ch <= "ۿ" for ch in text or "")

        missing = set()
        for spec in SPECIALTIES.values():
            for m in spec["medicines"]:
                for field in ("form", "default_dose", "default_note"):
                    if arabic(m[field]) and not m[f"{field}_en"]:
                        missing.add(m[field])
            for p in spec["procedures"]:
                if arabic(p["description"]) and not p["description_en"]:
                    missing.add(p["name_en"])
            for name, _lines, notes in spec["templates"]:
                from clinics.catalogue.english import english

                for text in (name, notes):
                    if arabic(text) and not english(text):
                        missing.add(text)
        self.assertEqual(missing, set())

    def test_default_section_names_get_both_languages(self):
        with clinic_transaction(self.a.id, self.a.shard):
            section = Section.objects.filter(clinic_id=self.a.id).first()
        self.assertEqual((section.name, section.name_en), ("أسنان", "Dental"))
        self.assertEqual(services_of(self.a)["consultation"].name_en, "Consultation")


class RouteTests(TeamCase):
    """T2: every team, names and walk-in route declares its codes, and a member without them gets 403."""

    EXPECTED = {
        ("GET", "api/v1/sections"): ("appointments.book",),
        ("GET", "api/v1/sections/<int:section_id>/services"): ("appointments.book",),
        ("GET", "api/v1/sections/<int:section_id>/doctors"): ("appointments.book",),
        ("GET", "api/v1/schedules"): ("settings.manage",),
        ("POST", "api/v1/schedules"): ("settings.manage",),
        ("PUT", "api/v1/schedules/<int:schedule_id>"): ("settings.manage",),
        ("DELETE", "api/v1/schedules/<int:schedule_id>"): ("settings.manage",),
        ("PATCH", "api/v1/sections/<int:section_id>"): ("settings.manage",),
        ("PUT", "api/v1/settings/profile"): ("settings.manage",),
        ("GET", "api/v1/team"): ("team.manage",),
        ("POST", "api/v1/team/members"): ("team.manage",),
        ("PATCH", "api/v1/team/members/<int:member_id>"): ("team.manage",),
        ("PUT", "api/v1/team/members/<int:member_id>/permissions"): ("team.manage",),
        ("PUT", "api/v1/team/members/<int:member_id>/hours"): ("team.manage",),
        ("POST", "api/v1/team/members/<int:member_id>/temporary-password"): ("team.manage",),
        ("GET", "api/v1/team/members/<int:member_id>/places"): ("team.manage",),
        ("PUT", "api/v1/team/members/<int:member_id>/places"): ("team.manage",),
        ("GET", "api/v1/me/invitations"): (),  # the signed-in account, no place
        ("POST", "api/v1/me/invitations/<int:invitation_id>/accept"): (),
    }

    def test_every_route_declares_its_codes(self):
        from reports.test_routes import routes

        self.assertEqual(routes({"clinics.urls"}), self.EXPECTED)
        walk_in = routes({"scheduling.urls"})[("POST", "api/v1/appointments/walk-in")]
        self.assertEqual(walk_in, ("appointments.book", "appointments.queue"))

    def test_the_doctor_is_refused_every_team_and_settings_route(self):
        doctor = self.a.client("doctor")
        for (method, pattern), codes in self.EXPECTED.items():
            if not codes or set(codes) <= perms.TEMPLATES["doctor"]:
                continue
            path = "/" + pattern
            for part in ("section_id", "schedule_id", "member_id"):
                path = path.replace(f"<int:{part}>", "1")
            self.assertEqual(call(doctor, method, path, {}).status_code, 403, (method, path))
