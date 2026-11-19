"""Billing endpoints (design 6.2, 5.3). Views validate, check codes and call one service."""

from audit.models import AuditAccess
from audit.services import record_access
from core import validate
from core.api import Result, api
from core.errors import ApiError
from core.numbers import parse_int, parse_money

from . import discounts, selectors, services
from .models import DISCOUNT_KINDS, LINE_KINDS, METHODS

ADDABLE_KINDS = [k for k in LINE_KINDS if k != "package"]  # a package line comes only from selling one (F7)


def _invoice_id(value):
    return validate.path_id(value, services.not_found("invoice"))


def _version_of_invoice(ctx, invoice_id):
    from scheduling.services import current_version

    appointment_id = selectors.invoice_row(ctx, invoice_id)["appointment_id"]
    return current_version(ctx, appointment_id) if appointment_id else None


def _invoice_answer(ctx, invoice_id, version=None):
    if version is None:
        version = _version_of_invoice(ctx, invoice_id)
    return {"invoice": selectors.invoice(ctx, invoice_id), "version": version}


def _payment_answer(ctx, payment, invoice, version):
    return {"payment": selectors.payment_payload(ctx.clinic, payment), "invoice": selectors.summary(invoice),
            "version": version}


def _replay_payment(ctx, stored):
    payment = selectors.payment(ctx, stored["target_id"])
    return _payment_answer(ctx, payment, selectors.invoice_row(ctx, payment["invoice_id"]),
                           _version_of_invoice(ctx, payment["invoice_id"]))


def _replay_invoice(ctx, stored):
    return _invoice_answer(ctx, stored["target_id"])


@api("POST", perm="billing.collect", replay=_replay_payment)
def pay_view(request, ctx, data, appointment_id):
    appointment_id = validate.path_id(appointment_id, services.not_found("appointment"))
    amount = parse_money(data.get("amount"), "amount", low=1)
    method = validate.choice(data, "method", METHODS)
    note = validate.text(data, "note", 200, required=False)
    payment, invoice, version = services.pay(ctx, appointment_id, amount, method, note)
    return Result(_payment_answer(ctx, payment, invoice, version), status=201, target_type="payment",
                  target_id=payment["id"])


@api("POST", perm="billing.collect", replay=_replay_invoice)
def visit_invoice_view(request, ctx, data, appointment_id):
    """Ensure the visit's invoice, so lines and discounts can address it before any payment."""
    appointment_id = validate.path_id(appointment_id, services.not_found("appointment"))
    invoice, created, version = services.invoice_for_visit(ctx, appointment_id)
    return Result(_invoice_answer(ctx, invoice["id"], version), status=201 if created else 200, target_type="invoice",
                  target_id=invoice["id"])


@api("GET", perm="billing.collect", access="invoice")
def invoice_view(request, ctx, data, invoice_id):
    body = selectors.invoice(ctx, _invoice_id(invoice_id))
    record_access(ctx, body["patient"]["id"], AuditAccess.Action.INVOICE)
    return {"invoice": body}


@api("POST", perm="billing.collect", replay=_replay_invoice)
def add_line_view(request, ctx, data, invoice_id):
    invoice_id = _invoice_id(invoice_id)
    service_id = data.get("service_id")
    unit_price = data.get("unit_price")
    _, version = services.add_line(
        ctx, invoice_id, validate.choice(data, "kind", ADDABLE_KINDS),
        validate.ident(service_id, "service_id") if service_id is not None else None,
        validate.text(data, "description", 200, required=False),
        parse_int(data.get("qty", 1), "qty", 1, 1000),
        parse_money(unit_price, "unit_price") if unit_price is not None else None)
    return Result(_invoice_answer(ctx, invoice_id, next(iter(version.values()))), status=201, target_type="invoice",
                  target_id=invoice_id)


