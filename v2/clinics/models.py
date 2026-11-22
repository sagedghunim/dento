"""Clinic configuration and people (design 4.2). Shard tables, all under row-level security."""

import datetime as dt

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q

from core.constraints import CompositeForeignKey


class Clinic(models.Model):
    """One row per place on the shard that holds it. The id comes from the directory.

    A place is a clinic (one section) or a medical center (several sections, design D1). Patients,
    the front desk, money and the team are shared across the place; each section has its own
    specialty, services, prices and doctors' sessions.
    """

    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"

    class Kind(models.TextChoices):
        CLINIC = "clinic"
        CENTER = "center"

    id = models.BigIntegerField(primary_key=True)
    # False on a shard the clinic has moved away from: requests are refused and routed again (design 3.3).
    owned = models.BooleanField(default=True)
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.CLINIC)
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120, blank=True, default="", db_default="")  # shown in English mode (design G3)
    city = models.CharField(max_length=60, blank=True, default="")
    address = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    timezone = models.CharField(max_length=60, default="Africa/Cairo")
    # A time before this belongs to the previous business day (design 4.0).
    day_ends_at = models.TimeField(default=dt.time(4, 0))
    specialty_code = models.CharField(max_length=30)  # the main specialty: the first section's
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    settings = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "clinics_clinic"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["active", "suspended"]), name="clinic_status_valid"),
            models.CheckConstraint(condition=Q(kind__in=["clinic", "center"]), name="clinic_kind_valid"),
        ]

    def __str__(self):
        return self.name


class Section(models.Model):
    """A section of a place, with its own specialty (design D1). Every place has at least one.

    A clinic has exactly one, named after its specialty; screens hide sections when there is only
    one. A doctor's sections are the sections of their sessions: there is no doctor-section table.
    Sections are deactivated, never deleted.
    """

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, db_index=False, related_name="+")
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120, blank=True, default="", db_default="")
    specialty_code = models.CharField(max_length=30)
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "clinics_section"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="section_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "name"], condition=Q(is_active=True), name="section_active_name_uniq"),
        ]


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner"
        DOCTOR = "doctor"
        ASSISTANT = "assistant"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, db_index=False, related_name="+")
    # The account lives in the directory database: a plain integer, no foreign key (design 3.3).
    user_id = models.BigIntegerField()
    role = models.CharField(max_length=10, choices=Role.choices)
    # The owner opened their alerts up to here (web/alerts.py, Saged 2026-11-19); newer ones are counted.
    alerts_seen_at = models.DateTimeField(null=True, blank=True)
    display_name = models.CharField(max_length=120)
    display_name_en = models.CharField(max_length=120, blank=True, default="", db_default="")
    title = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    # An assistant limited to some sections of a center (design G1); empty means every section.
    sections = ArrayField(models.BigIntegerField(), default=list, blank=True)
    # Every team write is conditional on it (design G1).
    version = models.PositiveIntegerField(default=1)
    granted = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    revoked = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    # Working hours the owner writes for their record (Saged, 2026-11-22): shown on Team, never blocks signing in.
    # Weekdays as Python's (0 = Monday).
    work_days = ArrayField(models.PositiveSmallIntegerField(), null=True, blank=True)  # null: none set
    work_starts = models.TimeField(null=True, blank=True)
    work_ends = models.TimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "clinics_membership"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="membership_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "user_id"], name="membership_one_per_user"),
            models.UniqueConstraint(fields=["clinic"], condition=Q(role="owner"), name="membership_one_owner"),
            models.CheckConstraint(condition=Q(role__in=["owner", "doctor", "assistant"]), name="membership_role_valid"),
            models.CheckConstraint(condition=~Q(role="owner") | Q(is_active=True), name="membership_owner_active"),
            models.CheckConstraint(condition=Q(sections__len__lte=20), name="membership_sections_max"),
            models.CheckConstraint(condition=Q(work_days__len__lte=7), name="membership_work_days_max"),
            models.CheckConstraint(condition=(Q(work_starts__isnull=True) & Q(work_ends__isnull=True))
                                   | (Q(work_starts__isnull=False) & Q(work_ends__isnull=False)),
                                   name="membership_work_hours_pair"),
            models.CheckConstraint(condition=Q(role="assistant") | Q(sections=[]), name="membership_sections_assistant"),
        ]

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER


