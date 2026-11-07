"""Invoices, lines and payments (design 4.5). Shard tables under row-level security.

Money is bigint piastres. Nothing is deleted except an invoice line removed from an
open invoice (audited): voids and refunds keep their rows.
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

METHODS = ["cash", "card", "wallet", "instapay", "other"]
LINE_KINDS = ["service", "procedure", "material", "other"]


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
        ]


class InvoiceLine(models.Model):
    clinic = models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, db_constraint=False, db_index=False,
                                related_name="+")
    kind = models.CharField(max_length=10)
    service = models.ForeignKey("clinics.Service", null=True, blank=True, on_delete=models.PROTECT,
                                db_constraint=False, db_index=False, related_name="+")
    # inventory_item arrives in M3, which adds the composite foreign key on (clinic_id, item_id).
    item_id = models.BigIntegerField(null=True, blank=True)
    description = models.CharField(max_length=200)
    qty = models.IntegerField(default=1)
    unit_price = models.BigIntegerField()
    amount = models.BigIntegerField()
    from_list = models.BooleanField(default=False)  # the service's list price

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
        ]
        indexes = [
            models.Index(fields=["clinic", "invoice", "id"], name="payment_invoice"),
            models.Index("clinic", "patient", F("received_at").desc(), F("id").desc(), name="payment_patient_hist"),
            models.Index(fields=["clinic", "day", "received_by_id"], name="payment_day_closing"),
        ]