@api("DELETE", perm="billing.collect", replay=_replay_invoice)
def remove_line_view(request, ctx, data, invoice_id, line_id):
    invoice_id = _invoice_id(invoice_id)
    line_id = validate.path_id(line_id, services.not_found("line"))
    version = services.remove_line(ctx, invoice_id, line_id)
    return Result(_invoice_answer(ctx, invoice_id, next(iter(version.values()))), target_type="invoice",
                  target_id=invoice_id)


@api("PUT", perm="billing.discount", replay=_replay_invoice)
def discount_view(request, ctx, data, invoice_id):
    invoice_id = _invoice_id(invoice_id)
    _, version = services.set_discount(ctx, invoice_id, parse_money(data.get("discount"), "discount"),
                                       parse_money(data.get("expected_discount"), "expected_discount"))
    return Result(_invoice_answer(ctx, invoice_id, next(iter(version.values())) if version else None),
                  target_type="invoice", target_id=invoice_id)


@api("POST", perm="billing.refund", replay=_replay_invoice)
def void_view(request, ctx, data, invoice_id):
    invoice_id = _invoice_id(invoice_id)
    version = services.void(ctx, invoice_id)
    return Result(_invoice_answer(ctx, invoice_id, next(iter(version.values())) if version else None),
                  target_type="invoice", target_id=invoice_id)


@api("POST", perm="billing.refund", replay=_replay_payment)
def refund_view(request, ctx, data, payment_id):
    payment_id = validate.path_id(payment_id, services.not_found("payment"))
    method = data.get("method")
    refund, invoice, version = services.refund(
        ctx, payment_id, parse_money(data.get("amount"), "amount", low=1),
        validate.choice(data, "method", METHODS) if method is not None else None,
        validate.text(data, "note", 200, required=False))
    return Result(_payment_answer(ctx, refund, invoice, version), status=201, target_type="payment",
                  target_id=refund["id"])


# --- Session packages (F7) -------------------------------------------------------------------------------------


def _package_id(value):
    return validate.path_id(value, services.package_not_found())


def _package_values(data, partial):
    values = {}
    if not partial or "name" in data:
        values["name"] = validate.text(data, "name", 120)
    if not partial or "sessions" in data:
        values["sessions"] = parse_int(data.get("sessions"), "sessions", 1, 500)
    if not partial or "price" in data:
        values["price"] = parse_money(data.get("price"), "price")
    if partial and "is_active" in data:
        values["is_active"] = validate.boolean(data, "is_active")
    return values


@api("GET", perm="billing.collect")
def packages_view(request, ctx, data):
    return {"items": services.packages(ctx, include_inactive=request.GET.get("all") == "1")}


def _replay_package(ctx, stored):
    return {"package": next(p for p in services.packages(ctx, True) if p["id"] == stored["target_id"])}


@api("POST", perm="packages.manage", replay=_replay_package)
def create_package_view(request, ctx, data):
    """POST {procedure_id, name, sessions, price}."""
    values = _package_values(data, partial=False)
    package = services.create_package(ctx, validate.ident(data.get("procedure_id"), "procedure_id"), values["name"],
                                      values["sessions"], values["price"])
    return Result({"package": package}, status=201, target_type="package", target_id=package["id"])


@api("PATCH", perm="packages.manage", replay=_replay_package)
def update_package_view(request, ctx, data, package_id):
    """PATCH {name?, sessions?, price?, is_active?}: absolute values."""
    package_id = _package_id(package_id)
    package = services.update_package(ctx, package_id, _package_values(data, partial=True))
    return Result({"package": package}, target_type="package", target_id=package_id)


def _replay_sale(ctx, stored):
    sold = selectors.patient_package(ctx, stored["target_id"])
    return {"patient_package": sold, **_invoice_answer(ctx, sold["invoice_id"])}


