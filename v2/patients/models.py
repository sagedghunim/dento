"""Patient files (design 4.3). A shard table under row-level security."""

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.constraints import CompositeForeignKey


class Patient(models.Model):
    class Sex(models.TextChoices):
        MALE = "m"
        FEMALE = "f"
        UNKNOWN = ""

    # No composite foreign key on this table, so clinic_id gets its own foreign key (design 4.0).
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_index=False, related_name="+")
    file_no = models.BigIntegerField()  # from the patient_file counter
    full_name = models.CharField(max_length=120)
    # The search forms (core.text), compared byte by byte so range predicates keep their index ranges.
    name_norm = models.CharField(max_length=240, db_collation="C")
    phone = models.CharField(max_length=20, blank=True, default="")
    phone_norm = models.CharField(max_length=20, blank=True, default="", db_collation="C")
    sex = models.CharField(max_length=1, choices=Sex.choices, blank=True, default="")
    birth_date = models.DateField(null=True, blank=True)
    birth_date_estimated = models.BooleanField(default=False)  # typed as an age
    address = models.CharField(max_length=200, blank=True, default="")
    notes = models.CharField(max_length=2000, blank=True, default="")
    allergies = models.CharField(max_length=1000, blank=True, default="")
    chronic_conditions = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "patients_patient"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="patient_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "file_no"], name="patient_file_no_uniq"),
            models.CheckConstraint(condition=Q(sex__in=["m", "f", ""]), name="patient_sex_valid"),
            models.CheckConstraint(condition=Q(file_no__gt=0), name="patient_file_no_positive"),
        ]
        indexes = [
            # Search is an index-only scan of this clinic's part of these (design 5.7).
            models.Index(fields=["clinic", "name_norm"], include=["id", "full_name", "phone", "file_no"],
                         name="patient_name_search"),
            models.Index(fields=["clinic", "phone_norm"], include=["id", "full_name", "file_no"],
                         name="patient_phone_search"),
        ]

    def __str__(self):
        return f"#{self.file_no}"


class Tag(models.Model):
    """A place's patient tags (F6): VIP, insurance, a referral source. Editable; removing one deactivates it."""

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    name = models.CharField(max_length=40)
    name_en = models.CharField(max_length=40, blank=True, default="", db_default="")  # design G3
    color = models.CharField(max_length=7, blank=True, default="")  # "#RRGGBB" or empty
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "patients_tag"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="tag_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "name"], condition=Q(is_active=True), name="tag_active_name_uniq"),
        ]


class PatientTag(models.Model):
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "patients_patient_tag"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "patient", "tag"], name="patient_tag_uniq"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="patient_tag_patient_fk"),
            CompositeForeignKey(fields=["clinic", "tag"], to="patients.Tag", to_fields=["clinic", "id"],
                                name="patient_tag_tag_fk"),
        ]
        indexes = [models.Index(fields=["clinic", "tag", "patient"], name="patient_tag_by_tag")]
