import json

from django.test import Client

from accounts import totp
from accounts.models import User
from core.testing import ShardTestCase, call


def post(client, url, body):
    return client.post(url, json.dumps(body), content_type="application/json")


class SignInTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def test_wrong_password_and_unknown_user_answer_the_same(self):
        r1 = post(Client(), "/api/v1/auth/login", {"username": "a_doctor", "password": "nope"})
        r2 = post(Client(), "/api/v1/auth/login", {"username": "nobody", "password": "nope"})
        self.assertEqual((r1.status_code, r2.status_code), (401, 401))
        self.assertEqual(r1.json()["error"]["code"], r2.json()["error"]["code"])

    def test_failures_are_throttled_per_username_and_ip(self):
        codes = [post(Client(), "/api/v1/auth/login", {"username": "a_doctor", "password": "nope"}).status_code
                 for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        blocked = post(Client(), "/api/v1/auth/login", {"username": "a_doctor", "password": "pass-word-123"})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked)

    def test_doctor_signs_in_and_lands_in_the_only_clinic(self):
        client = Client()
        r = post(client, "/api/v1/auth/login", {"username": "A_Doctor", "password": "pass-word-123"})
        self.assertEqual((r.status_code, r.json()["next"]), (200, "home"))
        me = client.get("/api/v1/me").json()
        self.assertEqual(me["active"]["clinic_id"], self.a.id)

    def test_owner_needs_the_code_and_each_code_works_once(self):
        owner = self.a.user("owner")
        client = Client()
        r = post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        self.assertEqual(r.json()["next"], "code")
        self.assertEqual(client.get("/api/v1/me").status_code, 401)  # pending isn't signed in
        code = totp.code_at(owner.plain_totp_secret, totp.current_step())
        self.assertEqual(post(client, "/api/v1/auth/code", {"code": code}).status_code, 200)
        # The same code on another device is refused.
        other = Client()
        post(other, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        self.assertEqual(post(other, "/api/v1/auth/code", {"code": code}).status_code, 400)

    def test_five_wrong_codes_end_the_pending_sign_in(self):
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        codes = [post(client, "/api/v1/auth/code", {"code": "000000"}).status_code for _ in range(5)]
        self.assertEqual(codes, [400, 400, 400, 400, 401])

    def test_a_phone_up_to_two_minutes_off_still_signs_in(self):
        # Saged, 2026-11-25: a doctor's code was refused again and again.
        owner = self.a.user("owner")
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        code = totp.code_at(owner.plain_totp_secret, totp.current_step() - 4)  # the phone is two minutes slow
        self.assertEqual(post(client, "/api/v1/auth/code", {"code": code}).status_code, 200)

    def test_a_phone_whose_clock_is_wrong_is_told_so(self):
        owner = self.a.user("owner")
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        code = totp.code_at(owner.plain_totp_secret, totp.current_step() + 10)  # five minutes fast
        r = post(client, "/api/v1/auth/code", {"code": code})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "clock_off"))
        self.assertEqual(client.get("/api/v1/me").status_code, 401)  # never accepted

    def test_a_device_that_passed_the_code_is_not_asked_again_for_30_days(self):
        owner = self.a.user("owner")
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        r = post(client, "/api/v1/auth/code", {"code": totp.code_at(owner.plain_totp_secret, totp.current_step())})
        cookie = r.cookies["hamada_device"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(int(cookie["max-age"]), 30 * 24 * 3600)
        post(client, "/api/v1/auth/logout", {})
        again = post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        self.assertNotEqual(again.json()["next"], "code")
        self.assertEqual(client.get("/api/v1/me").status_code, 200)
        # Another device still needs the code; several devices can each be remembered.
        other = Client()
        self.assertEqual(post(other, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"}).json()["next"], "code")
        # A mark copied to another account, or one from before a password change, is worthless.
        fresh = Client()
        fresh.cookies["hamada_device"] = cookie.value
        user = User.objects.get(pk=owner.pk)
        user.set_password("another-pass-word-456")
        user.save(update_fields=["password"])
        self.assertEqual(post(fresh, "/api/v1/auth/login", {"username": "a_owner", "password": "another-pass-word-456"}).json()["next"], "code")
        forged = Client()
        forged.cookies["hamada_device"] = cookie.value + "x"
        self.assertEqual(post(forged, "/api/v1/auth/login", {"username": "a_owner", "password": "another-pass-word-456"}).json()["next"], "code")

    def _trusted_cookie(self, place):
        user = place.user("owner")
        username = user.username
        client = Client()
        post(client, "/api/v1/auth/login", {"username": username, "password": "pass-word-123"})
        r = post(client, "/api/v1/auth/code", {"code": totp.code_at(user.plain_totp_secret, totp.current_step())})
        return r.cookies["hamada_device"].value

    def test_a_device_mark_is_for_one_account_and_dies_with_a_new_code_secret(self):
        self.clinic("b")
        mark = self._trusted_cookie(self.a)
        other = Client()
        other.cookies["hamada_device"] = mark
        self.assertEqual(post(other, "/api/v1/auth/login", {"username": "b_owner", "password": "pass-word-123"}).json()["next"], "code")
        # Setting the code up again (a lost phone) forgets every device.
        User.objects.filter(username="a_owner").update(totp_secret=User.objects.get(username="b_owner").totp_secret)
        again = Client()
        again.cookies["hamada_device"] = mark
        self.assertEqual(post(again, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"}).json()["next"], "code")

    def test_the_device_mark_is_secure_on_the_live_site(self):
        from django.test import override_settings

        with override_settings(SESSION_COOKIE_SECURE=True):
            owner = self.a.user("owner")
            client = Client()
            post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
            r = post(client, "/api/v1/auth/code", {"code": totp.code_at(owner.plain_totp_secret, totp.current_step())})
            self.assertTrue(r.cookies["hamada_device"]["secure"])

    def test_a_clock_off_answer_is_still_a_strike(self):
        owner = self.a.user("owner")
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        codes = [post(client, "/api/v1/auth/code", {"code": totp.code_at(owner.plain_totp_secret, totp.current_step() + 10 + i)})
                 .status_code for i in range(5)]
        self.assertEqual(codes, [400, 400, 400, 400, 401])

    def test_wrong_codes_are_capped_per_day_but_a_recovery_code_still_works(self):
        from accounts import crypto
        from accounts.models import RecoveryCode

        owner = self.a.user("owner")
        for _ in range(3):  # 3 sign-ins x 5 wrong codes = the day's 15
            client = Client()
            post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
            for _ in range(5):
                post(client, "/api/v1/auth/code", {"code": "000000"})
        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_owner", "password": "pass-word-123"})
        right = totp.code_at(owner.plain_totp_secret, totp.current_step())
        self.assertEqual(post(client, "/api/v1/auth/code", {"code": right}).status_code, 429)
        RecoveryCode.objects.create(user_id=owner.pk, code_hash=crypto.hash_recovery_code("abcde-12345"))
        self.assertEqual(post(client, "/api/v1/auth/code", {"code": "abcde-12345"}).status_code, 200)

    def test_signing_in_by_phone_meets_the_accounts_limit(self):
        from accounts import throttle
        from core.text import normalize_phone

        owner = self.a.user("owner")
        User.objects.filter(pk=owner.pk).update(phone=normalize_phone("01012345678"))
        for _ in range(throttle.LIMITS["user"]):
            throttle.record_failure([("user", "a_owner")])
        r = post(Client(), "/api/v1/auth/login", {"username": "01012345678", "password": "pass-word-123"})
        self.assertEqual(r.status_code, 429)

    def test_owner_without_the_code_set_up_is_sent_to_set_it_up(self):
        owner = self.a.user("owner")
        User.objects.filter(pk=owner.pk).update(totp_secret="", totp_confirmed_at=None)
        client = self.a.client("owner", mfa=False)
        r = call(client, "GET", "/api/v1/clinic")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "mfa_setup_required"))

    def test_owner_session_without_the_code_is_refused(self):
        r = call(self.a.client("owner", mfa=False), "GET", "/api/v1/clinic")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "mfa_required"))

    def test_set_up_gives_ten_recovery_codes_that_work_once(self):
        doctor = self.a.user("doctor")
        client = self.a.client("doctor")
        setup = client.get("/api/v1/me/totp").json()
        self.assertIn("<svg", setup["qr_svg"])
        code = totp.code_at(setup["secret"].replace(" ", ""), totp.current_step())
        r = call(client, "POST", "/api/v1/me/totp/confirm", {"code": code})
        self.assertEqual(r.status_code, 200)
        recovery = r.json()["recovery_codes"]
        self.assertEqual(len(recovery), 10)
        doctor.refresh_from_db()
        self.assertTrue(doctor.has_totp)
        for attempt, expected in [(recovery[0], 200), (recovery[0], 400)]:
            c = Client()
            post(c, "/api/v1/auth/login", {"username": "a_doctor", "password": "pass-word-123"})
            self.assertEqual(post(c, "/api/v1/auth/code", {"code": attempt}).status_code, expected)


class PasswordTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def test_temporary_password_must_be_changed_before_anything_else(self):
        assistant = self.a.user("assistant")
        User.objects.filter(pk=assistant.pk).update(must_change_password=True)
        client = self.a.client("assistant")
        r = call(client, "GET", "/api/v1/clinic")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "password_change_required"))
        r = call(client, "POST", "/api/v1/me/password", {"current": "pass-word-123", "new": "a-much-better-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(call(client, "GET", "/api/v1/clinic").status_code, 200)

    def test_changing_the_password_signs_out_other_sessions(self):
        first = self.a.client("doctor")
        second = self.a.client("doctor")
        self.assertEqual(call(second, "GET", "/api/v1/clinic").status_code, 200)
        r = call(first, "POST", "/api/v1/me/password", {"current": "pass-word-123", "new": "a-much-better-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(call(first, "GET", "/api/v1/clinic").status_code, 200)
        self.assertEqual(call(second, "GET", "/api/v1/clinic").status_code, 401)

    def test_weak_password_is_refused(self):
        r = call(self.a.client("doctor"), "POST", "/api/v1/me/password", {"current": "pass-word-123", "new": "1234567890"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "weak_password"))


class SessionRenewalTests(ShardTestCase):
    """A sign-in lasts while it is used: each use pushes the end back (Saged, 2026-11-22)."""

    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def test_using_the_site_pushes_the_sign_in_end_back(self):
        import datetime as dt

        from django.conf import settings
        from django.contrib.sessions.models import Session
        from django.utils import timezone

        from core.middleware import SESSION_RENEWED

        client = Client()
        post(client, "/api/v1/auth/login", {"username": "a_doctor", "password": "pass-word-123"})
        key = client.cookies[settings.SESSION_COOKIE_NAME].value
        stale = timezone.now() - dt.timedelta(days=3)
        session = client.session
        session[SESSION_RENEWED] = stale.timestamp()
        session.save()
        Session.objects.filter(session_key=key).update(expire_date=timezone.now() + dt.timedelta(hours=1))

        self.assertEqual(client.get("/api/v1/me").status_code, 200)
        expires = Session.objects.get(session_key=key).expire_date
        self.assertGreater(expires, timezone.now() + dt.timedelta(seconds=settings.SESSION_COOKIE_AGE - 60))
        self.assertGreater(settings.SESSION_COOKIE_AGE, 7 * 24 * 3600)
