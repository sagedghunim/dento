"""Billing endpoints (design 6.2, 5.3). Views validate, check codes and call one service."""

from audit.models import AuditAccess
from audit.services import record_access
from core import validate
from core.api import Result, api
from core.numbers import parse_int, parse_money

from . import selectors, services
from .models import LINE_KINDS, METHODS


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
        ctx, invoice_id, validate.choice(data, "kind", LINE_KINDS),
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