@api("POST", perm="billing.collect", replay=_replay_sale)
def sell_package_view(request, ctx, data, appointment_id):
    """POST {package_id}: sell a package on this visit (a line on its invoice and the patient's sessions)."""
    appointment_id = validate.path_id(appointment_id, services.not_found("appointment"))
    sold, invoice, version = services.sell_package(ctx, appointment_id,
                                                   validate.ident(data.get("package_id"), "package_id"))
    return Result({"patient_package": selectors.patient_package_payload(ctx.clinic, sold),
                   **_invoice_answer(ctx, invoice["id"], version)}, status=201, target_type="patient_package",
                  target_id=sold["id"])


@api("GET", perm="patients.view", access="history")
def patient_packages_view(request, ctx, data, patient_id):
    """The patient's packages, those with sessions left first (F7 "unused sessions show on the patient")."""
    from patients.selectors import exists
    from patients.views import _not_found

    patient_id = validate.path_id(patient_id, _not_found())
    exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    from . import visibility

    return {"items": visibility.rows(ctx, selectors.patient_packages(ctx, patient_id), "price")}


# --- Discount types (Saged, 2026-11-13) --------------------------------------------------------------------------


def _type_id(value):
    return validate.path_id(value, discounts.not_found())


def _type_values(data, partial):
    """name, kind, value (a whole percent, or piastres), desk_can_apply; a PATCH also is_active and sort.

    The value's range depends on the kind, which a PATCH may leave as it is: the service checks the pair."""
    values = {}
    if not partial or "name" in data:
        values["name"] = validate.text(data, "name", 60)
    if not partial or "kind" in data:
        values["kind"] = validate.choice(data, "kind", DISCOUNT_KINDS)
    if not partial or "value" in data:
        values["value"] = parse_int(data.get("value"), "value", 1, discounts.MAX_FIXED)
    if "desk_can_apply" in data:
        values["desk_can_apply"] = validate.boolean(data, "desk_can_apply")
    if partial and "is_active" in data:
        values["is_active"] = validate.boolean(data, "is_active")
    if partial and "sort" in data:
        values["sort"] = parse_int(data.get("sort"), "sort", 0, 32000)
    return values


@api("GET", perm="settings.manage")
def discount_types_view(request, ctx, data):
    """The clinic's discount types for Team & settings: the active ones, or all of them with ?all=1."""
    return {"items": discounts.types(ctx, include_inactive=request.GET.get("all") == "1")}


def _replay_type(ctx, stored):
    return {"discount_type": discounts.get(ctx, stored["target_id"])}


@api("POST", perm="settings.manage", replay=_replay_type)
def create_discount_type_view(request, ctx, data):
    """POST {name, kind: percent | fixed, value, desk_can_apply?}."""
    values = _type_values(data, partial=False)
    created = discounts.create(ctx, values["name"], values["kind"], values["value"], values.get("desk_can_apply", True))
    return Result({"discount_type": created}, status=201, target_type="discount_type", target_id=created["id"])


@api("PATCH", perm="settings.manage", replay=_replay_type)
def update_discount_type_view(request, ctx, data, type_id):
    """PATCH {name?, kind?, value?, desk_can_apply?, is_active?, sort?}: absolute values. is_active false stops it."""
    type_id = _type_id(type_id)
    updated = discounts.update(ctx, type_id, _type_values(data, partial=True))
    return Result({"discount_type": updated}, target_type="discount_type", target_id=type_id)


@api("PUT", perm="settings.manage")
def order_discount_types_view(request, ctx, data):
    """PUT {ids: [...]}: the active types in their new order (absolute, so a resend is harmless)."""
    ids = data.get("ids")
    if not isinstance(ids, list) or len(ids) > discounts.MAX_ACTIVE:
        raise ApiError("invalid", "ids must be a list", fields={"ids": "list"})
    return {"items": discounts.reorder(ctx, [validate.ident(i, "ids") for i in ids])}


@api("GET", perm="billing.collect")
def visit_discount_types_view(request, ctx, data, appointment_id):
    """What the Pay dialog offers for this visit: the active types (each with `allowed` for this person) and the
    type the doctor suggested. No patient data."""
    return discounts.for_visit(ctx, validate.path_id(appointment_id, services.not_found("appointment")))


