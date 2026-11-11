import re

import polib
from django.test import Client, SimpleTestCase
from django.utils import translation

from accounts.models import User
from audit.models import AuditAccess
from clinics.models import Clinic, Membership
from clinics.specialties import SPECIALTIES
from core.i18n import catalogue_path, extract_msgids
from core.routing import clinic_transaction
from core.testing import ShardTestCase
from scheduling.testing import M1TestCase
from web import format as fmt
from web.http import route_exists


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
        self.assertIn('href="/places/new"', html)  # "add a place" (design D3)


# --- M1 screens (design 7, 10 T15) -------------------------------------------------------------------------------


def fragment(client, url="/today/queue", etag=None, membership=True):
    headers = {}
    if membership:
        headers["X-Membership"] = str(client.membership_id)
    if etag:
        headers["If-None-Match"] = etag
    return client.get(url, headers=headers)


def access_count(fixture, action):
    with clinic_transaction(fixture.id, fixture.shard):
        return AuditAccess.objects.filter(clinic_id=fixture.id, action=action).count()


class TodayTests(M1TestCase):
    def test_today_is_the_home_page_in_arabic_with_the_side_navigation(self):
        self.book(arrive=True)
        r = self.desk.get("/")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("اليوم في العيادة", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("Mohamed Ahmed", html)
        self.assertIn('data-act="call"', html)  # the waiting row offers "call in"

    def test_navigation_follows_permissions_and_marks_unbuilt_sections(self):
        desk = self.desk.get("/").content.decode()
        owner = self.owner.get("/").content.decode()
        self.assertNotIn("الحسابات", desk)  # finances: owner-only codes
        self.assertIn("الحسابات", owner)
        self.assertIn("قريباً", owner)      # not built in M1
        self.assertIn('href="/patients"', desk)

    def test_the_fragment_answers_304_while_nothing_changes(self):
        first = fragment(self.desk)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first["Poll-After"], "5")
        self.assertEqual(first["Cache-Control"], "no-store")
        self.assertIn("X-Server-Time", first)
        again = fragment(self.desk, etag=first["ETag"])
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again["Poll-After"], "5")
        self.assertEqual(again["ETag"], first["ETag"])
        self.book()
        changed = fragment(self.desk, etag=first["ETag"])
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], first["ETag"])
        self.assertIn("Mohamed Ahmed", changed.content.decode())

    def test_the_page_carries_the_etag_its_rows_were_built_with(self):
        self.book()
        page = self.desk.get("/").content.decode()
        etag = re.search(r'data-etag="([^"]+)"', page).group(1).replace("&quot;", '"')
        self.assertEqual(fragment(self.desk, etag=etag).status_code, 304)

    def test_the_fragment_refuses_like_the_api(self):
        self.assertEqual(fragment(self.desk, membership=False).json()["error"]["code"], "clinic_changed")
        self.assertEqual(Client().get("/today/queue", headers={"X-Membership": "1"}).status_code, 401)
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership("assistant").id).update(
                revoked=["appointments.book", "appointments.queue"])
        self.assertEqual(fragment(self.desk).status_code, 403)
        self.assertEqual(self.desk.get("/")["Location"], "/patients")  # Today is gone: off to the patients

    def test_rows_never_show_another_clinic(self):
        self.book(fixture=self.b, new_patient={"full_name": "Other Clinic Patient"})
        self.assertNotIn("Other Clinic Patient", fragment(self.desk).content.decode())

    def test_the_clinic_digit_setting_applies(self):
        self.book(arrive=True)
        with clinic_transaction(self.a.id, self.a.shard):
            clinic = Clinic.objects.get(id=self.a.id)
            Clinic.objects.filter(id=self.a.id).update(settings={**clinic.settings, "digits": "arab"})
        self.assertIn("٠١٠ ١٢٣٤ ٥٦٧٨", fragment(self.desk).content.decode())
        self.assertEqual(fmt.digit_setting(), "latn")  # reset after the request: a worker thread serves many clinics

    def test_phones_keep_their_digit_order_in_right_to_left_text(self):
        self.book()
        self.assertIn('<bdi class="ltr num nowrap">010 1234 5678</bdi>', fragment(self.desk).content.decode())

    def test_undo_is_offered_once_the_endpoint_exists(self):
        a = self.book(arrive=True).json()["appointment"]
        html = fragment(self.desk).content.decode()
        self.assertEqual('data-act="undo-arrive"' in html, route_exists(f"/api/v1/appointments/{a['id']}/undo-arrive"))


