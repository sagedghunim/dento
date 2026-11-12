"""The doctor's visit (design 4.6, E3). Shard tables, all under forced row-level security.

Every row carries clinic_id; references between rows are composite foreign keys on (clinic_id, id), so a
row can never point at another clinic's row. Nothing clinical is deleted: library rows and plan items are
deactivated or dropped, notes keep their revisions.
"""

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

UNITS = ["tooth", "quadrant", "arch", "session", "unit", "case"]
# Tooth conditions (design E3). Caries and filling take surfaces.
CONDITIONS = ["healthy", "caries", "pulp_caries", "planned", "filling", "crown", "root_canal", "open_root_canal",
              "extracted", "missing", "implant", "bridge", "fractured", "impacted"]
SURFACES = ["M", "O", "D", "B", "L"]
# FDI numbers: 11-48 permanent, 51-85 primary.
TEETH = [q * 10 + n for q in (1, 2, 3, 4) for n in range(1, 9)] + [q * 10 + n for q in (5, 6, 7, 8) for n in range(1, 6)]
PLAN_ITEM_STATUSES = ["planned", "in_progress", "done", "dropped"]


def _clinic():
    return models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                             related_name="+")


def _ref(to, null=False):
    return models.ForeignKey(to, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+",
                             null=null, blank=null)


def _fk(field, to, name):
    return CompositeForeignKey(fields=["clinic", field], to=to, to_fields=["clinic", "id"], name=name)


def _uniq(name):
    return models.UniqueConstraint(fields=["clinic", "id"], name=name)


# --- Libraries, per section --------------------------------------------------------------------------------


class Medicine(models.Model):
    """The section's medicine library, copied from the specialty and edited with settings.medicines."""

    clinic = _clinic()
    section = _ref("clinics.Section")
    name = models.CharField(max_length=120)
    generic = models.CharField(max_length=200, blank=True, default="")  # the active ingredient, for allergy warnings
    strength = models.CharField(max_length=60, blank=True, default="")
    form = models.CharField(max_length=60, blank=True, default="")
    default_dose = models.CharField(max_length=200, blank=True, default="")
    default_note = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "clinical_medicine"
        constraints = [
            _uniq("medicine_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "section", "name", "strength"], condition=Q(is_active=True),
                                    name="medicine_active_name_uniq"),
            _fk("section", "clinics.Section", "medicine_section_fk"),
        ]


class PrescriptionTemplate(models.Model):
    """A named set of prescription lines, per section (design E3)."""

    clinic = _clinic()
    section = _ref("clinics.Section")
    name = models.CharField(max_length=120)
    notes = models.CharField(max_length=1000, blank=True, default="")
    is_active = models.BooleanField(default=True)
    version = models.IntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "clinical_prescription_template"
        constraints = [
            _uniq("rx_template_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "section", "name"], condition=Q(is_active=True),
                                    name="rx_template_active_name_uniq"),
            _fk("section", "clinics.Section", "rx_template_section_fk"),
        ]


class PrescriptionTemplateItem(models.Model):
    clinic = _clinic()
    template = _ref(PrescriptionTemplate)
    position = models.PositiveSmallIntegerField()
    medicine = _ref(Medicine, null=True)
    name = models.CharField(max_length=120)
    strength = models.CharField(max_length=60, blank=True, default="")
    form = models.CharField(max_length=60, blank=True, default="")
    dose = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "clinical_prescription_template_item"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "template", "position"], name="rx_template_item_position_uniq"),
            _fk("template", "clinical.PrescriptionTemplate", "rx_template_item_template_fk"),
            _fk("medicine", "clinical.Medicine", "rx_template_item_medicine_fk"),
        ]


