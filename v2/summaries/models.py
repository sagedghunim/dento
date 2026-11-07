"""Per-day summaries (design 4.4): turns, limits, money and Today's version. Depends only on `clinics`.

Status counts on Today come from the day's appointment rows, so these hold only what
finance, `max_bookings` and the version need. Services change them in one statement
per table and flow, in key order (design 5.0); the recompute job checks them (5.10).
"""

from django.db import models
from django.db.models import Q

from core.constraints import CompositeForeignKey


class DoctorDay(models.Model):
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    doctor = models.ForeignKey("clinics.Membership", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    day = models.DateField()
    last_queue_no = models.IntegerField(default=0)  # never lowered; turn numbers may have gaps
    booked = models.IntegerField(default=0)  # active bookings, for max_bookings
    done = models.IntegerField(default=0)
    collected = models.BigIntegerField(default=0)  # piastres, by payment day
    refunded = models.BigIntegerField(default=0)

    class Meta:
        db_table = "summaries_doctor_day"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "day", "doctor"], name="doctor_day_uniq"),
            models.CheckConstraint(
                condition=Q(last_queue_no__gte=0, booked__gte=0, done__gte=0, collected__gte=0, refunded__gte=0),
                name="doctor_day_counters_not_negative"),
            CompositeForeignKey(fields=["clinic", "doctor"], to="clinics.Membership", to_fields=["clinic", "id"],
                                name="doctor_day_doctor_fk"),
        ]


class ClinicDay(models.Model):
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_index=False, related_name="+")
    day = models.DateField()
    version = models.BigIntegerField(default=0)  # Today's ETag (design 5.11)
    done = models.IntegerField(default=0)
    collected = models.BigIntegerField(default=0)
    refunded = models.BigIntegerField(default=0)
    expenses = models.BigIntegerField(default=0)
    # Set by every change to the day's sources, cleared by the recompute (design 5.10).
    changed_since_recompute = models.BooleanField(default=False)

    class Meta:
        db_table = "summaries_clinic_day"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "day"], name="clinic_day_uniq"),
            models.CheckConstraint(
                condition=Q(version__gte=0, done__gte=0, collected__gte=0, refunded__gte=0, expenses__gte=0),
                name="clinic_day_counters_not_negative"),
        ]
