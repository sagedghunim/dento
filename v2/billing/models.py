"""Invoices, lines and payments (design 4.5). Shard tables under row-level security.

Money is bigint piastres. Nothing is deleted except an invoice line removed from an
open invoice (audited): voids and refunds keep their rows.
"""

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now
from django.utils import timezone

from core.constraints import CompositeForeignKey

METHODS = ["cash", "card", "transfer", "wallet", "instapay", "other"]  # transfer: design E3
LINE_KINDS = ["service", "procedure", "material", "package", "other"]  # package: F7


class Invoice(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        VOID = "void"

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    number = models.BigIntegerField()  # from the invoice counter: sequential, no gaps
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    appointment = models.ForeignKey("scheduling.Appointment", null=True, blank=True, on_delete=models.PROTECT,
                                    db_constraint=False, db_index=False, related_name="+")
    status = models.CharField(max_length=4, choices=Status.choices, default=Status.OPEN)
    subtotal = models.BigIntegerField(default=0)
    discount = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)
    paid = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "billing_invoice"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="invoice_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "number"], name="invoice_number_uniq"),
            # One open invoice per visit; every ON CONFLICT on it repeats the predicate (design 4.5).
            models.UniqueConstraint(fields=["clinic", "appointment"], condition=Q(status="open"),
                                    name="invoice_one_open_per_visit"),
            models.CheckConstraint(condition=Q(status__in=["open", "void"]), name="invoice_status_valid"),
            models.CheckConstraint(condition=Q(discount__gte=0, discount__lte=F("subtotal")), name="invoice_discount_valid"),
            models.CheckConstraint(condition=Q(total=F("subtotal") - F("discount")), name="invoice_total_valid"),
            models.CheckConstraint(condition=Q(paid__gte=0, paid__lte=F("total")), name="invoice_paid_valid"),
            models.CheckConstraint(condition=~Q(status="void") | Q(paid=0), name="invoice_void_unpaid"),
            models.CheckConstraint(condition=Q(number__gt=0), name="invoice_number_positive"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="invoice_patient_fk"),
            CompositeForeignKey(fields=["clinic", "appointment"], to="scheduling.Appointment",
                                to_fields=["clinic", "id"], name="invoice_appointment_fk"),
        ]
        indexes = [
            models.Index("clinic", "patient", F("created_at").desc(), F("id").desc(), name="invoice_patient_hist"),
            # Receivables (F5): only invoices with something owed. Its predicate reads paid, so a payment's update
            # of the invoice is no longer HOT; payments are far rarer than the status changes that must stay HOT.
            models.Index(fields=["clinic", "patient", "created_at"], condition=Q(status="open", paid__lt=F("total")),
                         name="invoice_unpaid"),
        ]


class InvoiceLine(models.Model):
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    kind = models.CharField(max_length=10)
    service = models.ForeignKey("clinics.Service", null=True, blank=True, on_delete=models.PROTECT,
                                db_constraint=False, db_index=False, related_name="+")
    item_id = models.BigIntegerField(null=True, blank=True)  # a material line's stock item (F2)
    internal_cost = models.BigIntegerField(db_default=0)  # what the line's materials cost the place (F2), piastres
    description = models.CharField(max_length=200)
    qty = models.IntegerField(default=1)
    unit_price = models.BigIntegerField()
    amount = models.BigIntegerField()
    from_list = models.BooleanField(default=False)  # the service's list price
    # Who added it and when: the desk may remove its own line on the same business day (design 5.3).
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "billing_invoice_line"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=LINE_KINDS), name="invoice_line_kind_valid"),
            models.CheckConstraint(condition=Q(amount=F("qty") * F("unit_price"), qty__gt=0, unit_price__gte=0),
                                   name="invoice_line_amount_valid"),
            CompositeForeignKey(fields=["clinic", "invoice"], to="billing.Invoice", to_fields=["clinic", "id"],
                                name="invoice_line_invoice_fk"),
            CompositeForeignKey(fields=["clinic", "service"], to="clinics.Service", to_fields=["clinic", "id"],
                                name="invoice_line_service_fk"),
            CompositeForeignKey(fields=["clinic", "item_id"], to="inventory.Item", to_fields=["clinic", "id"],
                                name="invoice_line_item_fk"),
            models.CheckConstraint(condition=Q(internal_cost__gte=0), name="invoice_line_cost_not_negative"),
        ]
        indexes = [models.Index(fields=["clinic", "invoice", "id"], name="invoice_line_invoice")]