class Procedure(models.Model):
    """The section's procedure catalogue (design E3): what the doctor records; each one adds an invoice line."""

    clinic = _clinic()
    section = _ref("clinics.Section")
    name_ar = models.CharField(max_length=160)
    name_en = models.CharField(max_length=160, blank=True, default="")
    category = models.CharField(max_length=40, blank=True, default="")
    unit = models.CharField(max_length=10, default="unit")
    price = models.BigIntegerField(default=0)  # piastres
    typical_min = models.BigIntegerField(null=True, blank=True)  # the market range, a hint only
    typical_max = models.BigIntegerField(null=True, blank=True)
    description = models.CharField(max_length=500, blank=True, default="")
    internal_cost = models.BigIntegerField(default=0)
    condition = models.CharField(max_length=20, blank=True, default="")  # the tooth state it sets, if any
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "clinical_procedure"
        constraints = [
            _uniq("procedure_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "section", "name_ar"], condition=Q(is_active=True),
                                    name="procedure_active_name_uniq"),
            models.CheckConstraint(condition=Q(unit__in=UNITS), name="procedure_unit_valid"),
            models.CheckConstraint(condition=Q(condition="") | Q(condition__in=CONDITIONS), name="procedure_condition_valid"),
            models.CheckConstraint(condition=Q(price__gte=0, internal_cost__gte=0), name="procedure_money_not_negative"),
            models.CheckConstraint(
                condition=Q(typical_min__isnull=True) | Q(typical_max__isnull=True)
                | Q(typical_min__gte=0, typical_max__gte=F("typical_min")),
                name="procedure_typical_range_valid"),
            _fk("section", "clinics.Section", "procedure_section_fk"),
        ]


# --- The visit record ---------------------------------------------------------------------------------------


class VisitNote(models.Model):
    """The chief complaint, diagnosis and notes of one visit, versioned (design 5.6, E3)."""

    clinic = _clinic()
    appointment = _ref("scheduling.Appointment")
    patient = _ref("patients.Patient")
    complaint = models.CharField(max_length=500, blank=True, default="")
    diagnosis = models.CharField(max_length=500, blank=True, default="")
    notes = models.CharField(max_length=5000, blank=True, default="")
    version = models.IntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    updated_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_visit_note"
        constraints = [
            _uniq("visit_note_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "appointment"], name="visit_note_one_per_visit"),
            _fk("appointment", "scheduling.Appointment", "visit_note_appointment_fk"),
            _fk("patient", "patients.Patient", "visit_note_patient_fk"),
        ]


class VisitNoteRevision(models.Model):
    """The text a note had before an edit after its visit's day (design 5.6)."""

    clinic = _clinic()
    note = _ref(VisitNote)
    version = models.IntegerField()
    complaint = models.CharField(max_length=500, blank=True, default="")
    diagnosis = models.CharField(max_length=500, blank=True, default="")
    notes = models.CharField(max_length=5000, blank=True, default="")
    replaced_at = models.DateTimeField(default=timezone.now)
    replaced_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_visit_note_revision"
        constraints = [_fk("note", "clinical.VisitNote", "visit_note_revision_note_fk")]
        indexes = [models.Index(fields=["clinic", "note", "version"], name="visit_note_revision_note")]


class Vitals(models.Model):
    """One visit's vital signs, all optional (design E3). Decimals for temperature and weight, not money."""

    clinic = _clinic()
    appointment = _ref("scheduling.Appointment")
    patient = _ref("patients.Patient")
    bp_systolic = models.PositiveSmallIntegerField(null=True, blank=True)  # mmHg
    bp_diastolic = models.PositiveSmallIntegerField(null=True, blank=True)
    pulse = models.PositiveSmallIntegerField(null=True, blank=True)  # beats a minute
    temperature = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)  # °C
    weight = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)  # kg
    height = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)  # cm
    spo2 = models.PositiveSmallIntegerField(null=True, blank=True)  # %
    blood_sugar = models.PositiveSmallIntegerField(null=True, blank=True)  # mg/dL
    version = models.IntegerField(default=1)
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_vitals"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "appointment"], name="vitals_one_per_visit"),
            models.CheckConstraint(condition=Q(bp_systolic__isnull=True) | Q(bp_systolic__range=(40, 300)), name="vitals_bp_systolic_range"),
            models.CheckConstraint(condition=Q(bp_diastolic__isnull=True) | Q(bp_diastolic__range=(20, 200)), name="vitals_bp_diastolic_range"),
            models.CheckConstraint(condition=Q(pulse__isnull=True) | Q(pulse__range=(20, 300)), name="vitals_pulse_range"),
            models.CheckConstraint(condition=Q(temperature__isnull=True) | Q(temperature__range=(25, 45)), name="vitals_temperature_range"),
            models.CheckConstraint(condition=Q(weight__isnull=True) | Q(weight__range=(0.3, 400)), name="vitals_weight_range"),
            models.CheckConstraint(condition=Q(height__isnull=True) | Q(height__range=(20, 260)), name="vitals_height_range"),
            models.CheckConstraint(condition=Q(spo2__isnull=True) | Q(spo2__range=(30, 100)), name="vitals_spo2_range"),
            models.CheckConstraint(condition=Q(blood_sugar__isnull=True) | Q(blood_sugar__range=(10, 1500)), name="vitals_sugar_range"),
            _fk("appointment", "scheduling.Appointment", "vitals_appointment_fk"),
            _fk("patient", "patients.Patient", "vitals_patient_fk"),
        ]


