"""Stock and procedure materials (design 4.7, F2). Shard tables, all under forced row-level security.

Stock changes only through conditional updates in services (design 5.4); the CHECK stops a
negative quantity even if a service forgets the condition, unless the item allows it. Every
change writes a movement with the balance after it, so the ledger explains the quantity.
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

MOVEMENT_KINDS = ["purchase", "use", "adjust", "waste"]


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

    class Meta:
        db_table = "inventory_item"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "id"], name="item_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "name"], condition=Q(is_active=True), name="item_active_name_uniq"),
            models.CheckConstraint(condition=Q(quantity__gte=0) | Q(allow_negative=True), name="item_quantity_not_negative"),
            models.CheckConstraint(condition=Q(threshold__gte=0, unit_cost__gte=0), name="item_threshold_cost_valid"),
            _fk("section", "clinics.Section", "item_section_fk"),
        ]
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
