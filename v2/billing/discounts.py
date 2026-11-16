"""Discount types (Saged, 2026-11-13): the clinic's own discounts, applied to an invoice in one tap.

The clinic defines up to 12 active types, each a percent of the invoice or a fixed amount. The doctor suggests one on
a visit (stored on the appointment, no money changes); the desk applies it. An invoice keeps the type's id with its
name and percent as they were then, so a later edit of the type changes no invoice, the receipt prints the name, and
a percent is computed again whenever a line is added or removed (billing.services.move_subtotal).

Who may apply: a type marked `desk_can_apply` needs only billing.collect (the endpoint's code); any other needs
billing.discount. Without billing.discount a person may replace or remove only what the desk could have given
itself: no discount, or a type the desk may apply. The free-typed amount stays under billing.discount.

Lock order (design 5.0): applying and removing take the appointment, then the invoice (billing.services), and read
the type without a lock. The suggestion takes the appointment only. Managing takes the clinic row (6), as any settings
change, so the cap of 12 holds when two are added at once, then the type's own row.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.errors import ApiError
from summaries.services import Deltas

from . import services

MAX_ACTIVE = 12
LIST_LIMIT = 100  # the active ones and the latest stopped ones
MAX_FIXED = 100_000_000_00  # piastres, as any amount (core.numbers.parse_money)
COLUMNS = "id, name, kind, value, desk_can_apply, is_active, sort"
FIELDS = tuple(name.strip() for name in COLUMNS.split(","))


def _type(values):
    return dict(zip(FIELDS, values))


def not_found():
    return ApiError("not_found", _("This discount wasn't found."), 404)


def _stopped():
    return ApiError("discount_stopped", _("This discount isn't used any more."), 409)


def _check_value(kind, value):
    high = 100 if kind == "percent" else MAX_FIXED
    if not 1 <= value <= high:
        message = _("A percent discount is from 1 to 100.") if kind == "percent" else _("Enter an amount.")
        raise ApiError("invalid", message, fields={"value": "range"})


def get(ctx, type_id):
    found = sql.row(ctx, f'SELECT {COLUMNS} FROM "billing_discount_type" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, type_id])
    if found is None:
        raise not_found()
    return _type(found)


def types(ctx, include_inactive=False):
    """The clinic's types in their order, the active ones first (one statement on (clinic_id, id); a dozen rows)."""
    where = "" if include_inactive else " AND is_active"
    return [_type(r) for r in sql.rows(
        ctx, f'SELECT {COLUMNS} FROM "billing_discount_type" WHERE clinic_id = %s{where} '
             "ORDER BY is_active DESC, sort, id LIMIT %s", [ctx.clinic_id, LIST_LIMIT])]


# --- Managing (settings.manage) -----------------------------------------------------------------------------------


def _lock_clinic(ctx):
    from clinics.models import Clinic

    Clinic.objects.select_for_update(no_key=True).get(id=ctx.clinic_id)  # lock order 6 (design 5.0)


def _check_room(ctx):
    """Under the clinic's lock: there is room for one more active type."""
    active = sql.row(ctx, 'SELECT count(*) FROM "billing_discount_type" WHERE clinic_id = %s AND is_active',
                     [ctx.clinic_id])[0]
    if active >= MAX_ACTIVE:
        raise ApiError("too_many", _("A clinic has up to 12 discount types. Stop one first."), 409)


def _savepoint(ctx, statement, params):
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == "discount_type_active_name_uniq":
            raise ApiError("duplicate", _("This discount is already in the list."), 409, fields={"name": "duplicate"})
        raise


def create(ctx, name, kind, value, desk_can_apply=True):
    """A new active type at the end of the list. Audited (it sets what the desk may take off a bill)."""
    _check_value(kind, value)
    _lock_clinic(ctx)
    _check_room(ctx)
    created = _type(_savepoint(ctx, f"""
        INSERT INTO "billing_discount_type" (clinic_id, name, kind, value, desk_can_apply, is_active, sort, created_at)
        SELECT %s, %s, %s, %s, %s, true, least(coalesce(max(sort), 0) + 1, 32000), %s
          FROM "billing_discount_type" WHERE clinic_id = %s AND is_active
        RETURNING {COLUMNS}""", [ctx.clinic_id, name, kind, value, desk_can_apply, timezone.now(), ctx.clinic_id]))
    record_change(ctx, "discount_type.create", "discount_type", created["id"],
                  after={"name": name, "kind": kind, "value": value, "desk_can_apply": desk_can_apply})
    return created