class Attachment(models.Model):
    """An image or PDF on the patient, optionally on a visit (design E3). The bytes live in clinical.files."""

    clinic = _clinic()
    patient = _ref("patients.Patient")
    appointment = _ref("scheduling.Appointment", null=True)
    title = models.CharField(max_length=200, blank=True, default="")
    file_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=60)
    size = models.IntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_attachment"
        constraints = [
            models.CheckConstraint(condition=Q(size__gt=0, size__lte=10 * 1024 * 1024), name="attachment_size_valid"),
            _fk("patient", "patients.Patient", "attachment_patient_fk"),
            _fk("appointment", "scheduling.Appointment", "attachment_appointment_fk"),
        ]
        indexes = [models.Index("clinic", "patient", F("created_at").desc(), F("id").desc(), name="attachment_patient")]


# --- Prescriptions -------------------------------------------------------------------------------------------


class Prescription(models.Model):
    """A printed prescription never changes (requirements 4.5); its lines are copies."""

    clinic = _clinic()
    number = models.BigIntegerField()  # from the clinic's prescription counter
    patient = _ref("patients.Patient")
    appointment = _ref("scheduling.Appointment", null=True)
    doctor = _ref("clinics.Membership")
    section = _ref("clinics.Section")
    notes = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_prescription"
        constraints = [
            _uniq("prescription_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "number"], name="prescription_number_uniq"),
            _fk("patient", "patients.Patient", "prescription_patient_fk"),
            _fk("appointment", "scheduling.Appointment", "prescription_appointment_fk"),
            _fk("doctor", "clinics.Membership", "prescription_doctor_fk"),
            _fk("section", "clinics.Section", "prescription_section_fk"),
        ]
        indexes = [models.Index("clinic", "patient", F("created_at").desc(), F("id").desc(), name="prescription_patient")]


class PrescriptionItem(models.Model):
    clinic = _clinic()
    prescription = _ref(Prescription)
    position = models.PositiveSmallIntegerField()
    medicine_id = models.BigIntegerField(null=True, blank=True)  # where it came from; the text below is the copy
    name = models.CharField(max_length=120)
    generic = models.CharField(max_length=200, blank=True, default="")
    strength = models.CharField(max_length=60, blank=True, default="")
    form = models.CharField(max_length=60, blank=True, default="")
    dose = models.CharField(max_length=200, blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        db_table = "clinical_prescription_item"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "prescription", "position"], name="prescription_item_position_uniq"),
            _fk("prescription", "clinical.Prescription", "prescription_item_prescription_fk"),
        ]


# --- Treatment plans, procedures and the dental chart --------------------------------------------------------


class Plan(models.Model):
    clinic = _clinic()
    patient = _ref("patients.Patient")
    section = _ref("clinics.Section")
    title = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_plan"
        constraints = [
            _uniq("plan_clinic_id_uniq"),
            _fk("patient", "patients.Patient", "plan_patient_fk"),
            _fk("section", "clinics.Section", "plan_section_fk"),
        ]
        indexes = [models.Index("clinic", "patient", F("created_at").desc(), F("id").desc(), name="plan_patient")]