class Payment(models.Model):
    class Kind(models.TextChoices):
        PAYMENT = "payment"
        REFUND = "refund"

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    # The visit's doctor, copied at insert: revenue per doctor (design 5.3).
    doctor = models.ForeignKey("clinics.Membership", null=True, blank=True, on_delete=models.PROTECT,
                               db_constraint=False, db_index=False, related_name="+")
    kind = models.CharField(max_length=7, choices=Kind.choices, default=Kind.PAYMENT)
    method = models.CharField(max_length=8)
    amount = models.BigIntegerField()
    refunded = models.BigIntegerField(default=0)
    refund_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, db_constraint=False,
                                  db_index=False, related_name="+")
    received_at = models.DateTimeField(default=timezone.now)
    day = models.DateField()  # the business day the money moved
    received_by_id = models.BigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")
    # F1: the register shift open when it was taken (cash needs one; other methods join it when one is open).
    shift = models.ForeignKey("finance.Shift", null=True, blank=True, on_delete=models.PROTECT, db_constraint=False,
                              db_index=False, related_name="+")
    # F5: the visit's section, copied at insert like the doctor, for revenue by section.
    section = models.ForeignKey("clinics.Section", null=True, blank=True, on_delete=models.PROTECT,
                                db_constraint=False, db_index=False, related_name="+")

    class Meta:
        db_table = "billing_payment"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="payment_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["payment", "refund"]), name="payment_kind_valid"),
            models.CheckConstraint(condition=Q(method__in=METHODS), name="payment_method_valid"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(condition=Q(refunded__gte=0, refunded__lte=F("amount")), name="payment_refunded_valid"),
            models.CheckConstraint(condition=Q(kind="payment") | Q(refunded=0), name="payment_only_payments_refunded"),
            models.CheckConstraint(
                condition=Q(kind="payment", refund_of__isnull=True) | Q(kind="refund", refund_of__isnull=False),
                name="payment_refund_has_original"),
            CompositeForeignKey(fields=["clinic", "invoice"], to="billing.Invoice", to_fields=["clinic", "id"],
                                name="payment_invoice_fk"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="payment_patient_fk"),
            CompositeForeignKey(fields=["clinic", "doctor"], to="clinics.Membership", to_fields=["clinic", "id"],
                                name="payment_doctor_fk"),
            CompositeForeignKey(fields=["clinic", "refund_of"], to="billing.Payment", to_fields=["clinic", "id"],
                                name="payment_refund_of_fk"),
            CompositeForeignKey(fields=["clinic", "shift"], to="finance.Shift", to_fields=["clinic", "id"],
                                name="payment_shift_fk"),
            CompositeForeignKey(fields=["clinic", "section"], to="clinics.Section", to_fields=["clinic", "id"],
                                name="payment_section_fk"),
        ]
        indexes = [
            models.Index(fields=["clinic", "invoice", "id"], name="payment_invoice"),
            models.Index("clinic", "patient", F("received_at").desc(), F("id").desc(), name="payment_patient_hist"),
            models.Index(fields=["clinic", "day", "received_by_id"], name="payment_day_closing"),
            models.Index(fields=["clinic", "shift", "id"], condition=Q(shift__isnull=False), name="payment_shift"),
        ]


class Package(models.Model):
    """A session package (F7): a procedure, a number of sessions and a price, per section."""

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    section = models.ForeignKey("clinics.Section", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    procedure = models.ForeignKey("clinical.Procedure", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                  related_name="+")
    name = models.CharField(max_length=120)
    sessions = models.IntegerField()
    price = models.BigIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "billing_package"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="package_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "section", "name"], condition=Q(is_active=True),
                                    name="package_active_name_uniq"),
            models.CheckConstraint(condition=Q(sessions__gte=1, sessions__lte=500, price__gte=0), name="package_valid"),
            CompositeForeignKey(fields=["clinic", "section"], to="clinics.Section", to_fields=["clinic", "id"],
                                name="package_section_fk"),
            CompositeForeignKey(fields=["clinic", "procedure"], to="clinical.Procedure", to_fields=["clinic", "id"],
                                name="package_procedure_fk"),
        ]


class PatientPackage(models.Model):
    """A package sold to a patient: its invoice line, and the sessions left (F7)."""

    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    package = models.ForeignKey(Package, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    procedure = models.ForeignKey("clinical.Procedure", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                  related_name="+")  # copied from the package at sale
    name = models.CharField(max_length=120)
    sessions = models.IntegerField()
    used = models.IntegerField(default=0)
    price = models.BigIntegerField()
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+")
    invoice_line_id = models.BigIntegerField(null=True, blank=True)
    appointment = models.ForeignKey("scheduling.Appointment", on_delete=models.PROTECT, db_constraint=False,
                                    db_index=False, related_name="+")
    sold_at = models.DateTimeField(default=timezone.now)
    sold_by_id = models.BigIntegerField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)  # its line removed or invoice voided before any use

    class Meta:
        db_table = "billing_patient_package"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="patient_package_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(used__gte=0, used__lte=F("sessions"), sessions__gte=1, price__gte=0),
                                   name="patient_package_used_valid"),
            CompositeForeignKey(fields=["clinic", "patient"], to="patients.Patient", to_fields=["clinic", "id"],
                                name="patient_package_patient_fk"),
            CompositeForeignKey(fields=["clinic", "package"], to="billing.Package", to_fields=["clinic", "id"],
                                name="patient_package_package_fk"),
            CompositeForeignKey(fields=["clinic", "procedure"], to="clinical.Procedure", to_fields=["clinic", "id"],
                                name="patient_package_procedure_fk"),
            CompositeForeignKey(fields=["clinic", "invoice"], to="billing.Invoice", to_fields=["clinic", "id"],
                                name="patient_package_invoice_fk"),
            CompositeForeignKey(fields=["clinic", "appointment"], to="scheduling.Appointment",
                                to_fields=["clinic", "id"], name="patient_package_appointment_fk"),
        ]
        indexes = [
            models.Index(fields=["clinic", "patient", "id"], name="patient_package_patient"),
            # Unused sessions (F6 lists, the session use in F7). Its predicate reads `used`, so a session use is a
            # non-HOT update of one small row; fine at a few uses a day.
            models.Index(fields=["clinic", "patient", "procedure"],
                         condition=Q(used__lt=F("sessions"), voided_at__isnull=True), name="patient_package_open"),
        ]
