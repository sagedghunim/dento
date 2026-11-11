"""Self-service sign-up and adding a place (design D2)."""

import datetime as dt
import hashlib
import json
import threading
import uuid

from django.db import connections
from django.test import Client, TransactionTestCase

from accounts.models import AccountEvent, AccountRequestKey, Throttle, User
from clinics.models import Clinic, Membership, Schedule, Section, Service
from core.api import SESSION_MFA
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from directory.models import DirectoryClinic, UserClinic


def body(**changes):
    owner = {"full_name": "د. ياسمين علي", "mobile": "01098765432", "username": "yasmine", "password": "a-long-pass-phrase"}
    owner.update(changes.pop("owner", {}))
    data = {"kind": "clinic", "sections": [{"specialty": "dental"}], "name": "عيادة ياسمين", "city": "المنصورة",
            "phone": "0502345678", "owner": owner}
    data.update(changes)
    return data


def signup(client=None, data=None, key=None, ip="10.0.0.1", **extra):
    client = client or Client()
    headers = {"Idempotency-Key": str(key or uuid.uuid4())}
    return client.generic("POST", "/api/v1/signup", json.dumps(data if data is not None else body()),
                          content_type="application/json", headers=headers, REMOTE_ADDR=ip, **extra)


def place_of(username):
    user = User.objects.get(username=username)
    link = UserClinic.objects.select_related("clinic").get(user=user, role="owner")
    return user, link


