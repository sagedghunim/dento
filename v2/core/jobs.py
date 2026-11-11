"""Daily upkeep as Procrastinate periodic tasks (design 4.10, 4.11, 5.10; chief judge M1, R5).

- Log partitions: the current month and 6 more on the directory and every shard. Without them, sign-in
  and every file open fail on the first day of a month nobody prepared.
- Request keys: rows older than 72 hours go, in batches of about 5,000, in the 04:00-09:00 Cairo window.
  On a shard the table is under forced row-level security, so the cleanup walks the shard's places from
  the directory, one transaction per place. A `hamada_maint` alias (design 4.0) can later do it in one pass.
"""

import datetime as dt
import logging

from django.db import connections, transaction
from django.utils import timezone
from procrastinate.contrib.django import app

from core.partitions import ensure_all
from core.routing import clinic_transaction

log = logging.getLogger(__name__)

KEEP_KEYS = dt.timedelta(hours=72)
KEY_BATCH = 5000
PARTITIONS_CRON = "30 1 * * *"  # 03:30 Cairo in winter (the cron runs in UTC)
KEY_CLEANUP_CRON = "0 3 * * *"  # 05:00 Cairo in winter


def clean_request_keys(now=None):
    """Delete request keys older than 72 hours on the directory and every shard. Returns the rows deleted."""
    from directory.models import DirectoryClinic

    cutoff = (now or timezone.now()) - KEEP_KEYS
    deleted = 0
    while True:
        with transaction.atomic(using="default"), connections["default"].cursor() as cursor:
            cursor.execute("DELETE FROM accounts_request_key WHERE ctid = ANY(ARRAY(SELECT ctid FROM accounts_request_key "
                           "WHERE created_at < %s LIMIT %s))", [cutoff, KEY_BATCH])
            deleted += cursor.rowcount
            if cursor.rowcount < KEY_BATCH:
                break
    for clinic_id, alias in DirectoryClinic.objects.order_by("id").values_list("id", "shard").iterator(chunk_size=500):
        while True:
            try:
                with clinic_transaction(clinic_id, alias), connections[alias].cursor() as cursor:
                    cursor.execute('DELETE FROM "core_request_key" WHERE clinic_id = %s AND key IN ('
                                   'SELECT key FROM "core_request_key" WHERE clinic_id = %s AND created_at < %s LIMIT %s)',
                                   [clinic_id, clinic_id, cutoff, KEY_BATCH])
                    count = cursor.rowcount
            except Exception:
                log.exception("request-key cleanup failed: clinic=%s", clinic_id)
                break
            deleted += count
            if count < KEY_BATCH:
                break
    return deleted


@app.periodic(cron=PARTITIONS_CRON, queueing_lock="core.ensure_partitions")
@app.task(name="core.ensure_partitions", pass_context=False)
def ensure_partitions_task(timestamp):
    found = ensure_all()
    log.info("partitions ahead %s", found)
    return found


@app.periodic(cron=KEY_CLEANUP_CRON, queueing_lock="core.clean_request_keys")
@app.task(name="core.clean_request_keys", pass_context=False)
def clean_request_keys_task(timestamp):
    return {"deleted": clean_request_keys()}
