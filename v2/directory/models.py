"""The clinic directory (design 3.3, 4.1). Lives in the directory database."""

import datetime as dt

from django.conf import settings
from django.db import models
from django.db.models import Q


class DirectoryClinic(models.Model):
    """Which shard holds each place. Its id is the place's id everywhere.

    `kind`, `timezone` and `day_ends_at` are copies of the shard's clinic row (the authority): the
    picker shows a center badge, and the nightly jobs visit only places whose business day has
    just ended (draft 2 check, I6). `swept_through` and `recomputed_through` are the last business
    days those jobs finished. The sweep repairs the copy whenever it differs from the shard.
    """

    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"
        MOVING = "moving"

    shard = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120, blank=True, default="", db_default="")  # the picker in English mode (design G3)
    kind = models.CharField(max_length=6, default="clinic")
    timezone = models.CharField(max_length=60, default="Africa/Cairo")
    day_ends_at = models.TimeField(default=dt.time(4, 0))
    swept_through = models.DateField(null=True, blank=True)
    recomputed_through = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "directory_clinic"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["active", "suspended", "moving"]), name="dir_clinic_status_valid"),
            models.CheckConstraint(condition=Q(kind__in=["clinic", "center"]), name="dir_clinic_kind_valid"),
        ]


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


class Invitation(models.Model):
    """An existing account invited to join a place (design 5.8, G1). In the directory: the invitee has no shard to
    look in. Accepting creates the shard membership, then the picker row."""

    clinic = models.ForeignKey(DirectoryClinic, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invitations")
    role = models.CharField(max_length=10)
    display_name = models.CharField(max_length=120)
    display_name_en = models.CharField(max_length=120, blank=True, default="", db_default="")
    title = models.CharField(max_length=120, blank=True, default="")
    invited_by_membership_id = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    membership_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "directory_invitation"
        constraints = [
            models.CheckConstraint(condition=Q(role__in=["doctor", "assistant"]), name="dir_invitation_role_valid"),
            models.UniqueConstraint(fields=["clinic", "user"], condition=Q(accepted_at__isnull=True),
                                    name="dir_invitation_open_uniq"),
        ]