class PlanItem(models.Model):
    clinic = _clinic()
    plan = _ref(Plan)
    procedure = _ref(Procedure)
    position = models.PositiveSmallIntegerField()
    tooth = models.PositiveSmallIntegerField(null=True, blank=True)
    area = models.CharField(max_length=20, blank=True, default="")
    price = models.BigIntegerField()
    status = models.CharField(max_length=12, default="planned")
    done_appointment = _ref("scheduling.Appointment", null=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "clinical_plan_item"
        constraints = [
            _uniq("plan_item_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "plan", "position"], name="plan_item_position_uniq"),
            models.CheckConstraint(condition=Q(status__in=PLAN_ITEM_STATUSES), name="plan_item_status_valid"),
            models.CheckConstraint(condition=Q(price__gte=0), name="plan_item_price_not_negative"),
            models.CheckConstraint(condition=Q(tooth__isnull=True) | Q(tooth__in=TEETH), name="plan_item_tooth_valid"),
            _fk("plan", "clinical.Plan", "plan_item_plan_fk"),
            _fk("procedure", "clinical.Procedure", "plan_item_procedure_fk"),
            _fk("done_appointment", "scheduling.Appointment", "plan_item_appointment_fk"),
        ]


class VisitProcedure(models.Model):
    """A procedure recorded in a visit: it adds an invoice line and may set a tooth's state (design E3)."""

    clinic = _clinic()
    appointment = _ref("scheduling.Appointment")
    patient = _ref("patients.Patient")
    procedure = _ref(Procedure)
    plan_item = _ref(PlanItem, null=True)
    tooth = models.PositiveSmallIntegerField(null=True, blank=True)
    surfaces = ArrayField(models.CharField(max_length=1), default=list, blank=True)
    area = models.CharField(max_length=20, blank=True, default="")
    qty = models.IntegerField(default=1)
    unit_price = models.BigIntegerField()
    amount = models.BigIntegerField()
    invoice_line_id = models.BigIntegerField(null=True, blank=True)  # the line can be removed; this keeps the record
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_visit_procedure"
        constraints = [
            _uniq("visit_procedure_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(amount=F("qty") * F("unit_price"), qty__gt=0, unit_price__gte=0),
                                   name="visit_procedure_amount_valid"),
            models.CheckConstraint(condition=Q(tooth__isnull=True) | Q(tooth__in=TEETH), name="visit_procedure_tooth_valid"),
            _fk("appointment", "scheduling.Appointment", "visit_procedure_appointment_fk"),
            _fk("patient", "patients.Patient", "visit_procedure_patient_fk"),
            _fk("procedure", "clinical.Procedure", "visit_procedure_procedure_fk"),
            _fk("plan_item", "clinical.PlanItem", "visit_procedure_plan_item_fk"),
        ]
        indexes = [models.Index("clinic", "appointment", "id", name="visit_procedure_visit"),
                   models.Index("clinic", "patient", F("created_at").desc(), F("id").desc(), name="visit_procedure_patient")]


class Tooth(models.Model):
    """A patient's tooth state now (FDI numbering)."""

    clinic = _clinic()
    patient = _ref("patients.Patient")
    tooth = models.PositiveSmallIntegerField()
    condition = models.CharField(max_length=20)
    surfaces = ArrayField(models.CharField(max_length=1), default=list, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(default=timezone.now)
    updated_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_tooth"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "patient", "tooth"], name="tooth_one_per_patient"),
            models.CheckConstraint(condition=Q(tooth__in=TEETH), name="tooth_number_valid"),
            models.CheckConstraint(condition=Q(condition__in=CONDITIONS), name="tooth_condition_valid"),
            _fk("patient", "patients.Patient", "tooth_patient_fk"),
        ]


class ToothHistory(models.Model):
    clinic = _clinic()
    patient = _ref("patients.Patient")
    tooth = models.PositiveSmallIntegerField()
    condition = models.CharField(max_length=20)
    surfaces = ArrayField(models.CharField(max_length=1), default=list, blank=True)
    appointment = _ref("scheduling.Appointment", null=True)
    procedure = _ref(Procedure, null=True)
    visit_procedure = _ref(VisitProcedure, null=True)
    note = models.CharField(max_length=500, blank=True, default="")
    at = models.DateTimeField(default=timezone.now)
    by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "clinical_tooth_history"
        constraints = [
            models.CheckConstraint(condition=Q(tooth__in=TEETH), name="tooth_history_number_valid"),
            models.CheckConstraint(condition=Q(condition__in=CONDITIONS), name="tooth_history_condition_valid"),
            _fk("patient", "patients.Patient", "tooth_history_patient_fk"),
            _fk("appointment", "scheduling.Appointment", "tooth_history_appointment_fk"),
            _fk("procedure", "clinical.Procedure", "tooth_history_procedure_fk"),
            _fk("visit_procedure", "clinical.VisitProcedure", "tooth_history_visit_procedure_fk"),
        ]
        indexes = [models.Index("clinic", "patient", "tooth", F("at").desc(), F("id").desc(), name="tooth_history_tooth")]
