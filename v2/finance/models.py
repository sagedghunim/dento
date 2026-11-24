"""The back office's money (design 4.8, F1, F3, F4). Shard tables, all under forced row-level security.

Money is bigint piastres. Nothing is deleted: a shift closes, an expense is voided, a pay rule is
replaced in place (audited), a payout stays. References between rows are composite foreign keys
on (clinic_id, id).
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import CompositeForeignKey

PAYOUT_METHODS = ["cash", "card", "transfer", "wallet", "instapay", "other"]


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


class Shift(models.Model):
    """A cash register shift (F1): one open per place, opened with a counted float, closed with a count."""

    clinic = _clinic()
    status = models.CharField(max_length=6, default="open")
    opening_float = models.BigIntegerField(default=0)
    opened_at = models.DateTimeField(default=timezone.now)
    opened_by_id = models.BigIntegerField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by_id = models.BigIntegerField(null=True, blank=True)
    expected = models.BigIntegerField(null=True, blank=True)  # what the drawer should hold, worked out at closing
    counted = models.BigIntegerField(null=True, blank=True)
    difference = models.BigIntegerField(null=True, blank=True)  # counted - expected
    note = models.CharField(max_length=200, blank=True, default="")
    auto_opened = models.BooleanField(default=False, db_default=False)  # opened by a cash entry, float 0 (F1)
    # Closed by the end-of-day sweep, not counted (Saged, 2026-11-24): counted = expected, difference 0.
    auto_closed = models.BooleanField(default=False, db_default=False)

    class Meta:
        db_table = "finance_shift"
        constraints = [
            _uniq("shift_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic"], condition=Q(status="open"), name="shift_one_open"),
            models.CheckConstraint(condition=Q(status__in=["open", "closed"]), name="shift_status_valid"),
            models.CheckConstraint(condition=Q(opening_float__gte=0), name="shift_float_not_negative"),
            models.CheckConstraint(
                condition=Q(status="open", closed_at__isnull=True, counted__isnull=True)
                | Q(status="closed", closed_at__isnull=False, counted__gte=0, expected__isnull=False,
                    difference=F("counted") - F("expected")),
                name="shift_closing_valid"),
        ]
        indexes = [models.Index("clinic", F("opened_at").desc(), F("id").desc(), name="shift_list")]


class CashMovement(models.Model):
    """Cash in or out of the drawer with a reason: petty cash, a bank deposit (F1)."""

    clinic = _clinic()
    shift = _ref(Shift)
    kind = models.CharField(max_length=3)
    amount = models.BigIntegerField()
    reason = models.CharField(max_length=200)
    at = models.DateTimeField(default=timezone.now)
    by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "finance_cash_movement"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["in", "out"]), name="cash_movement_kind_valid"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="cash_movement_amount_positive"),
            _fk("shift", "finance.Shift", "cash_movement_shift_fk"),
        ]
        indexes = [models.Index(fields=["clinic", "shift", "id"], name="cash_movement_shift")]


class Category(models.Model):
    """Expense categories (F3). The defaults are copied per place; the place edits its own."""

    clinic = _clinic()
    name = models.CharField(max_length=60)
    name_en = models.CharField(max_length=60, blank=True, default="", db_default="")  # design G3
    code = models.CharField(max_length=20, blank=True, default="")  # the default it came from: rent, materials...
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "finance_category"
        constraints = [
            _uniq("category_clinic_id_uniq"),
            models.UniqueConstraint(fields=["clinic", "name"], condition=Q(is_active=True), name="category_active_name_uniq"),
        ]


class Expense(models.Model):
    clinic = _clinic()
    category = _ref(Category)
    amount = models.BigIntegerField()
    day = models.DateField()
    note = models.CharField(max_length=200, blank=True, default="")
    shift = _ref(Shift, null=True)  # paid from the drawer of this shift
    purchase = models.BooleanField(default=False)  # made by a stock purchase; voided only with it
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "finance_expense"
        constraints = [
            _uniq("expense_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
            _fk("category", "finance.Category", "expense_category_fk"),
            _fk("shift", "finance.Shift", "expense_shift_fk"),
        ]
        indexes = [
            models.Index("clinic", F("day").desc(), F("id").desc(), name="expense_day"),
            models.Index(fields=["clinic", "shift"], condition=Q(shift__isnull=False), name="expense_shift"),
        ]


class PayRule(models.Model):
    """How a member is paid (F4): a share of the revenue they produce, a fixed amount per visit or per
    procedure, a monthly salary, or any mix. One rule per member, changed in place with a version."""

    clinic = _clinic()
    membership = _ref("clinics.Membership")
    percent_bp = models.IntegerField(default=0)  # basis points: 3550 = 35.5%
    per_visit = models.BigIntegerField(default=0)
    per_procedure = models.BigIntegerField(default=0)
    monthly_salary = models.BigIntegerField(default=0)
    version = models.IntegerField(default=1)
    updated_at = models.DateTimeField(default=timezone.now)
    updated_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "finance_pay_rule"
        constraints = [
            models.UniqueConstraint(fields=["clinic", "membership"], name="pay_rule_one_per_member"),
            models.CheckConstraint(condition=Q(percent_bp__gte=0, percent_bp__lte=10000), name="pay_rule_percent_valid"),
            models.CheckConstraint(condition=Q(per_visit__gte=0, per_procedure__gte=0, monthly_salary__gte=0),
                                   name="pay_rule_money_not_negative"),
            _fk("membership", "clinics.Membership", "pay_rule_membership_fk"),
        ]


class Payout(models.Model):
    """Money paid to a member against a month's statement (F4). Cash comes out of an open shift."""

    clinic = _clinic()
    membership = _ref("clinics.Membership")
    month = models.DateField()  # the first day of the statement's month
    amount = models.BigIntegerField()
    method = models.CharField(max_length=8)
    shift = _ref(Shift, null=True)
    day = models.DateField()  # the business day it was paid
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "finance_payout"
        constraints = [
            _uniq("payout_clinic_id_uniq"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payout_amount_positive"),
            models.CheckConstraint(condition=Q(method__in=PAYOUT_METHODS), name="payout_method_valid"),
            models.CheckConstraint(condition=~Q(method="cash") | Q(shift__isnull=False), name="payout_cash_has_shift"),
            models.CheckConstraint(condition=Q(month__day=1), name="payout_month_first_day"),
            _fk("membership", "clinics.Membership", "payout_membership_fk"),
            _fk("shift", "finance.Shift", "payout_shift_fk"),
        ]
        indexes = [
            models.Index(fields=["clinic", "membership", "month"], name="payout_member_month"),
            models.Index(fields=["clinic", "shift"], condition=Q(shift__isnull=False), name="payout_shift"),
        ]
