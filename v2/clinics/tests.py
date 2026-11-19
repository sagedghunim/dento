import threading
import types
import uuid

from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase

from clinics.permissions import ALL, GrantRefused, check_change, effective_codes
from core.testing import ShardTestCase, call


def member(id, role, granted=(), revoked=()):
    return types.SimpleNamespace(id=id, role=role, granted=list(granted), revoked=list(revoked))


class GrantRuleTests(SimpleTestCase):
    def test_templates_and_overrides(self):
        self.assertEqual(effective_codes("owner"), ALL)
        self.assertIn("clinical.view", effective_codes("assistant")) # the desk reads the file (Saged, 2026-11-19)
        self.assertNotIn("clinical.view", effective_codes("assistant", revoked=["clinical.view"]))
        self.assertNotIn("billing.collect", effective_codes("doctor", revoked=["billing.collect"]))
        self.assertNotIn("made.up", effective_codes("doctor", granted=["made.up"]))

    def test_a_manager_who_isnt_the_owner_cant_give_what_they_dont_hold(self):
        manager = member(1, "assistant", granted=["team.manage"])
        target = member(2, "assistant")
        for code in ["billing.refund", "finances.view", "audit.view", "team.manage"]:
            with self.assertRaises(GrantRefused):
                check_change(manager, target, "assistant", [code], [])
        with self.assertRaises(GrantRefused):  # a role change that reaches clinical codes
            check_change(manager, target, "doctor", [], [])
        check_change(manager, target, "assistant", [], ["inventory.use"])  # removing a held code is fine

    def test_nobody_edits_themselves_or_the_owner(self):
        owner = member(1, "owner")
        with self.assertRaises(GrantRefused):
            check_change(owner, owner, "owner", [], [])
        manager = member(2, "doctor", granted=["team.manage"])
        with self.assertRaises(GrantRefused):
            check_change(manager, owner, "doctor", [], [])
        other_manager = member(3, "doctor", granted=["team.manage"])
        with self.assertRaises(GrantRefused):
            check_change(manager, other_manager, "doctor", [], ["team.manage"])

    def test_the_owner_can_give_owner_only_codes(self):
        check_change(member(1, "owner"), member(2, "assistant"), "assistant", ["finances.view"], [])


class ClinicApiTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")
        self.b = self.clinic("b", "shard_2")

    def test_any_member_reads_the_clinic_configuration(self):
        r = call(self.a.client("assistant"), "GET", "/api/v1/clinic")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["clinic"]["id"], self.a.id)
        self.assertTrue(data["services"])
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_x_membership_from_another_tab_is_refused(self):
        r = call(self.a.client("assistant"), "GET", "/api/v1/clinic", membership=self.b.membership("assistant").id)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "clinic_changed"))

    def test_settings_need_the_permission(self):
        body = {"changes": {"follow_up_days": 7}, "expected": {"follow_up_days": 14}}
        self.assertEqual(call(self.a.client("assistant"), "PUT", "/api/v1/settings/clinic", body).status_code, 403)
        r = call(self.a.client("owner"), "PUT", "/api/v1/settings/clinic", body)
        self.assertEqual((r.status_code, r.json()["settings"]["follow_up_days"]), (200, 7))

    def test_a_repeat_replays_and_a_reused_key_with_another_body_is_refused(self):
        client = self.a.client("owner")
        key = uuid.uuid4()
        body = {"changes": {"follow_up_days": 7}, "expected": {"follow_up_days": 14}}
        first = call(client, "PUT", "/api/v1/settings/clinic", body, key=key)
        again = call(client, "PUT", "/api/v1/settings/clinic", body, key=key)
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(first.json(), again.json())
        other = call(client, "PUT", "/api/v1/settings/clinic", {**body, "changes": {"follow_up_days": 9}}, key=key)
        self.assertEqual((other.status_code, other.json()["error"]["code"]), (422, "request_key_reused"))

    def test_a_refusal_frees_the_key(self):
        client = self.a.client("owner")
        key = uuid.uuid4()
        stale = call(client, "PUT", "/api/v1/settings/clinic", {"changes": {"follow_up_days": 7}, "expected": {"follow_up_days": 3}}, key=key)
        self.assertEqual(stale.status_code, 409)
        fixed = call(client, "PUT", "/api/v1/settings/clinic", {"changes": {"follow_up_days": 7}, "expected": {"follow_up_days": 3}}, key=key)
        self.assertEqual(fixed.status_code, 409)  # still stale: the key ran again, nothing stored

    def test_a_mutating_request_without_a_key_is_refused(self):
        client = self.a.client("owner")
        r = client.put("/api/v1/settings/clinic", "{}", content_type="application/json",
                       headers={"X-Membership": str(client.membership_id)})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "key_required"))

    def test_settings_changes_are_audited(self):
        from audit.models import AuditChange
        from core.routing import clinic_transaction

        call(self.a.client("owner"), "PUT", "/api/v1/settings/clinic",
             {"changes": {"digits": "arab"}, "expected": {"digits": "latn"}})
        with clinic_transaction(self.a.id, self.a.shard):
            change = AuditChange.objects.get(clinic_id=self.a.id, action="settings.change")
        self.assertEqual(change.details, {"before": {"digits": "latn"}, "after": {"digits": "arab"}})

    def test_inactive_member_is_refused_at_once(self):
        from clinics.models import Membership
        from core.routing import clinic_transaction

        client = self.a.client("assistant")
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(is_active=False)
        r = call(client, "GET", "/api/v1/clinic")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "membership_inactive"))


class ConcurrentRepeatTests(TransactionTestCase):
    """Two copies of one request at once: one effect, two identical answers (design 10, T3)."""

    databases = "__all__"

    def test_two_threads_with_one_key(self):
        helper = ShardTestCase()
        a = helper.clinic("t", "shard_1")
        key = uuid.uuid4()
        body = {"changes": {"follow_up_days": 5}, "expected": {"follow_up_days": 14}}
        results = []

        def work():
            try:
                results.append(call(a.client("owner"), "PUT", "/api/v1/settings/clinic", body, key=key))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual(results[0].json(), results[1].json())
        from audit.models import AuditChange
        from core.routing import clinic_transaction

        with clinic_transaction(a.id, a.shard):
            self.assertEqual(AuditChange.objects.filter(clinic_id=a.id, action="settings.change").count(), 1)
