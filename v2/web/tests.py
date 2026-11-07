import polib
from django.test import Client, SimpleTestCase

from accounts.models import User
from core.i18n import catalogue_path, extract_msgids
from core.testing import ShardTestCase


class CatalogueTests(SimpleTestCase):
    """Replaces makemessages (design 7.2): Arabic has every string, and no unused ones."""

    def test_arabic_has_every_string_and_no_unused_ones(self):
        wanted = set(extract_msgids())
        catalogue = polib.pofile(str(catalogue_path("ar")))
        have = {entry.msgid for entry in catalogue if entry.msgstr and not entry.obsolete}
        self.assertEqual(sorted(wanted - have), [], "missing Arabic translations")
        self.assertEqual(sorted(have - wanted), [], "unused Arabic translations")


class PageTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def test_sign_in_page_is_arabic_and_right_to_left_by_default(self):
        r = Client().get("/login")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("تسجيل الدخول", html)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertIn("Content-Security-Policy", r)

    def test_english_for_a_user_who_chose_it(self):
        doctor = self.a.user("doctor")
        User.objects.filter(pk=doctor.pk).update(language="en")
        r = self.a.client("doctor").get("/")
        self.assertIn('lang="en" dir="ltr"', r.content.decode())

    def test_pages_need_a_signed_in_member(self):
        r = Client().get("/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/login?next=/"))

    def test_home_shows_the_clinic(self):
        r = self.a.client("assistant").get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("a clinic", r.content.decode())

    def test_forced_password_change_redirects(self):
        assistant = self.a.user("assistant")
        User.objects.filter(pk=assistant.pk).update(must_change_password=True)
        r = self.a.client("assistant").get("/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/account/password"))

    def test_owner_without_the_code_is_sent_to_sign_in_again(self):
        r = self.a.client("owner", mfa=False).get("/")
        self.assertEqual((r.status_code, r["Location"]), (302, "/login?step=code"))

    def test_member_of_two_clinics_picks_one(self):
        b = self.clinic("b", "shard_2")
        from directory.flows import add_member

        add_member(b.entry, self.a.user("assistant"), "assistant", "Assistant")
        client = Client()
        client.force_login(self.a.user("assistant"))
        r = client.get("/clinics")
        html = r.content.decode()
        self.assertIn("a clinic", html)
        self.assertIn("b clinic", html)
