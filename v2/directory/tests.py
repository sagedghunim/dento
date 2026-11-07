from accounts.models import User
from clinics.models import Clinic, Membership, Service
from core.api import SESSION_MFA
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from directory.models import DirectoryClinic, UserClinic


class PlatformTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.user("support", with_totp=True, is_platform_admin=True)
        self.client.force_login(self.admin)
        session = self.client.session
        session[SESSION_MFA] = True
        session.save()

    def create(self, **extra):
        body = {"name": "عيادة جديدة", "specialty": "dental", "city": "طنطا", "owner_username": "newowner",
                "owner_name": "د. جديد", **extra}
        return call(self.client, "POST", "/api/v1/platform/clinics", body)

    def test_create_a_clinic_with_its_owner(self):
        r = self.create()
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertTrue(data["temporary_password"])
        entry = DirectoryClinic.objects.get(id=data["clinic"]["id"])
        owner = User.objects.get(username="newowner")
        self.assertTrue(owner.must_change_password)
        self.assertTrue(owner.check_password(data["temporary_password"]))
        self.assertTrue(UserClinic.objects.filter(user=owner, clinic=entry, role="owner").exists())
        with clinic_transaction(entry.id, entry.shard):
            self.assertTrue(Clinic.objects.filter(id=entry.id, owned=True).exists())
            self.assertTrue(Membership.objects.filter(clinic_id=entry.id, user_id=owner.id, role="owner").exists())
            self.assertTrue(Service.objects.filter(clinic_id=entry.id, kind="consultation").exists())

    def test_a_repeat_creates_nothing_and_never_shows_the_password_again(self):
        key = "6f1f2d1e-0000-4000-8000-000000000001"
        body = {"name": "x", "specialty": "dental", "owner_username": "o1", "owner_name": "O"}
        first = call(self.client, "POST", "/api/v1/platform/clinics", body, key=key)
        again = call(self.client, "POST", "/api/v1/platform/clinics", body, key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertIsNone(again.json()["temporary_password"])
        self.assertEqual(DirectoryClinic.objects.filter(name="x").count(), 1)

    def test_new_clinics_go_to_the_least_full_shard(self):
        shards = [self.create(owner_username=f"o{i}").json()["clinic"]["shard"] for i in range(4)]
        self.assertEqual(sorted(shards), ["shard_1", "shard_1", "shard_2", "shard_2"])

    def test_suspend_stops_the_clinics_members(self):
        a = self.clinic("a")
        assistant = a.client("assistant")
        self.assertEqual(call(assistant, "GET", "/api/v1/clinic").status_code, 200)
        r = call(self.client, "POST", f"/api/v1/platform/clinics/{a.id}/suspend")
        self.assertEqual((r.status_code, r.json()["clinic"]["status"]), (200, "suspended"))
        refused = call(assistant, "GET", "/api/v1/clinic")
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (403, "clinic_suspended"))
        call(self.client, "POST", f"/api/v1/platform/clinics/{a.id}/reactivate")
        self.assertEqual(call(assistant, "GET", "/api/v1/clinic").status_code, 200)

    def test_only_platform_admins_with_their_code(self):
        a = self.clinic("a")
        owner = a.client("owner")
        self.assertEqual(call(owner, "GET", "/api/v1/platform/clinics").status_code, 403)
        session = self.client.session
        session[SESSION_MFA] = False
        session.save()
        r = call(self.client, "GET", "/api/v1/platform/clinics")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "mfa_required"))

    def test_platform_admins_cant_become_clinic_owners(self):
        from core.errors import ApiError
        from directory.flows import create_clinic

        with self.assertRaises(ApiError):
            create_clinic(name="x", specialty_code="dental", owner=self.admin)

    def test_the_platform_page_lists_clinics(self):
        self.create()
        r = self.client.get("/platform")
        self.assertEqual(r.status_code, 200)
        self.assertIn("عيادة جديدة", r.content.decode())
