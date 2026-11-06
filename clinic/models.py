"""Data model. Every business table has a `clinic` owner; money is integer piastres."""
from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Specialty(models.Model):
    name = models.CharField(max_length=60, unique=True)
    tag = models.CharField(max_length=60)
    services = models.JSONField(default=list)
    features = models.JSONField(default=list)


class Clinic(models.Model):
    name = models.CharField(max_length=120)
    city = models.CharField(max_length=60)
    specialty = models.ForeignKey(Specialty, on_delete=models.PROTECT)
    timezone = models.CharField(max_length=60, default="Africa/Cairo")
    created_at = models.DateTimeField(auto_now_add=True)


class Role(models.TextChoices):
    OWNER = "owner", "Doctor (owner)"
    ASSOCIATE = "associate", "Associate"
    ASSISTANT = "assistant", "Assistant"


class Perm(models.TextChoices):
    FINANCES = "finances", "See clinic finances"
    INVENTORY = "inventory", "Add & edit inventory"
    BOOKING = "booking", "Book & change appointments"
    RX = "rx", "Write prescriptions"
    ONLINE = "online", "Run online consultations"
    TEAM = "team", "Manage team & permissions"


class Membership(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=12, choices=Role.choices)
    display_name = models.CharField(max_length=120)
    title = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["clinic", "user"], name="one_membership_per_clinic"),
            models.CheckConstraint(condition=Q(role__in=Role.values), name="membership_role_valid"),
            models.UniqueConstraint(fields=["clinic"], condition=Q(role="owner"), name="one_owner_per_clinic"),
        ]


class RolePermission(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="role_permissions")
    role = models.CharField(max_length=12, choices=Role.choices)
    perm = models.CharField(max_length=12, choices=Perm.choices)
    allowed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["clinic", "role", "perm"], name="one_row_per_role_perm"),
            models.CheckConstraint(condition=~Q(role="owner"), name="owner_never_stored"),
        ]


class Patient(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="patients")
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["clinic", "name"])]


class Appointment(models.Model):
    class Status(models.TextChoices):
        BOOKED = "booked"
        WAITING = "waiting"
        ROOM = "room"
        DONE = "done"
        CANCELLED = "cancelled"

    class Source(models.TextChoices):
        CLINIC = "clinic", "At the clinic"
        PHONE = "phone", "By phone"
        ONLINE = "online", "Online"

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="appointments")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="appointments")
    doctor = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL, related_name="appointments")
    service = models.CharField(max_length=120)
    starts_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.BOOKED)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.CLINIC)
    fee = models.BigIntegerField(default=0)
    is_new = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["clinic", "starts_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["booked", "waiting", "room", "done", "cancelled"]), name="appt_status_valid"),
            models.CheckConstraint(condition=Q(fee__gte=0), name="appt_fee_not_negative"),
        ]


class Payment(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="payments")
    appointment = models.OneToOneField(Appointment, null=True, on_delete=models.PROTECT, related_name="payment")
    amount = models.BigIntegerField()
    received_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["clinic", "received_at"])]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payment_positive")]


class StockItem(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="stock_items")
    name = models.CharField(max_length=120)
    unit = models.CharField(max_length=30)
    pack = models.CharField(max_length=120, blank=True)
    quantity = models.IntegerField(default=0)
    threshold = models.IntegerField(default=0)
    unit_price = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["clinic", "name"], name="stock_name_unique_in_clinic"),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="stock_never_negative"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="stock_price_not_negative"),
        ]


class StockMovement(models.Model):
    class Kind(models.TextChoices):
        PURCHASE = "purchase"
        USE = "use"

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="movements")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    quantity = models.IntegerField()
    unit_price = models.BigIntegerField()
    patient = models.ForeignKey(Patient, null=True, on_delete=models.PROTECT)
    created_by = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["clinic", "kind", "-created_at"])]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="movement_qty_positive")]


class Expense(models.Model):
    class Category(models.TextChoices):
        RENT = "Rent"
        ELECTRICITY = "Electricity"
        INVENTORY = "Inventory"
        SALARIES = "Salaries"
        OTHER = "Other"

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="expenses")
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=12, choices=Category.choices)
    amount = models.BigIntegerField()
    spent_at = models.DateTimeField()
    movement = models.OneToOneField(StockMovement, null=True, on_delete=models.PROTECT)
    created_by = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL)

    class Meta:
        indexes = [models.Index(fields=["clinic", "-spent_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="expense_not_negative"),
            models.CheckConstraint(condition=Q(category__in=["Rent", "Electricity", "Inventory", "Salaries", "Other"]), name="expense_category_valid"),
        ]


class StaffPay(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="staff_pay")
    name = models.CharField(max_length=120)
    title = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=[("fixed", "Fixed"), ("percent", "Percentage")])
    percent = models.PositiveSmallIntegerField(default=0)
    base = models.BigIntegerField(default=0)
    amount = models.BigIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(percent__lte=100), name="percent_at_most_100")]


class Medicine(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="medicines")
    name = models.CharField(max_length=120)
    form = models.CharField(max_length=60)
    dose = models.CharField(max_length=160)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "name"], name="medicine_unique_in_clinic")]


class Prescription(models.Model):
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="prescriptions")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="prescriptions")
    doctor = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["clinic", "patient", "-created_at"])]


class PrescriptionItem(models.Model):
    """Name and dose are copied so an old prescription never changes."""
    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="items")
    medicine_name = models.CharField(max_length=120)
    form = models.CharField(max_length=60)
    dose = models.CharField(max_length=160)
    quantity = models.PositiveSmallIntegerField()
    days = models.PositiveSmallIntegerField()


class Consultation(models.Model):
    class Status(models.TextChoices):
        NEW = "new"
        ACCEPTED = "accepted"
        IN_CALL = "incall"
        DONE = "done"
        DECLINED = "declined"

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="consultations")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    reason = models.CharField(max_length=300)
    scheduled_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)

    class Meta:
        indexes = [models.Index(fields=["clinic", "scheduled_at"])]
        constraints = [models.CheckConstraint(condition=Q(status__in=["new", "accepted", "incall", "done", "declined"]), name="consult_status_valid")]


class Activity(models.Model):
    """The day's feed, and the audit trail when `sensitive` is set (never shown in the feed)."""
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="activity")
    actor = models.ForeignKey(Membership, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    text = models.CharField(max_length=300)
    sensitive = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["clinic", "sensitive", "-created_at"])]


class IdempotencyKey(models.Model):
    """Lets a retried or double-clicked POST return the first answer instead of acting twice."""
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)
    key = models.CharField(max_length=64)
    status = models.PositiveSmallIntegerField(default=0)
    body = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["clinic", "key"], name="idempotency_unique")]
