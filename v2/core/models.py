from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.utils import timezone


class RequestKey(models.Model):
    """A claimed Idempotency-Key on a shard (design 4.11, 5.0).

    No response body and no patient data: a replay rebuilds the answer from the target.
    Rows live at least 72 hours; a nightly job deletes older ones in batches.
    """

    pk = models.CompositePrimaryKey("clinic_id", "key")
    clinic_id = models.BigIntegerField()
    key = models.UUIDField()
    membership_id = models.BigIntegerField()
    endpoint = models.CharField(max_length=80)
    request_hash = models.CharField(max_length=64)
    # The HTTP status of the first answer, for the replay.
    status = models.PositiveSmallIntegerField(default=200)
    target_type = models.CharField(max_length=40, blank=True, default="")
    target_id = models.BigIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "core_request_key"
        indexes = [BrinIndex(fields=["created_at"], name="core_request_key_created_brin")]
