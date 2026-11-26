"""Stock and procedure materials (design 4.7, F2). Shard tables, all under forced row-level security.

Stock changes only through conditional updates in services (design 5.4); the CHECK stops a
negative quantity even if a service forgets the condition, unless the item allows it. Every
change writes a movement with the balance after it, so the ledger explains the quantity.
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

MOVEMENT_KINDS = ["purchase", "use", "adjust", "waste", "return"]  # return: a removed procedure's materials


def _clinic():
    return models.ForeignKey("clinics.Clinic", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                             related_name="+")


def _ref(to, null=False):
    return models.ForeignKey(to, on_delete=models.PROTECT, db_constraint=False, db_index=False, related_name="+",
                             null=null, blank=null)


def _fk(field, to, name):
    return CompositeForeignKey(fields=["clinic", field], to=to, to_fields=["clinic", "id"], name=name)


class Item(models.Model):
    """A stock item, in one section or shared across the place (section null)."""

    clinic = _clinic()
    section = _ref("clinics.Section", null=True)
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120, blank=True, default="", db_default="")  # design G3
    category = models.CharField(max_length=40, blank=True, default="")
    sku = models.CharField(max_length=40, blank=True, default="")
    unit = models.CharField(max_length=20, default="unit")
    quantity = models.IntegerField(default=0)
    threshold = models.IntegerField(default=0)  # the minimum: at or below it the item is low
    unit_cost = models.BigIntegerField(default=0)  # average cost per unit, piastres
    notes = models.CharField(max_length=500, blank=True, default="")
    allow_negative = models.BooleanField(default=False)  # F2: "unless the place allows it", per item
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Who sells it (design H1): low stock prepares an order to this supplier; the backup is a name for the desk.
    supplier = _ref("inventory.Supplier", null=True)
    backup_supplier = _ref("inventory.Supplier", null=True)
    max_qty = models.IntegerField(default=0, db_default=0)  # what an order fills up to; 0 = twice the minimum
    # Short with nobody to bring it (design H1 backup): the "needs a supplier" list. Never set by stock use.
    need_qty = models.IntegerField(default=0, db_default=0)

    class Meta:
        db_table = "inventory_item"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="item_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "name"], condition=Q(is_active=True), name="item_active_name_uniq"),
            models.CheckConstraint(condition=Q(quantity__gte=0) | Q(allow_negative=True), name="item_quantity_not_negative"),
            models.CheckConstraint(condition=Q(threshold__gte=0, unit_cost__gte=0), name="item_threshold_cost_valid"),
            _fk("section", "clinics.Section", "item_section_fk"),
            # Added NOT VALID on the live table, validated in 0008 (design H1).
            models.CheckConstraint(condition=Q(max_qty__gte=0), name="item_max_qty_valid"),
            models.CheckConstraint(condition=~Q(supplier=F("backup_supplier")), name="item_backup_not_supplier"),
            _fk("supplier", "inventory.Supplier", "item_supplier_fk"),
            _fk("backup_supplier", "inventory.Supplier", "item_backup_supplier_fk"),
            # Added NOT VALID, validated in 0011 (design H1 backup).
            models.CheckConstraint(condition=Q(need_qty__gte=0), name="item_need_qty_valid"),
        ]
        indexes = [models.Index(fields=["clinic", "supplier"], condition=Q(supplier__isnull=False),
                                name="item_supplier"),
                   # The side bar's count of items nobody will bring; only those rare writes leave HOT.
                   models.Index(fields=["clinic"], condition=Q(need_qty__gt=0), name="item_need")]
        # No partial low-stock index (design 4.7): it would make every stock use a non-HOT update.


class Movement(models.Model):
    clinic = _clinic()
    item = _ref(Item)
    kind = models.CharField(max_length=8)
    qty = models.IntegerField()  # signed: + in, - out
    balance_after = models.IntegerField()
    unit_cost = models.BigIntegerField(default=0)  # the average cost at that moment
    patient = _ref("patients.Patient", null=True)
    appointment = _ref("scheduling.Appointment", null=True)
    visit_procedure_id = models.BigIntegerField(null=True, blank=True)
    expense = _ref("finance.Expense", null=True)
    reason = models.CharField(max_length=200, blank=True, default="")
    created_by_id = models.BigIntegerField(null=True, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "inventory_movement"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=MOVEMENT_KINDS), name="movement_kind_valid"),
            models.CheckConstraint(condition=~Q(qty=0), name="movement_qty_not_zero"),
            _fk("item", "inventory.Item", "movement_item_fk"),
            _fk("patient", "patients.Patient", "movement_patient_fk"),
            _fk("appointment", "scheduling.Appointment", "movement_appointment_fk"),
            _fk("expense", "finance.Expense", "movement_expense_fk"),
        ]
        indexes = [
            models.Index("clinic", "item", F("at").desc(), F("id").desc(), name="movement_item"),
            models.Index("clinic", F("at").desc(), F("id").desc(), condition=Q(kind="use"), name="movement_use"),
            # The month's expired or broken stock on the expenses page (Saged, 2026-11-27).
            models.Index("clinic", F("at").desc(), F("id").desc(), condition=Q(kind="waste"), name="movement_waste"),
            # A removed procedure gives its materials back (Saged, 2026-11-17): its movements, found by the record.
            models.Index(fields=["clinic", "visit_procedure_id"], condition=Q(visit_procedure_id__isnull=False),
                         name="movement_visit_procedure"),
            # What the visit took by hand (Saged, 2026-11-25): the visit page reads it by appointment.
            models.Index(fields=["clinic", "appointment"], condition=Q(appointment__isnull=False),
                         name="movement_appointment"),
        ]


class Material(models.Model):
    """What a procedure uses (F2): recording the procedure deducts these and costs its line."""

    clinic = _clinic()
    procedure = _ref("clinical.Procedure")
    item = _ref(Item)
    qty = models.IntegerField()

    class Meta:
        db_table = "inventory_material"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "procedure", "item"], name="material_uniq"),
            models.CheckConstraint(condition=Q(qty__gt=0, qty__lte=10000), name="material_qty_valid"),
            _fk("procedure", "clinical.Procedure", "material_procedure_fk"),
            _fk("item", "inventory.Item", "material_item_fk"),
        ]


# --- Suppliers and purchase orders (design H1) ------------------------------------------------------------------

SUPPLIER_KINDS = ["supplier", "lab"]
ORDER_STATUSES = ["draft", "sent", "done", "cancelled"]
ENTRY_KINDS = ["bill", "payment"]


class Supplier(models.Model):
    """Who the place buys from: a supplier, or (H2) a dental lab. One table, so one account serves both."""

    clinic = _clinic()
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, blank=True, default="")  # digits, with the country code (201...)
    notes = models.CharField(max_length=300, blank=True, default="")
    kind = models.CharField(max_length=8, default="supplier")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    # When the supplier usually comes by (Saged, 2026-11-25): weekdays as bits, Monday = 1 << 0 (Python's weekday),
    # and an optional time of day: the clinic's local clock time, not UTC (no date to convert it with).
    visit_days = models.SmallIntegerField(default=0, db_default=0)
    visit_time = models.TimeField(null=True, blank=True)
    # Who to ask when this one hasn't got an item (design H1 backup): one step only, an item's own backup first.
    backup = models.ForeignKey("self", on_delete=models.PROTECT, db_constraint=False, db_index=False,
                               related_name="+", null=True, blank=True)

    class Meta:
        db_table = "inventory_supplier"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="supplier_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "kind", "name"], condition=Q(is_active=True),
                                    name="supplier_active_name_uniq"),
            models.CheckConstraint(condition=Q(kind__in=SUPPLIER_KINDS), name="supplier_kind_valid"),
            models.CheckConstraint(condition=Q(phone__regex=r"^[0-9]*$"), name="supplier_phone_digits"),
            models.CheckConstraint(condition=Q(visit_days__gte=0, visit_days__lte=127), name="supplier_visit_days_valid"),
            models.CheckConstraint(condition=~Q(backup=F("id")), name="supplier_backup_not_self"),
            _fk("backup", "inventory.Supplier", "supplier_backup_fk"),
        ]


class Order(models.Model):
    clinic = _clinic()
    supplier = _ref(Supplier)
    status = models.CharField(max_length=10, default="draft")
    note = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)  # null: the system (refill)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by_id = models.BigIntegerField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "inventory_order"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="order_clinic_id_uniq"),
            # One draft per supplier: refill adds to it (INSERT ... ON CONFLICT).
            models.UniqueConstraint(fields=["clinic", "supplier"], condition=Q(status="draft"), name="order_one_draft"),
            models.CheckConstraint(condition=Q(status__in=ORDER_STATUSES), name="order_status_valid"),
            models.CheckConstraint(condition=Q(status__in=["draft", "cancelled"]) | Q(sent_at__isnull=False),
                                   name="order_sent_at_set"),
            models.CheckConstraint(condition=Q(status__in=["done", "cancelled"], closed_at__isnull=False)
                                   | Q(status__in=["draft", "sent"], closed_at__isnull=True), name="order_closed_at_set"),
            _fk("supplier", "inventory.Supplier", "order_supplier_fk"),
        ]
        indexes = [models.Index("clinic", "status", F("created_at").desc(), F("id").desc(), name="order_status")]


class OrderLine(models.Model):
    clinic = _clinic()
    order = _ref(Order)
    item = _ref(Item)
    qty = models.IntegerField()
    received = models.IntegerField(default=0)
    unit_price = models.BigIntegerField(default=0)  # piastres, from the last receipt
    # What the supplier hasn't got ("مش عنده", or short when the order closed): it moved to a backup or the list.
    missing = models.IntegerField(default=0, db_default=0)
    # Put here by a backup move or picked by hand from the list: short again, it goes to the list (one step).
    is_backup = models.BooleanField(default=False, db_default=False)

    class Meta:
        db_table = "inventory_order_line"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "order", "item"], name="order_line_uniq"),
            models.CheckConstraint(condition=Q(qty__gt=0, received__gte=0, unit_price__gte=0), name="order_line_valid"),
            models.CheckConstraint(condition=Q(missing__gte=0, missing__lte=F("qty")), name="order_line_missing_valid"),
            _fk("order", "inventory.Order", "order_line_order_fk"),
            _fk("item", "inventory.Item", "order_line_item_fk"),
        ]
        indexes = [models.Index(fields=["clinic", "item"], name="order_line_item")]


class VendorEntry(models.Model):
    """What we owe a supplier or a lab: bills less payments. A payment is also an expense."""

    clinic = _clinic()
    supplier = _ref(Supplier)
    kind = models.CharField(max_length=8)
    amount = models.BigIntegerField()
    day = models.DateField()
    order = _ref(Order, null=True)
    expense = _ref("finance.Expense", null=True)
    note = models.CharField(max_length=200, blank=True, default="")
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "inventory_vendor_entry"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="vendor_entry_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(kind__in=ENTRY_KINDS), name="vendor_entry_kind_valid"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="vendor_entry_amount_valid"),
            models.CheckConstraint(condition=Q(voided_at__isnull=True, voided_by_id__isnull=True)
                                   | Q(voided_at__isnull=False, voided_by_id__isnull=False), name="vendor_entry_void_valid"),
            _fk("supplier", "inventory.Supplier", "vendor_entry_supplier_fk"),
            _fk("order", "inventory.Order", "vendor_entry_order_fk"),
            _fk("expense", "finance.Expense", "vendor_entry_expense_fk"),
        ]
        indexes = [models.Index(fields=["clinic", "supplier"], include=["kind", "amount"],
                                condition=Q(voided_at__isnull=True), name="vendor_entry_open")]
