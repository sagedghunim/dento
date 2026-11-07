"""The clinic directory (design 3.3, 4.1). Lives in the directory database."""

from django.conf import settings
from django.db import models
from django.db.models import Q


class DirectoryClinic(models.Model):
    """Which shard holds each clinic. Its id is the clinic's id everywhere."""

    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"
        MOVING = "moving"

    shard = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "directory_clinic"
        constraints = [models.CheckConstraint(condition=Q(status__in=["active", "suspended", "moving"]), name="dir_clinic_status_valid")]


class UserClinic(models.Model):
    """A copy of each membership, for the clinic picker. The shard is the authority (design 3.3)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="clinic_links")
    clinic = models.ForeignKey(DirectoryClinic, on_delete=models.CASCADE, related_name="user_links")
    membership_id = models.BigIntegerField()
    role = models.CharField(max_length=10)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "directory_user_clinic"
        constraints = [models.UniqueConstraint(fields=["user", "clinic"], name="dir_user_clinic_uniq")]