class SignUpTests(ShardTestCase):
    def test_sign_up_creates_the_owner_and_the_place_and_signs_in(self):
        client = Client()
        key = uuid.uuid4()
        r = signup(client, key=key)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json(), {"next": "security"})
        self.assertEqual(r["Idempotency-Key"], str(key))
        user, link = place_of("yasmine")
        self.assertEqual((user.phone, user.full_name, user.must_change_password, user.has_totp),
                         ("01098765432", "د. ياسمين علي", False, False))
        self.assertTrue(user.check_password("a-long-pass-phrase"))
        entry = link.clinic
        self.assertEqual((entry.name, entry.kind, entry.status), ("عيادة ياسمين", "clinic", "active"))
        with clinic_transaction(entry.id, entry.shard):
            clinic = Clinic.objects.get(id=entry.id)
            self.assertEqual((clinic.city, clinic.phone, clinic.specialty_code), ("المنصورة", "0502345678", "dental"))
            [section] = Section.objects.filter(clinic_id=entry.id)
            owner = Membership.objects.get(clinic_id=entry.id, role="owner")
            self.assertEqual(owner.user_id, user.id)
            self.assertTrue(Service.objects.filter(clinic_id=entry.id, section=section, kind="consultation").exists())
            sessions = list(Schedule.objects.filter(clinic_id=entry.id).order_by("weekday"))
        # A first session so the place can book at once: Saturday to Thursday, 17:00-23:00, a queue.
        self.assertEqual(sorted(s.weekday for s in sessions), [0, 1, 2, 3, 5, 6])
        self.assertEqual({(s.doctor_id, s.section_id, s.starts, s.ends, s.mode) for s in sessions},
                         {(owner.id, section.id, dt.time(17), dt.time(23), "queue")})
        events = list(AccountEvent.objects.filter(user_id=user.id).values_list("kind", "ip"))
        self.assertIn(("signup", "10.0.0.1"), events)
        # Signed in, in the new place, and sent to set up two-step sign-in like any owner.
        me = client.get("/api/v1/me").json()
        self.assertEqual((me["user"]["username"], me["active"]["clinic_id"], me["clinics"][0]["kind"]),
                         ("yasmine", entry.id, "clinic"))
        r = call(client, "GET", "/api/v1/clinic", membership=me["active"]["membership_id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "mfa_setup_required"))

    def test_a_center_takes_its_sections(self):
        data = body(kind="center", sections=[{"specialty": "pediatrics", "name": "الأطفال"}, {"specialty": "dermatology"},
                                             {"specialty": "dental", "name": "تجميل الأسنان"}])
        self.assertEqual(signup(data=data).status_code, 201)
        _user, link = place_of("yasmine")
        self.assertEqual(link.clinic.kind, "center")
        with clinic_transaction(link.clinic_id, link.clinic.shard):
            sections = list(Section.objects.filter(clinic_id=link.clinic_id).order_by("sort").values_list("name", "specialty_code"))
            first = Section.objects.filter(clinic_id=link.clinic_id).order_by("sort").first()
            self.assertEqual(set(Schedule.objects.filter(clinic_id=link.clinic_id).values_list("section_id", flat=True)),
                             {first.id})
            self.assertEqual(Clinic.objects.get(id=link.clinic_id).specialty_code, "pediatrics")
        self.assertEqual(sections, [("الأطفال", "pediatrics"), ("جلدية", "dermatology"), ("تجميل الأسنان", "dental")])

    def test_bad_input_is_a_clear_400_with_the_field(self):
        cases = [
            (body(kind="shop"), "kind"), (body(sections=[]), "sections"),
            (body(kind="center"), "sections"), (body(sections=[{"specialty": "x"}]), "sections.0.specialty"),
            (body(name=""), "name"), (body(city=""), "city"), (body(phone="12"), "phone"), ({**body(), "owner": None}, "owner"),
            (body(owner={"full_name": ""}), "owner.full_name"), (body(owner={"mobile": "0123"}), "owner.mobile"),
            (body(owner={"username": "01012345678"}), "owner.username"), (body(owner={"username": "ab"}), "owner.username"),
            (body(owner={"username": "ياسمين"}), "owner.username"), (body(owner={"password": "1234567890"}), "owner.password"),
            (body(owner={"password": "short"}), "owner.password"), (body(owner={"password": "yasmine123"}), "owner.password"),
        ]
        for data, field in cases:
            r = signup(data=data)
            self.assertEqual(r.status_code, 400, (field, r.content))
            self.assertIn(field, r.json()["error"]["fields"], (field, r.content))
        self.assertFalse(User.objects.filter(username="yasmine").exists())
        self.assertFalse(DirectoryClinic.objects.exists())
        self.assertEqual(signup(data=body(), key="not-a-uuid").status_code, 400)

    def test_a_taken_username_or_mobile_is_409_with_fields_and_creates_nothing(self):
        self.user("yasmine")
        self.user("other", phone="01098765432")
        r = signup()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "username_taken"))
        self.assertEqual(r.json()["error"]["fields"], {"owner.username": "taken", "owner.mobile": "taken"})
        r = signup(data=body(owner={"username": "yasmine2"}))
        self.assertEqual((r.status_code, r.json()["error"]["code"], r.json()["error"]["fields"]),
                         (409, "phone_taken", {"owner.mobile": "taken"}))
        r = signup(data=body(owner={"username": "YASMINE2", "mobile": "+201098765431"}))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(DirectoryClinic.objects.count(), 1)

    def test_the_honeypot_answers_like_success_and_creates_nothing(self):
        client = Client()
        r = signup(client, data=body(website="http://spam.example"))
        self.assertEqual((r.status_code, r.json()), (201, {"next": "security"}))
        self.assertFalse(User.objects.filter(username="yasmine").exists())
        self.assertFalse(DirectoryClinic.objects.exists())
        self.assertEqual(client.get("/api/v1/me").status_code, 401)
        self.assertTrue(AccountEvent.objects.filter(kind="signup_honeypot", ip="10.0.0.1").exists())

    def test_three_places_an_hour_per_address_then_429(self):
        for i in range(3):
            r = signup(data=body(owner={"username": f"owner{i}", "mobile": f"0109876543{i}"}))
            self.assertEqual(r.status_code, 201, r.content)
        r = signup(data=body(owner={"username": "owner9", "mobile": "01098765439"}))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "too_many_signups"))
        self.assertTrue(0 < int(r["Retry-After"]) <= 3600)
        self.assertEqual(signup(data=body(owner={"username": "owner9", "mobile": "01098765439"}), ip="10.0.0.2").status_code,
                         201)

    def test_ten_a_day_and_twenty_attempts_an_hour(self):
        from accounts import throttle

        for _ in range(10):
            throttle.place_take("signup_d", "10.0.0.3")
        r = signup(ip="10.0.0.3")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(3600 < int(r["Retry-After"]) or dt.datetime.now(dt.UTC).hour == 23)
        # Taken usernames don't use up the hourly sign-ups, but every attempt counts toward 20 an hour.
        self.user("yasmine")
        for _ in range(20):
            self.assertEqual(signup(ip="10.0.0.4").status_code, 409)
        self.assertEqual(signup(data=body(owner={"username": "free"}), ip="10.0.0.4").status_code, 429)
        self.assertEqual(Throttle.objects.get(scope="signup_try", key="10.0.0.4").failures, 20)
        self.assertFalse(Throttle.objects.filter(scope="signup_h", key="10.0.0.4").exists())

    def test_a_repeat_replays_and_signs_the_browser_in_without_a_second_account(self):
        key = uuid.uuid4()
        first = signup(key=key)
        lost = Client()  # the first answer never arrived: the browser sends again
        again = signup(lost, key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.json(), {"next": "security"})
        self.assertEqual(User.objects.filter(username="yasmine").count(), 1)
        self.assertEqual(DirectoryClinic.objects.count(), 1)
        self.assertEqual(lost.get("/api/v1/me").json()["user"]["username"], "yasmine")
        reused = signup(data=body(city="طنطا"), key=key)
        self.assertEqual((reused.status_code, reused.json()["error"]["code"]), (422, "request_key_reused"))
        # The stored hash never holds the password in a form a fast hash can test.
        stored = AccountRequestKey.objects.get(user_id=0, key=key)
        plain = json.dumps(body(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertNotEqual(stored.request_hash, hashlib.sha256(f"POST /api/v1/signup\n{plain}".encode()).hexdigest())

    def test_a_retry_after_a_refusal_runs_again_with_the_same_key(self):
        self.user("yasmine")
        key = uuid.uuid4()
        self.assertEqual(signup(key=key).status_code, 409)
        User.objects.filter(username="yasmine").delete()
        self.assertEqual(signup(key=key).status_code, 201)

    def test_csrf_is_checked(self):
        client = Client(enforce_csrf_checks=True)
        r = signup(client)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "csrf_failed"))
        self.assertFalse(User.objects.filter(username="yasmine").exists())


class AddPlaceTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a", "shard_1")

    def post(self, client, data, key=None, ip="10.0.1.1"):
        return client.generic("POST", "/api/v1/me/places", json.dumps(data), content_type="application/json",
                              headers={"Idempotency-Key": str(key or uuid.uuid4())}, REMOTE_ADDR=ip)

    def test_a_signed_in_account_adds_a_place_and_owns_it(self):
        client = self.a.client("doctor")
        data = body(kind="center", sections=[{"specialty": "internal"}, {"specialty": "ophthalmology"}])
        del data["owner"]
        key = uuid.uuid4()
        r = self.post(client, data, key)
        self.assertEqual(r.status_code, 201, r.content)
        answer = r.json()
        doctor = self.a.user("doctor")
        entry = DirectoryClinic.objects.get(id=answer["clinic"]["id"])
        self.assertEqual((answer["clinic"]["kind"], answer["next"]), ("center", "security"))
        link = UserClinic.objects.get(user=doctor, clinic=entry)
        self.assertEqual((link.role, link.membership_id), ("owner", answer["membership_id"]))
        with clinic_transaction(entry.id, entry.shard):
            self.assertEqual(Membership.objects.get(clinic_id=entry.id, role="owner").user_id, doctor.id)
            self.assertEqual(Section.objects.filter(clinic_id=entry.id).count(), 2)
            self.assertEqual(Schedule.objects.filter(clinic_id=entry.id).count(), 6)
        me = client.get("/api/v1/me").json()
        self.assertEqual(me["active"]["clinic_id"], entry.id)  # the new place is now the active one
        self.assertEqual(sorted(c["kind"] for c in me["clinics"]), ["center", "clinic"])
        self.assertTrue(AccountEvent.objects.filter(user_id=doctor.id, kind="place_added").exists())
        again = self.post(client, data, key)
        self.assertEqual((again.status_code, again.json()), (201, answer))
        self.assertEqual(UserClinic.objects.filter(user=doctor, role="owner").count(), 1)
        # An owner with two-step sign-in set up lands at home.
        owner_client = self.a.client("owner")
        self.assertEqual(self.post(owner_client, {**data, "name": "مركز آخر"}).json()["next"], "home")

    def test_refusals(self):
        data = body()
        del data["owner"]
        self.assertEqual(self.post(Client(), data).status_code, 401)
        admin = self.user("support", with_totp=True, is_platform_admin=True)
        client = Client()
        client.force_login(admin)
        session = client.session
        session[SESSION_MFA] = True
        session.save()
        self.assertEqual(self.post(client, data).status_code, 403)
        doctor = self.a.client("doctor")
        r = self.post(doctor, {**data, "sections": [{"specialty": "dental"}, {"specialty": "internal"}]})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (400, {"sections": "count"}))
        for i in range(3):
            self.assertEqual(self.post(doctor, {**data, "name": f"Place {i}"}, ip="10.0.1.9").status_code, 201)
        self.assertEqual(self.post(doctor, {**data, "name": "Place 4"}, ip="10.0.1.9").status_code, 429)


class PlatformCenterTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.user("support", with_totp=True, is_platform_admin=True)
        self.client.force_login(self.admin)
        session = self.client.session
        session[SESSION_MFA] = True
        session.save()

    def test_the_platform_admin_creates_a_center(self):
        r = call(self.client, "POST", "/api/v1/platform/clinics", {
            "name": "مركز النيل", "kind": "center", "owner_username": "nile", "owner_name": "د. نيل",
            "sections": [{"specialty": "dental"}, {"specialty": "pediatrics", "name": "Kids"}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["clinic"]["kind"], "center")
        entry = DirectoryClinic.objects.get(id=r.json()["clinic"]["id"])
        with clinic_transaction(entry.id, entry.shard):
            self.assertEqual(list(Section.objects.filter(clinic_id=entry.id).order_by("sort").values_list("name", flat=True)),
                             ["أسنان", "Kids"])
            self.assertEqual(Schedule.objects.filter(clinic_id=entry.id).count(), 6)
        bad = call(self.client, "POST", "/api/v1/platform/clinics", {
            "name": "x", "kind": "center", "owner_username": "x1", "owner_name": "X", "sections": [{"specialty": "dental"}]})
        self.assertEqual((bad.status_code, bad.json()["error"]["fields"]), (400, {"sections": "count"}))
        self.assertFalse(User.objects.filter(username="x1").exists())


class SignUpRaceTests(TransactionTestCase):
    """Two sign-ups for one username at once, from two addresses: one account, and a 409 for the other."""

    databases = "__all__"

    def test_one_username_twice_at_once(self):
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(index):
            try:
                barrier.wait()
                results[index] = signup(data=body(owner={"mobile": f"0109876543{index}"}), ip=f"10.0.2.{index}")
            except Exception as error:  # surfaced below
                results[index] = error
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(getattr(r, "status_code", r) for r in results), [201, 409], results)
        loser = next(r for r in results if r.status_code == 409)
        self.assertEqual(loser.json()["error"]["fields"], {"owner.username": "taken"})
        self.assertEqual(User.objects.filter(username="yasmine").count(), 1)
        self.assertEqual(DirectoryClinic.objects.count(), 1)
