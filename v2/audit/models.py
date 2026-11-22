"""The change log and the access log (design 4.10). Shard tables, under row-level security.

Neither has a foreign key: a log must never block or be blocked by the rows it describes.
"""

from django.db import models
from django.utils import timezone


class AuditChange(models.Model):
    """Sensitive changes, with before and after. Low volume; kept as long as financial records."""

    clinic_id = models.BigIntegerField()
    actor_kind = models.CharField(max_length=10)  # member, system, platform
    membership_id = models.BigIntegerField(null=True)
    user_id = models.BigIntegerField(null=True)
    # The file it belongs to (Saged, 2026-11-22): the owner reads a patient's history from here.
    patient_id = models.BigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=40, blank=True, default="")
    target_id = models.BigIntegerField(null=True)
    details = models.JSONField(default=dict)
    ip = models.GenericIPAddressField(null=True)
    request_id = models.CharField(max_length=36, blank=True, default="")
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_change"
        indexes = [
            models.Index("clinic_id", models.F("at").desc(), models.F("id").desc(), name="audit_change_clinic_at"),
            models.Index("clinic_id", "target_type", "target_id", models.F("at").desc(), models.F("id").desc(), name="audit_change_target"),
            models.Index("clinic_id", "patient_id", models.F("at").desc(), models.F("id").desc(), name="audit_change_patient",
                         condition=models.Q(patient_id__isnull=False)),
        ]


class AuditAccess(models.Model):
    """Every read of clinical data. Partitioned by month in SQL; kept 24 months online."""

    class Action(models.IntegerChoices):
        OPEN = 1
        PRINT = 2
        EXPORT = 3
        NOTE = 4
        PRESCRIPTION = 5
        INVOICE = 6
        HISTORY = 7
        ATTACHMENT = 8  # opening an attachment's file (design E3)

    id = models.BigAutoField(primary_key=True)
    clinic_id = models.BigIntegerField()
    at = models.DateTimeField(default=timezone.now)
    membership_id = models.BigIntegerField()
    patient_id = models.BigIntegerField()
    action = models.SmallIntegerField(choices=Action.choices)
    ip = models.GenericIPAddressField(null=True)
    request_id = models.CharField(max_length=36, blank=True, default="")

    class Meta:
        managed = False
        db_table = "audit_access"