class PatientPageTests(M1TestCase):
    def test_search_fragment_lists_matches_without_an_access_event(self):
        self.book()
        before = access_count(self.a, AuditAccess.Action.OPEN)
        r = fragment(self.desk, "/patients/search?q=moh&pick=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("data-pick-patient", r.content.decode())
        self.assertEqual(access_count(self.a, AuditAccess.Action.OPEN), before)

    def test_opening_the_file_writes_one_access_event(self):
        patient = self.book().json()["appointment"]["patient"]["id"]
        before = access_count(self.a, AuditAccess.Action.OPEN)
        r = self.desk.get(f"/patients/{patient}")
        self.assertEqual(r.status_code, 200)
        title = re.search(r"<title>(.*?)</title>", r.content.decode(), re.S).group(1)
        self.assertNotIn("Mohamed", title)
        self.assertEqual(access_count(self.a, AuditAccess.Action.OPEN), before + 1)

    def test_another_clinics_patient_is_not_found(self):
        other = self.book(fixture=self.b).json()["appointment"]["patient"]["id"]
        self.assertEqual(self.desk.get(f"/patients/{other}").status_code, 404)
        self.assertEqual(fragment(self.desk, f"/patients/{other}/visits").status_code, 404)

    def test_load_more_pages_with_the_cursor(self):
        patient = self.book().json()["appointment"]["patient"]["id"]
        r = fragment(self.desk, f"/patients/{patient}/visits")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Count"], "1")
        self.assertEqual(r["X-Next"], "")


class ReceiptTests(M1TestCase):
    def paid(self, fixture=None):
        return self.book(fixture=fixture, arrive=True, payment={"amount": 30000, "method": "cash"}).json()["appointment"]

    def test_printing_writes_an_access_event_and_keeps_the_name_out_of_the_title(self):
        a = self.paid()
        before = access_count(self.a, AuditAccess.Action.PRINT)
        r = self.desk.get(f"/print/receipts/{a['invoice']['id']}?format=80mm")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("print-80mm.css", html)
        self.assertNotIn("Mohamed", re.search(r"<title>(.*?)</title>", html, re.S).group(1))
        self.assertIn("Mohamed Ahmed", html)
        self.assertEqual(access_count(self.a, AuditAccess.Action.PRINT), before + 1)

    def test_language_and_paper_come_from_the_request(self):
        a = self.paid()
        html = self.desk.get(f"/print/receipts/{a['invoice']['id']}?lang=en&format=a5").content.decode()
        self.assertIn('lang="en" dir="ltr"', html)
        self.assertIn("print-a5.css", html)
        self.assertIn("EGP", html)

    def test_another_clinics_invoice_is_not_found_and_not_logged(self):
        other = self.paid(fixture=self.b)
        before = access_count(self.b, AuditAccess.Action.PRINT)
        self.assertEqual(self.desk.get(f"/print/receipts/{other['invoice']['id']}").status_code, 404)
        self.assertEqual(access_count(self.b, AuditAccess.Action.PRINT), before)


class SignupPageTests(ShardTestCase):
    def test_sign_up_is_public_and_arabic(self):
        r = Client().get("/signup")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('lang="ar" dir="rtl"', html)
        for spec in SPECIALTIES.values():
            self.assertIn(spec["name"]["ar"], html)
        self.assertIn('name="website"', html)  # the honeypot
        self.assertIn("hamada_csrftoken", r.cookies)

    def test_the_sign_in_page_links_to_sign_up(self):
        html = Client().get("/login").content.decode()
        self.assertIn('href="/signup"', html)
        self.assertIn("عيادة أو مركز جديد؟", html)

    def test_a_signed_in_person_adds_a_place_instead(self):
        client = Client()
        client.force_login(self.user("someone"))
        self.assertEqual(client.get("/signup")["Location"], "/places/new")
        self.assertEqual(client.get("/places/new").status_code, 200)


class FormatTests(SimpleTestCase):
    def test_money_from_piastres(self):
        self.assertEqual(fmt.amount_text(125050), "1,250.50")
        self.assertEqual(fmt.amount_text(30000), "300")
        with translation.override("ar"):
            self.assertIn("ج.م", str(fmt.money(30000)))

    def test_phone_groups(self):
        self.assertEqual(fmt.phone("01012345678"), "010 1234 5678")
        self.assertEqual(fmt.phone("+441234"), "+441234")

    def test_clock_shows_the_wall_clock_of_the_clinics_offset(self):
        with translation.override("ar"):
            self.assertEqual(fmt.clock_text("2026-07-15T20:05:00+03:00"), "8:05 م")