@api("PUT", perm="billing.collect", replay=_replay_invoice)
def discount_type_view(request, ctx, data, invoice_id):
    """PUT {type_id: id | null, expected_discount}: apply this type to the invoice; null removes the type.

    A type the desk may not apply also needs billing.discount (checked in the service, as a price not from the
    list is)."""
    invoice_id = _invoice_id(invoice_id)
    if "type_id" not in data:
        raise ApiError("invalid", "type_id is required (null removes the discount type)", fields={"type_id": "required"})
    type_id = validate.ident(data["type_id"], "type_id") if data["type_id"] is not None else None
    _, version = discounts.apply(ctx, invoice_id, type_id, parse_money(data.get("expected_discount"), "expected_discount"))
    return Result(_invoice_answer(ctx, invoice_id, next(iter(version.values())) if version else None),
                  target_type="invoice", target_id=invoice_id)


def _replay_suggestion(ctx, stored):
    return {"suggested": discounts.suggested(ctx, stored["target_id"])}


@api("PUT", perm="discounts.suggest", replay=_replay_suggestion)
def suggest_discount_view(request, ctx, data, appointment_id):
    """PUT {type_id: id | null}: the discount the doctor suggests for this visit; null clears it. No money changes."""
    appointment_id = validate.path_id(appointment_id, services.not_found("appointment"))
    if "type_id" not in data:
        raise ApiError("invalid", "type_id is required (null clears the suggestion)", fields={"type_id": "required"})
    type_id = validate.ident(data["type_id"], "type_id") if data["type_id"] is not None else None
    return Result({"suggested": discounts.suggest(ctx, appointment_id, type_id)}, target_type="appointment",
                  target_id=appointment_id)


# --- Installments (Saged, 2026-11-19) -------------------------------------------------------------------------


def _installment_answer(ctx, stored):
    from . import installments

    return {"plan": installments.read(ctx, stored["target_id"])}


@api("GET", perm=("billing.collect", "money.view"), access="invoice")
def installments_view(request, ctx, data, patient_id):
    from patients.selectors import exists

    from . import installments

    exists(ctx, patient_id)
    return {"plan": installments.read(ctx, patient_id)}


def _day(value, field):
    try:
        return validate.iso_day(value, field)
    except ApiError:
        raise
    except Exception:
        raise ApiError("invalid", f"{field}: a day as YYYY-MM-DD", fields={field: "day"})


@api("POST", perm=("billing.collect", "money.view"), replay=_installment_answer)
def installments_make_view(request, ctx, data, patient_id):
    """{down, every: week|2weeks|month, amount, first_due?, note?} or {parts: [{due_on, amount}], note?}."""
    from . import installments

    request_body = {"note": validate.text(data, "note", 200, required=False)}
    if data.get("parts"):
        if not isinstance(data["parts"], list):
            raise ApiError("invalid", "parts must be a list", fields={"parts": "list"})
        request_body["parts"] = [{"due_on": _day(p.get("due_on"), "due_on"), "amount": parse_money(p.get("amount"), "amount", 1)}
                                 for p in data["parts"] if isinstance(p, dict)]
    else:
        request_body.update({
            "down": parse_money(data.get("down") or 0, "down"),
            "every": validate.choice(data, "every", list(installments.EVERY)),
            "amount": parse_money(data.get("amount"), "amount", 1),
            "first_due": _day(data["first_due"], "first_due") if data.get("first_due") else None,
        })
    plan = installments.make(ctx, patient_id, request_body)
    return Result({"plan": plan}, status=201, target_type="patient", target_id=patient_id)


@api("DELETE", perm=("billing.collect", "money.view"), replay=_installment_answer)
def installments_cancel_view(request, ctx, data, patient_id):
    from . import installments

    plan = installments.cancel(ctx, patient_id)
    return Result({"plan": plan}, target_type="patient", target_id=patient_id)