def update(ctx, type_id, values):
    """Absolute values for name, kind, value, desk_can_apply, is_active, sort. Invoices that already carry the type
    keep the name and the percent they were given. Audited."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    if values.get("is_active"):
        _lock_clinic(ctx)  # before the type's row: the same order as adding one
    before = sql.row(ctx, f'SELECT {COLUMNS} FROM "billing_discount_type" WHERE clinic_id = %s AND id = %s '
                          "FOR NO KEY UPDATE", [ctx.clinic_id, type_id])
    if before is None:
        raise not_found()
    before = _type(before)
    _check_value(values.get("kind", before["kind"]), values.get("value", before["value"]))
    if values.get("is_active") and not before["is_active"]:
        _check_room(ctx)
    assignments = ", ".join(f"{name} = %s" for name in values)  # names from the view's fixed list, never the body
    after = _type(_savepoint(
        ctx, f'UPDATE "billing_discount_type" SET {assignments} WHERE clinic_id = %s AND id = %s RETURNING {COLUMNS}',
        [*values.values(), ctx.clinic_id, type_id]))
    record_change(ctx, "discount_type.update", "discount_type", type_id, before={n: before[n] for n in values},
                  after={n: after[n] for n in values})
    return after


def reorder(ctx, ids):
    """The active types in the order given: absolute, so a resend changes nothing. The list must be exactly the
    active types; otherwise someone changed them meanwhile (409 `stale`, the screen reads them again)."""
    _lock_clinic(ctx)
    active = [r[0] for r in sql.rows(ctx, 'SELECT id FROM "billing_discount_type" WHERE clinic_id = %s AND is_active '
                                          "ORDER BY sort, id", [ctx.clinic_id])]
    if len(ids) != len(set(ids)) or set(ids) != set(active):
        raise ApiError("stale", _("The discounts changed meanwhile. They were loaded again."), 409)
    if ids != active:
        sql.run(ctx, 'UPDATE "billing_discount_type" AS d SET sort = o.position FROM unnest(%s::bigint[]) '
                         "WITH ORDINALITY AS o(id, position) WHERE d.clinic_id = %s AND d.id = o.id",
                    [ids, ctx.clinic_id])
        record_change(ctx, "discount_type.reorder", "clinic", ctx.clinic_id, before={"order": active},
                      after={"order": ids})
    return types(ctx)


# --- Applying one to an invoice (billing.collect; billing.discount for a type the desk may not apply) ----------


def may_change(ctx, invoice, desk_can_apply):
    """Whether this person may replace or remove the invoice's present discount.

    billing.discount may change any. Without it, only what the desk could have given itself: no discount, or a
    type the desk may apply (`desk_can_apply` is the present type's flag, None for a free-typed amount).
    """
    if ctx.can("billing.discount"):
        return True
    if not ctx.can("billing.collect"):
        return False
    return bool(desk_can_apply) if invoice["discount_type_id"] is not None else invoice["discount"] == 0


def _require_change(ctx, invoice):
    present = get(ctx, invoice["discount_type_id"]) if invoice["discount_type_id"] is not None else None
    if not may_change(ctx, invoice, present and present["desk_can_apply"]):
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)


def _stale(invoice):
    return ApiError("stale", _("The discount changed meanwhile. Reload and try again."), 409,
                    fields={"discount": invoice["discount"]})


def _written(ctx, invoice, appointment, updated):
    record_change(ctx, "invoice.discount", "invoice", invoice["id"],
                  before={"discount": invoice["discount"], "type": invoice["discount_type_id"]},
                  after={"discount": updated["discount"], "type": updated["discount_type_id"],
                         **({"name": updated["discount_name"]} if updated["discount_type_id"] else {})})
    version = Deltas().clinic_day(services._visit_day(ctx, invoice, appointment)).write_clinics(ctx)
    return updated, version


def apply(ctx, invoice_id, type_id, expected):
    """PUT invoices/{id}/discount-type: put this type's discount on the invoice, or remove the type (type_id None).

    `expected` is the discount the screen saw (design 5.3): another value is 409 `stale`. Applying the type the
    invoice already carries, for the same amount, is a harmless repeat. Returns (invoice, versions or None).
    """
    invoice, appointment = services._lock_invoice(ctx, invoice_id)
    services._require_open(invoice)
    if type_id is None:
        return _remove(ctx, invoice, appointment, expected)
    kind = get(ctx, type_id)
    if not kind["desk_can_apply"]:
        ctx.require("billing.discount")
    percent = kind["value"] if kind["kind"] == "percent" else None
    discount = services.percent_of(invoice["subtotal"], percent) if percent is not None else kind["value"]
    if (invoice["discount_type_id"], invoice["discount"], invoice["discount_percent"]) == (kind["id"], discount, percent):
        return invoice, None  # already so
    if not kind["is_active"]:
        raise _stopped()
    _require_change(ctx, invoice)
    if invoice["discount"] != expected:
        raise _stale(invoice)
    if discount > invoice["subtotal"]:
        raise ApiError("discount_too_large", _("The discount can't be more than the invoice."), 409,
                       fields={"discount": "range"})
    if not services.fits(invoice, invoice["subtotal"] - discount):
        raise services._paid_exceeds()
    updated = services.write_discount(ctx, invoice["id"], discount, kind["id"], kind["name"], percent)
    return _written(ctx, invoice, appointment, updated)


def _remove(ctx, invoice, appointment, expected):
    if invoice["discount_type_id"] is None:
        return invoice, None  # no type to remove: a free-typed amount is changed under billing.discount
    _require_change(ctx, invoice)
    if invoice["discount"] != expected:
        raise _stale(invoice)
    return _written(ctx, invoice, appointment, services.write_discount(ctx, invoice["id"], 0))


# --- The doctor's suggestion (discounts.suggest, on a visit in the person's scope) ------------------------------


def suggest(ctx, appointment_id, type_id):
    """PUT appointments/{id}/suggested-discount: the type the doctor suggests for this visit, or none.

    An absolute value on the locked appointment, so a repeat changes nothing. It changes no money and no summary:
    the desk sees it in the Pay dialog and applies it. For holders of discounts.suggest (off for a doctor until the
    owner opens it, Saged 2026-11-16): the endpoint's code, the visit's scope (lock_appointment), and a visit that
    has arrived.
    """
    appointment = services.lock_appointment(ctx, appointment_id)
    if appointment["status"] in ("booked", "cancelled", "no_show"):
        raise ApiError("visit_closed", _("Suggest a discount on a visit that has arrived."), 409,
                       fields={"status": appointment["status"]})
    kind = None
    if type_id is not None:
        kind = get(ctx, type_id)
        if not kind["is_active"]:
            raise _stopped()
    sql.run(ctx, 'UPDATE "scheduling_appointment" SET suggested_discount_type_id = %s WHERE clinic_id = %s AND id = %s',
            [type_id, ctx.clinic_id, appointment_id])
    return {"id": kind["id"], "name": kind["name"]} if kind else None


def suggested(ctx, appointment_id):
    """The visit's suggested type while it is still active, or None. One read, with the visit's scope checked."""
    from scheduling.rules import check_scope

    found = sql.row(ctx, """
        SELECT a.section_id, a.doctor_id, d.id, d.name
          FROM "scheduling_appointment" a
          LEFT JOIN "billing_discount_type" d ON d.clinic_id = a.clinic_id AND d.id = a.suggested_discount_type_id
                                              AND d.is_active
         WHERE a.clinic_id = %s AND a.id = %s""", [ctx.clinic_id, appointment_id])
    if found is None:
        raise services.not_found("appointment")
    check_scope(ctx, found[0], found[1])
    return {"id": found[2], "name": found[3]} if found[2] else None


def for_visit(ctx, appointment_id):
    """GET appointments/{id}/discount-types: what the Pay dialog offers (two statements)."""
    suggestion = suggested(ctx, appointment_id)
    may_all = ctx.can("billing.discount")
    return {"items": [dict(kind, allowed=may_all or kind["desk_can_apply"]) for kind in types(ctx)],
            "suggested": suggestion}
