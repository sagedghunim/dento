"""Appointments (design 4.4). A shard table under row-level security, fillfactor 85."""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

STATUSES = ["booked", "waiting", "in_room", "done", "cancelled", "no_show"]
SOURCES = ["clinic", "phone", "online", "whatsapp"]


class Appointment(models.Model):
    class Status(models.TextChoices):
        BOOKED = "booked"
        WAITING = "waiting"
        IN_ROOM = "in_room"
        DONE = "done"
        CANCELLED = "cancelled"
        NO_SHOW = "no_show"

    class Source(models.TextChoices):
        CLINIC = "clinic"
        PHONE = "phone"
        ONLINE = "online"
        WHATSAPP = "whatsapp"

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    doctor = models.ForeignKey("clinics.Membership", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    service = models.ForeignKey("clinics.Service", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    day = models.DateField()  # the business day (core.days.business_day)
    queue_no = models.IntegerField()
    starts_at = models.DateTimeField(null=True, blank=True)  # slot bookings only
    ends_at = models.DateTimeField(null=True, blank=True)
    is_extra = models.BooleanField(default=False)  # added to a full slot on purpose
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.BOOKED)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.CLINIC)
    fee = models.BigIntegerField(default=0)
    is_follow_up = models.BooleanField(default=False)
    is_new = models.BooleanField(default=False)
    auto_closed = models.BooleanField(default=False)  # closed by the end-of-day sweep
    note = models.CharField(max_length=500, blank=True, default="")
    arrived_at = models.DateTimeField(null=True, blank=True)
    called_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)  # never indexed
    updated_by_id = models.BigIntegerField(null=True, blank=True)
    # The slot index reads only this column, so status changes stay HOT updates (design 4.4).
    holds_slot = models.GeneratedField(
        expression=Q(starts_at__isnull=False, is_extra=False) & ~Q(status__in=["cancelled", "no_show"]),
        output_field=models.BooleanField(), db_persist=True)

    class Meta:
        db_table = "scheduling_appointment"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="appointment_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "day", "doctor", "queue_no"], name="appointment_turn_uniq"),
            models.UniqueConstraint(fields=["clinic", "doctor", "starts_at"], condition=Q(holds_slot=True),
                                    name="appointment_slot_uniq"),
            models.CheckConstraint(condition=Q(status__in=STATUSES), name="appointment_status_valid"),
            models.CheckConstraint(condition=Q(source__in=SOURCES), name="appointment_source_valid"),
            models.CheckConstraint(condition=Q(fee__gte=0), name="appointment_fee_not_negative"),
            models.CheckConstraint(condition=Q(queue_no__gt=0), name="appointment_queue_no_positive"),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True, ends_at__isnull=True)
                | Q(starts_at__isnull=False, ends_at__isnull=False, ends_at__gt=F("starts_at")),
                name="appointment_slot_times_valid"),
            # The timestamps each status needs.
            models.CheckConstraint(
                condition=~Q(status="booked")
                | Q(arrived_at__isnull=True, called_at__isnull=True, finished_at__isnull=True, cancelled_at__isnull=True),
                name="appointment_booked_times"),
            models.CheckConstraint(condition=~Q(status="waiting") | Q(arrived_at__isnull=False, called_at__isnull=True),
                                   name="appointment_waiting_times"),
            models.CheckConstraint(condition=~Q(status="in_room") | Q(arrived_at__isnull=False, called_at__isnull=False),
                                   name="appointment_in_room_times"),
            models.CheckConstraint(
                condition=~Q(status="done") | Q(arrived_at__isnull=False, called_at__isnull=False, finished_at__isnull=False),
                name="appointment_done_times"),
            models.CheckConstraint(condition=~Q(status="cancelled") | Q(cancelled_at__isnull=False),
                                   name="appointment_cancelled_times"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="appointment_patient_fk"),
            CompositeForeignKey(fields=["clinic", "doctor"], to="clinics.Membership", to_fields=["clinic", "id"],
                                name="appointment_doctor_fk"),
            CompositeForeignKey(fields=["clinic", "service"], to="clinics.Service", to_fields=["clinic", "id"],
                                name="appointment_service_fk"),
        ]
        indexes = [
            models.Index("clinic", "patient", F("day").desc(), F("id").desc(), name="appointment_patient_hist"),
        ]
