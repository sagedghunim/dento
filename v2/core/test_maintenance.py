"""Daily upkeep (chief judge M1, R5): log partitions and request-key cleanup are scheduled and work."""

import datetime as dt
import json
import uuid

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.test import Client

from accounts.models import AccountRequestKey
from core import jobs
from core.models import RequestKey
from core.partitions import (ALERT_BELOW, MONTHS_AHEAD, ensure_all, ensure_partitions, existing_partitions,
                             partition_name)
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import M1TestCase, frozen


def month_ahead(months):
    today = dt.datetime.now(dt.UTC).date()
    index = today.year * 12 + today.month - 1 + months
    return dt.date(index // 12, index % 12 + 1, 15)


class PartitionTests(M1TestCase):
    def test_both_tasks_run_daily(self):
        from procrastinate.contrib.django import app

        periodic = {key[0]: task.cron for key, task in app.periodic_registry.periodic_tasks.items()}
        self.assertEqual(periodic["core.ensure_partitions"], jobs.PARTITIONS_CRON)
        self.assertEqual(periodic["core.clean_request_keys"], jobs.KEY_CLEANUP_CRON)
        self.assertEqual((MONTHS_AHEAD, ALERT_BELOW), (6, 3))

    def test_every_database_keeps_six_months_ahead(self):
        found = ensure_all()
        self.assertEqual(found["default"], {"accounts_account_event": 6})
        for alias in settings.SHARDS:
            self.assertEqual(found[alias], {"audit_access": 6})

    def test_a_run_with_the_clock_moved_forward_creates_the_missing_months_and_file_opens_keep_working(self):
        later = month_ahead(9)
        patient = self.book().json()["appointment"]["patient"]["id"]
        name = partition_name("audit_access", dt.date(later.year, later.month, 1))
        self.assertNotIn(name, existing_partitions(connections["shard_1"], "audit_access"))
        instant = dt.datetime.combine(later, dt.time(12), tzinfo=dt.UTC)
        with frozen(instant):
            owner = self.a.client("owner")  # a session made at that time
            with self.assertRaises(IntegrityError), transaction.atomic(using="shard_1"):
                call(owner, "GET", f"/api/v1/patients/{patient}")  # no partition yet: the access log can't be written
        with self.assertLogs("core.partitions", "ERROR"):  # fewer than 3 months ahead: the alert fires
            ensure_partitions("shard_2", months_ahead=1, today=month_ahead(MONTHS_AHEAD + 12))
        found = ensure_all(today=later)
        self.assertEqual(found["shard_1"], {"audit_access": 6})
        self.assertIn(name, existing_partitions(connections["shard_1"], "audit_access"))
        with frozen(instant):
            self.assertEqual(call(self.a.client("owner"), "GET", f"/api/v1/patients/{patient}").status_code, 200)
            r = Client().post("/api/v1/auth/login", json.dumps({"username": "a_doctor", "password": "pass-word-123"}),
                              content_type="application/json")
            self.assertEqual(r.status_code, 200)  # the sign-in event has its month too


class RequestKeyCleanupTests(M1TestCase):
    def test_keys_older_than_72_hours_go_and_newer_ones_stay(self):
        now = dt.datetime.now(dt.UTC)
        old, fresh = now - dt.timedelta(hours=73), now - dt.timedelta(hours=71)
        for fixture in (self.a, self.b):
            with clinic_transaction(fixture.id, fixture.shard):
                for created in (old, old, fresh):
                    RequestKey.objects.create(clinic_id=fixture.id, key=uuid.uuid4(), membership_id=1,
                                              endpoint="x.y", request_hash="h", created_at=created)
        AccountRequestKey.objects.create(user_id=1, key=uuid.uuid4(), endpoint="x", request_hash="h", created_at=old)
        AccountRequestKey.objects.create(user_id=1, key=uuid.uuid4(), endpoint="x", request_hash="h", created_at=fresh)
        self.assertEqual(jobs.clean_request_keys(now), 5)
        for fixture in (self.a, self.b):
            with clinic_transaction(fixture.id, fixture.shard):
                left = list(RequestKey.objects.filter(clinic_id=fixture.id).values_list("created_at", flat=True))
            self.assertTrue(left and all(created > now - jobs.KEEP_KEYS for created in left))
        self.assertEqual(AccountRequestKey.objects.filter(created_at__lt=now - jobs.KEEP_KEYS).count(), 0)
        self.assertEqual(jobs.clean_request_keys_task(timestamp=0), {"deleted": 0})
