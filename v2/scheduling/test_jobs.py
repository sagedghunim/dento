"""The nightly jobs visit only places whose business day has just ended (draft 2 check, I6), as Procrastinate tasks."""

import datetime as dt
from zoneinfo import ZoneInfo

from clinics.models import Clinic
from core.routing import clinic_transaction
from directory.models import DirectoryClinic
from scheduling import jobs
from scheduling.models import Appointment
from scheduling.testing import M1TestCase, frozen, summaries

CAIRO = ZoneInfo("Africa/Cairo")


def cairo(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=CAIRO)


class DuePlaceTests(M1TestCase):
    """A in Cairo (day ends 04:00) on shard 1; B in Riyadh with the day ending at 02:00 on shard 2."""

    def setUp(self):
        super().setUp()
        with clinic_transaction(self.b.id, self.b.shard):
            Clinic.objects.filter(id=self.b.id).update(timezone="Asia/Riyadh", day_ends_at=dt.time(2, 0))
        DirectoryClinic.objects.filter(id=self.b.id).update(timezone="Asia/Riyadh", day_ends_at=dt.time(2, 0))

    def due(self, job, now, **kwargs):
        return {row["id"]: row["closed"] for row in jobs.targets(job, now, **kwargs)}

    def test_each_place_is_visited_once_after_its_own_day_ends(self):
        first = cairo("2026-11-11T01:00")  # 02:00 in Riyadh: B's day ended 30 minutes ago only at 02:30
        self.assertEqual(self.due("sweep", first), {self.a.id: dt.date(2026, 11, 9), self.b.id: dt.date(2026, 11, 9)})
        jobs.run_sweep(now=first)
        self.assertEqual(self.due("sweep", first), {})  # done: nothing left to visit
        # 01:31 Cairo is 02:31 Riyadh: B's day of 10 November has ended, A's hasn't.
        self.assertEqual(self.due("sweep", cairo("2026-11-11T01:31")), {self.b.id: dt.date(2026, 11, 10)})
        jobs.run_sweep(now=cairo("2026-11-11T01:31"))
        self.assertEqual(self.due("sweep", cairo("2026-11-11T04:29")), {})
        self.assertEqual(self.due("sweep", cairo("2026-11-11T04:30")), {self.a.id: dt.date(2026, 11, 10)})
        self.assertEqual(self.due("sweep", cairo("2026-11-11T04:30"), shard="shard_2"), {})
        self.assertEqual((DirectoryClinic.objects.get(id=self.b.id).swept_through,
                          DirectoryClinic.objects.get(id=self.a.id).swept_through),
                         (dt.date(2026, 11, 10), dt.date(2026, 11, 9)))

    def test_the_sweep_closes_the_day_through_the_due_list(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            booked = self.book(client=desk).json()["appointment"]
        jobs.run_sweep(now=cairo("2026-11-11T04:29"))
        self.assertEqual(self.appointment(booked["id"]).status, "booked")
        self.assertEqual(jobs.run_sweep(now=cairo("2026-11-11T04:30")), {"no_show": 1})
        self.assertEqual(self.appointment(booked["id"]).status, "no_show")
        self.assertEqual(jobs.run_sweep(now=cairo("2026-11-11T04:40")), {})

    def test_a_suspended_place_is_skipped_and_a_failure_doesnt_stop_the_others(self):
        DirectoryClinic.objects.filter(id=self.b.id).update(status="suspended")
        self.assertEqual(set(self.due("sweep", cairo("2026-11-11T05:00"))), {self.a.id})
        DirectoryClinic.objects.filter(id=self.b.id).update(status="active", shard="shard_9")  # not configured
        totals = jobs.run_sweep(now=cairo("2026-11-11T05:00"))
        self.assertEqual(totals.get("failed"), 1)
        self.assertEqual(DirectoryClinic.objects.get(id=self.a.id).swept_through, dt.date(2026, 11, 10))
        self.assertIsNone(DirectoryClinic.objects.get(id=self.b.id).swept_through)  # still due next time

    def test_the_sweep_repairs_a_wrong_directory_copy(self):
        DirectoryClinic.objects.filter(id=self.b.id).update(timezone="Africa/Cairo", day_ends_at=dt.time(4, 0),
                                                            kind="center")
        with self.assertLogs("scheduling.jobs", "WARNING"):
            jobs.run_sweep(now=cairo("2026-11-11T05:00"))
        entry = DirectoryClinic.objects.get(id=self.b.id)
        self.assertEqual((entry.timezone, entry.day_ends_at, entry.kind), ("Asia/Riyadh", dt.time(2, 0), "clinic"))
        # The mark is the shard's truth: Riyadh's day of 10 November ended at 02:00 there.
        self.assertEqual(entry.swept_through, dt.date(2026, 11, 10))

    def test_the_recompute_waits_for_the_sweep_and_runs_once_a_day(self):
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            appt = self.book(client=desk, arrive=True, payment={"amount": 5000, "method": "cash"}).json()["appointment"]
        now = cairo("2026-11-11T04:30")
        self.assertEqual(self.due("recompute", now), {})  # nothing swept yet
        jobs.run_sweep(now=now)
        self.assertEqual(set(self.due("recompute", now)), {self.a.id, self.b.id})
        with frozen(now):
            self.assertEqual(jobs.run_recompute(now=now), [])
        self.assertEqual(self.due("recompute", now), {})
        self.assertFalse(summaries(self.a, dt.date(2026, 11, 10))[0]["changed_since_recompute"])
        row = self.appointment(appt["id"])  # arrived and never seen: on the new day (Saged, 2026-11-24)
        self.assertEqual((row.status, row.day), ("waiting", dt.date(2026, 11, 11)))
        self.assertEqual(DirectoryClinic.objects.get(id=self.a.id).recomputed_through, dt.date(2026, 11, 10))

    def test_a_catch_up_run_for_named_places_ignores_the_marks(self):
        jobs.run_sweep(now=cairo("2026-11-11T05:00"))
        with frozen(cairo("2026-11-10T19:00")):
            desk = self.a.client("assistant")
            booked = self.book(client=desk).json()["appointment"]
        self.assertEqual(jobs.run_sweep(now=cairo("2026-11-11T05:00")), {})
        self.assertEqual(jobs.run_sweep(now=cairo("2026-11-11T05:00"), clinic_ids={self.a.id}),
                         {"no_show": 1})
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(Appointment.objects.get(clinic_id=self.a.id, id=booked["id"]).status, "no_show")


class ProcrastinateTests(M1TestCase):
    def test_one_sweep_and_one_recompute_per_shard_every_ten_minutes(self):
        from django.conf import settings
        from procrastinate.contrib.django import app

        periodic = {key: (task.cron, task.configure_kwargs) for key, task in app.periodic_registry.periodic_tasks.items()}
        for alias in settings.SHARDS:
            cron, kwargs = periodic["scheduling.sweep_shard", alias]
            self.assertEqual(cron, "*/10 * * * *")
            self.assertEqual((kwargs["queueing_lock"], kwargs["task_kwargs"]["alias"]), (f"sweep:{alias}", alias))
            self.assertNotIn("lock", kwargs)  # a stopped worker must never leave a shard blocked
            cron, kwargs = periodic["scheduling.recompute_shard", alias]
            self.assertEqual((cron, kwargs["queueing_lock"]), ("5-59/10 * * * *", f"recompute:{alias}"))

    def test_the_tasks_run_their_shard_only(self):
        with frozen(cairo("2026-11-10T19:00")):
            booked = self.book(client=self.a.client("assistant")).json()["appointment"]
        with frozen(cairo("2026-11-11T05:00")):
            self.assertEqual(jobs.sweep_shard(timestamp=0, alias="shard_2"), {})
            self.assertEqual(self.appointment(booked["id"]).status, "booked")
            self.assertEqual(DirectoryClinic.objects.get(id=self.b.id).swept_through, dt.date(2026, 11, 10))
            self.assertIsNone(DirectoryClinic.objects.get(id=self.a.id).swept_through)
            self.assertEqual(jobs.sweep_shard(timestamp=0, alias="shard_1"), {"no_show": 1})
            self.assertEqual(jobs.recompute_shard(timestamp=0, alias="shard_1"), {"differences": 0})
        self.assertEqual(DirectoryClinic.objects.get(id=self.a.id).recomputed_through, dt.date(2026, 11, 10))
        self.assertIsNone(DirectoryClinic.objects.get(id=self.b.id).recomputed_through)


class DrawerDayTests(M1TestCase):
    """The day ends at a fixed time, whatever the drawer (Saged, 2026-11-24; replaces "the drawer decides" of
    2026-11-22): a register left open held one place on yesterday while another showed today."""

    def test_an_open_drawer_doesnt_hold_the_day_back(self):
        from core.testing import call
        from scheduling.testing import today

        with frozen(cairo("2026-11-19T10:00")):
            r = call(self.desk, "POST", "/api/v1/register/open", {"opening_float": 0})
        self.assertIn(r.status_code, (201, 409), r.content)  # the fixture's cash may have opened one
        self.assertEqual(today(self.a, cairo("2026-11-20T03:59")), dt.date(2026, 11, 19))
        self.assertEqual(today(self.a, cairo("2026-11-20T04:00")), dt.date(2026, 11, 20))
        with frozen(cairo("2026-11-20T05:30")):
            self.assertEqual(today(self.a), dt.date(2026, 11, 20))
