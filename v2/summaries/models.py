"""Per-day summaries (design 4.4): turns, limits, money and Today's version. Depends only on `clinics`.

Status counts on Today come from the day's appointment rows, so these hold only what
finance, `max_bookings` and the version need. Services change them in one statement
per table and flow, in key order (design 5.0); the recompute job checks them (5.10).
"""

from django.db import models
from django.db.models import F, Q

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
    # Reports and staff pay (F4, F8), by the visit's day: procedures recorded, no-shows, and the times of
    # the visits finished with all three stamps (arrived, called in, finished).
    procedures = models.IntegerField(db_default=0)
    no_shows = models.IntegerField(db_default=0)
    timed = models.IntegerField(db_default=0)
    wait_secs = models.BigIntegerField(db_default=0)  # arrived -> called in, summed over `timed` visits
    room_secs = models.BigIntegerField(db_default=0)  # called in -> finished

    class Meta:
        db_table = "summaries_doctor_day"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "day", "doctor"], name="doctor_day_uniq"),
            models.CheckConstraint(
                condition=Q(last_queue_no__gte=0, booked__gte=0, done__gte=0, collected__gte=0, refunded__gte=0),
                name="doctor_day_counters_not_negative"),
            models.CheckConstraint(
                condition=Q(procedures__gte=0, no_shows__gte=0, timed__gte=0, wait_secs__gte=0, room_secs__gte=0),
                name="doctor_day_report_counters_not_negative"),
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


class MoneyDay(models.Model):
    """Money by payment day, section and method (F5): revenue by section and by method without reading payments.

    section_id 0 is "no section" (an invoice without a visit), so the unique key has no NULL.
    """

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    day = models.DateField()
    section_id = models.BigIntegerField(default=0)
    method = models.CharField(max_length=8)
    collected = models.BigIntegerField(default=0)
    refunded = models.BigIntegerField(default=0)

    class Meta:
        db_table = "summaries_money_day"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "day", "section_id", "method"], name="money_day_uniq"),
            models.CheckConstraint(condition=Q(collected__gte=0, refunded__gte=0), name="money_day_not_negative"),
        ]


class PatientStats(models.Model):
    """A patient's visits in one row (F6): first and last visit day, how many, and when they came back.

    Written by finish and the sweep (one upsert), so the insights count patients, never visits.
    """

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    first_day = models.DateField()
    last_day = models.DateField()
    visits = models.IntegerField(default=0)
    reactivated_day = models.DateField(null=True, blank=True)  # a visit after a long gap (F6 "reactivated")

    class Meta:
        db_table = "summaries_patient"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "patient"], name="patient_stats_uniq"),
            models.CheckConstraint(condition=Q(visits__gte=1, last_day__gte=F("first_day")), name="patient_stats_valid"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="patient_stats_patient_fk"),
        ]
        indexes = [
            models.Index(fields=["clinic", "last_day", "patient"], name="patient_stats_last"),
            models.Index(fields=["clinic", "first_day"], name="patient_stats_first"),
            models.Index(fields=["clinic", "reactivated_day"], condition=Q(reactivated_day__isnull=False),
                         name="patient_stats_reactivated"),
        ]
