"""Overview, the owner's home (Saged, 2026-11-12), and the side bar of an owner who manages, also treats, or runs
the clinic alone (Saged, 2026-11-13)."""

import datetime as dt
import re

from clinics.models import Clinic, Membership, Schedule, Section
from core.routing import clinic_transaction
from core.testing import ShardTestCase


def nav_links(html):
    side = html.split('class="main-nav"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'class="nav-btn" (?:data-)?href="([^"]+)"', side)  # a link, or an entry with parts


class OverviewCase(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")
        self.owner_id = self.a.membership("owner").id

    def sessions(self, keep):
        """The owner works as a doctor (keep=True: a session of theirs exists) or only manages."""
        with clinic_transaction(self.a.id, self.a.shard):
            mine = Schedule.objects.filter(clinic_id=self.a.id, doctor_id=self.owner_id)
            if not keep:
                mine.delete()
            elif not mine.exists():
                section = Section.objects.filter(clinic_id=self.a.id).values_list("id", flat=True).first()
                Schedule.objects.create(clinic_id=self.a.id, doctor_id=self.owner_id, section_id=section, weekday=0,
                                        starts=dt.time(9), ends=dt.time(17), mode="queue")
        return self.a.client("owner")

    def codes(self, role, granted=(), revoked=()):
        with clinic_transaction(self.a.id, self.a.shard):
            Membership.objects.filter(clinic_id=self.a.id, id=self.a.membership(role).id).update(
                granted=list(granted), revoked=list(revoked))
        return self.a.client(role)


class OwnerNavTests(OverviewCase):
    def test_an_owner_who_only_manages_lands_on_the_overview(self):
        owner = self.sessions(keep=False)
        r = owner.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/overview")
        html = owner.get("/overview").content.decode()
        self.assertEqual(nav_links(html), ["/overview", "/alerts", "/finances", "/prices", "/team", "/patients"])
        self.assertNotIn("data-book-patient", owner.get("/patients").content.decode())  # lookup only
        self.assertEqual(owner.get("/today")["Location"], "/overview")

    def test_an_owner_who_also_treats_gets_their_own_queue_without_booking(self):
        owner = self.sessions(keep=True)
        r = owner.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertEqual(nav_links(html), ["/", "/prescriptions", "/prices", "/overview", "/alerts", "/finances", "/team", "/patients"])
        for marker in ("data-open-walkin", "data-open-book", 'id="walkin-dialog"'):
            self.assertNotIn(marker, html, marker)  # booking stays the desk's
        self.assertEqual(owner.get("/prescriptions").status_code, 200)
        self.assertNotIn("data-book-patient", owner.get("/patients").content.decode())

    def test_a_solo_owner_sees_everything_and_books(self):
        self.sessions(keep=True)
        with clinic_transaction(self.a.id, self.a.shard):
            Clinic.objects.filter(id=self.a.id).update(settings={"solo": True})
        owner = self.a.client("owner")
        r = owner.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for link in ("/", "/overview", "/patients", "/finances", "/prescriptions", "/team"):
            self.assertIn(link, nav_links(html), link)
        self.assertIn("data-open-book", html)
        self.assertEqual(owner.get("/prescriptions").status_code, 200)
        self.assertEqual(owner.get("/overview").status_code, 200)

    def test_solo_never_applies_to_a_center(self):
        center = self.clinic("c", sections=[{"specialty": "dental", "name": "Teeth"}, {"specialty": "pediatrics", "name": "Kids"}])
        with clinic_transaction(center.id, center.shard):
            Clinic.objects.filter(id=center.id).update(settings={"solo": True})
            Schedule.objects.filter(clinic_id=center.id, doctor_id=center.membership("owner").id).delete()
        owner = center.client("owner")
        self.assertEqual(owner.get("/")["Location"], "/overview")  # stored by mistake: still a manager

    def test_others_are_unchanged(self):
        desk = self.a.client("assistant")
        self.assertEqual(desk.get("/").status_code, 200)
        self.assertNotIn("/overview", nav_links(desk.get("/patients").content.decode()))
        self.assertEqual(desk.get("/overview").status_code, 403)


class OverviewPageTests(OverviewCase):
    def test_the_owner_sees_performance_money_and_insights(self):
        owner = self.sessions(keep=False)
        for period in ("today", "week", "month"):
            r = owner.get(f"/overview?period={period}")
            html = r.content.decode()
            self.assertEqual(r.status_code, 200, period)
            self.assertIn(f'href="?period={period}" aria-current="page"', html)
            self.assertIn('data-ov="visits"', html)
            self.assertIn('data-ov="revenue"', html)
            self.assertIn("/finances?tab=owed", html)
        self.assertEqual(owner.get("/overview?period=nonsense").status_code, 200)

    def test_reports_without_finances_shows_no_money(self):
        doctor = self.codes("doctor", granted=["reports.view"])
        r = doctor.get("/overview")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-ov="visits"', html)
        for marker in ('data-ov="revenue"', "/finances?tab=owed", "الربح", "الإيراد"):
            self.assertNotIn(marker, html, marker)
        self.assertEqual(nav_links(html)[:3], ["/", "/follow-ups", "/overview"])

    def test_reports_without_a_queue_goes_home_to_the_overview(self):
        viewer = self.codes("assistant", granted=["reports.view"], revoked=["appointments.book", "appointments.queue"])
        r = viewer.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/overview")