class Service(models.Model):
    class Kind(models.TextChoices):
        CONSULTATION = "consultation"
        FOLLOW_UP = "follow_up"
        PROCEDURE = "procedure"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, db_index=False, related_name="+")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120, blank=True, default="", db_default="")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    price = models.BigIntegerField(default=0)
    duration_min = models.PositiveSmallIntegerField(default=15)
    # For a consultation: the service whose price a follow-up uses (design 5.1).
    follow_up_service = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                          db_constraint=False, db_index=False, related_name="+")
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)
    # The doctor's price-list item this booking service stands for (Saged, 2026-11-22): the desk books from the same
    # list the doctor records from. Kept in step by clinics.booking_services.sync; a plain integer, as clinical comes
    # after clinics.
    procedure_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinics_service"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="service_clinic_id_uniq"),
            # Each section of a center has its own "Consultation".
            models.UniqueConstraint(fields=["clinic", "section", "name"], name="service_section_name_uniq"),
            models.UniqueConstraint(fields=["clinic", "procedure_id"], condition=Q(procedure_id__isnull=False),
                                    name="service_one_per_procedure"),
            models.CheckConstraint(condition=Q(kind__in=["consultation", "follow_up", "procedure"]), name="service_kind_valid"),
            models.CheckConstraint(condition=Q(price__gte=0), name="service_price_not_negative"),
            CompositeForeignKey(fields=["clinic", "follow_up_service"], to="clinics.Service",
                                to_fields=["clinic", "id"], name="service_follow_up_fk"),
            CompositeForeignKey(fields=["clinic", "section"], to="clinics.Section", to_fields=["clinic", "id"],
                                name="service_section_fk"),
        ]


class Schedule(models.Model):
    class Mode(models.TextChoices):
        QUEUE = "queue"
        SLOTS = "slots"

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, db_index=False, db_constraint=False, related_name="+")
    doctor = models.ForeignKey(Membership, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    # A session belongs to one section; a doctor's sections are the sections of their sessions (design D1).
    section = models.ForeignKey(Section, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    weekday = models.PositiveSmallIntegerField()  # Monday is 0, as in Python
    starts = models.TimeField()
    ends = models.TimeField()
    mode = models.CharField(max_length=5, choices=Mode.choices, default=Mode.QUEUE)
    slot_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    max_bookings = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinics_schedule"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "doctor", "weekday", "starts"], name="schedule_session_uniq"),
            models.CheckConstraint(condition=Q(weekday__lte=6), name="schedule_weekday_valid"),
            models.CheckConstraint(condition=Q(mode__in=["queue", "slots"]), name="schedule_mode_valid"),
            # A session may run past midnight (ends < starts) because of the business day (design 4.0).
            models.CheckConstraint(condition=~Q(ends=F("starts")), name="schedule_not_empty"),
            models.CheckConstraint(
                condition=Q(mode="queue", slot_minutes__isnull=True) | Q(mode="slots", slot_minutes__gte=5, slot_minutes__lte=120),
                name="schedule_slot_minutes_valid",
            ),
            CompositeForeignKey(fields=["clinic", "doctor"], to="clinics.Membership", to_fields=["clinic", "id"],
                                name="schedule_doctor_fk"),
            CompositeForeignKey(fields=["clinic", "section"], to="clinics.Section", to_fields=["clinic", "id"],
                                name="schedule_section_fk"),
        ]


class Counter(models.Model):
    """Gap-free numbers per clinic (design 4.2): taken inside the transaction that uses them."""

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, db_index=False, related_name="+")
    name = models.CharField(max_length=20)
    value = models.BigIntegerField(default=0)

    class Meta:
        db_table = "clinics_counter"
        constraints = [models.UniqueConstraint(fields=["clinic", "name"], name="counter_name_uniq")]
