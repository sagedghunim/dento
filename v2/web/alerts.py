"""The owner's alerts (Saged, 2026-11-19): the sensitive things the team did, read from the change log.

Removing a recorded procedure or a bill line, a discount, a refund, a voided bill, a register closed short or over,
a price change, a voided expense and a stock count that changed. The change log (audit_change) already holds each
one with who and when, so nothing new is written when they happen: this reads it on its index (clinic, at DESC).

The owner only. Opening the page marks them seen (Membership.alerts_seen_at); the side bar counts the newer ones.
The owner turns kinds of alerts off and on (Membership.alerts_off, Saged 2026-11-24): an off kind is neither listed
nor counted.
"""

import datetime as dt
import json

from django.db import OperationalError, transaction
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from audit.services import record_change
from core import sql
from core.api import Result, api, page
from core.days import local_iso
from core.errors import ApiError

from .http import clinic_page

# The kinds the owner can turn off, in the settings' order: (key, the change-log actions, the label's msgid).
# "late" is any change to a file after its visit had finished (Saged, 2026-11-22: web.file_history).
KINDS = (
    ("procedure", ("visit_procedure.remove",), "Removed a recorded procedure"),
    ("line", ("invoice.line_remove",), "Removed a line from a bill"),
    ("discount", ("invoice.discount",), "Gave a discount"),
    ("refund", ("payment.refund",), "Refunded money to a patient"),
    ("void", ("invoice.void",), "Voided a bill"),
    ("register", ("shift.close",), "The register was short or over"),
    ("cash", ("cash.in", "cash.out"), "Money added to or taken from the drawer"),
    ("price", ("procedure.price",), "Changed a price"),
    ("expense", ("expense.void",), "Voided an expense"),
    ("stock", ("item.count",), "A stock count changed the quantity"),
    ("patient", ("patient.hide", "patient.delete"), "Deleted a patient"),
    ("late", (), "A file changed after its visit ended"),
)
KEYS = tuple(k for k, _a, _l in KINDS)
ACTIONS = tuple(a for _k, actions, _l in KINDS for a in actions)
DAYS = 60
LIMIT = 150
# A register closed with the count matching is not news, nor a bill line the doctor's removed procedure took with it
# (that procedure's own alert says it).
_WHERE = ("clinic_id = %s AND (action = ANY(%s){late}) AND at >= %s "
          "AND NOT (action = 'shift.close' AND coalesce((details->'after'->>'difference')::bigint, 0) = 0) "
          "AND NOT (action = 'invoice.line_remove' AND details->>'from_procedure' = 'true')")


def _labels():
    # Written out so the string catalogue finds them (core.i18n reads the gettext calls).
    return {"procedure": _("Removed a recorded procedure"), "line": _("Removed a line from a bill"),
            "discount": _("Gave a discount"), "refund": _("Refunded money to a patient"), "void": _("Voided a bill"),
            "register": _("The register was short or over"), "cash": _("Money added to or taken from the drawer"),
            "price": _("Changed a price"), "expense": _("Voided an expense"),
            "stock": _("A stock count changed the quantity"), "patient": _("Deleted a patient"),
            "late": _("A file changed after its visit ended")}


def off_kinds(ctx):
    return set(ctx.membership.alerts_off or ()) & set(KEYS)


def _where(ctx):
    """(sql, actions) for the kinds still on."""
    off = off_kinds(ctx)
    actions = [a for key, kind_actions, _l in KINDS if key not in off for a in kind_actions]
    late = "" if "late" in off else " OR details->>'late' = 'true'"
    return _WHERE.format(late=late), actions


def _since():
    return timezone.now() - dt.timedelta(days=DAYS)


def sees(ctx):
    return ctx.membership is not None and ctx.membership.is_owner


def unseen(ctx):
    """How many alerts the owner hasn't opened yet (one count on the index), or 0 for anyone else."""
    if not sees(ctx):
        return 0
    seen = ctx.membership.alerts_seen_at
    since = max(seen, _since()) if seen else _since()
    where, actions = _where(ctx)
    found = sql.row(ctx, f'SELECT count(*) FROM (SELECT 1 FROM "audit_change" WHERE {where} LIMIT 100) a',
                    [ctx.clinic_id, actions, since])
    return found[0]


def items(ctx):
    """The latest alerts, newest first, each with who, when, what and the patient when there is one."""
    where, actions = _where(ctx)
    rows = sql.dicts(ctx, f'SELECT id, action, membership_id, target_id, patient_id, details, at FROM "audit_change" '
                          f"WHERE {where} ORDER BY at DESC, id DESC LIMIT %s",
                     [ctx.clinic_id, actions, _since(), LIMIT])
    if not rows:
        return []
    for r in rows:  # jsonb read as text by the raw cursor
        if isinstance(r["details"], str):
            r["details"] = json.loads(r["details"])
    names = dict(sql.rows(ctx, 'SELECT id, display_name FROM "clinics_membership" WHERE clinic_id = %s', [ctx.clinic_id]))
    invoice_ids = [r["target_id"] for r in rows if r["action"].startswith("invoice.")]
    invoices = {r[0]: (r[1], r[2], r[3]) for r in sql.rows(ctx, """
        SELECT i.id, i.number, i.patient_id, p.full_name FROM "billing_invoice" i
          JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
         WHERE i.clinic_id = %s AND i.id = ANY(%s::bigint[])""", [ctx.clinic_id, invoice_ids])} if invoice_ids else {}
    refunds = [r["target_id"] for r in rows if r["action"] == "payment.refund"]
    payments = {r[0]: (r[1], r[2]) for r in sql.rows(ctx, """
        SELECT y.id, y.patient_id, p.full_name FROM "billing_payment" y
          JOIN "patients_patient" p ON p.clinic_id = y.clinic_id AND p.id = y.patient_id
         WHERE y.clinic_id = %s AND y.id = ANY(%s::bigint[])""", [ctx.clinic_id, refunds])} if refunds else {}
    procedure_ids = [r["target_id"] for r in rows if r["action"] == "procedure.price"]
    procedure_ids += [r["details"].get("before", {}).get("procedure_id") for r in rows if r["action"] == "visit_procedure.remove"]
    procedures = dict(sql.rows(ctx, 'SELECT id, name_ar FROM "clinical_procedure" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                               [ctx.clinic_id, [i for i in procedure_ids if i]])) if procedure_ids else {}
    patient_ids = [r["details"].get("before", {}).get("patient_id") for r in rows if r["action"] == "visit_procedure.remove"]
    patients = dict(sql.rows(ctx, 'SELECT id, full_name FROM "patients_patient" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                             [ctx.clinic_id, [i for i in patient_ids if i]])) if patient_ids else {}
    item_ids = [r["target_id"] for r in rows if r["action"] == "item.count"]
    stock = dict(sql.rows(ctx, 'SELECT id, name FROM "inventory_item" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                          [ctx.clinic_id, item_ids])) if item_ids else {}
    from .file_history import describe

    late = [r for r in rows if r["action"] not in ACTIONS]
    told = {r["id"]: text for r, text in zip(late, describe(ctx, late))}
    late_ids = [r["patient_id"] for r in late if r["patient_id"]]
    late_names = dict(sql.rows(ctx, 'SELECT id, full_name FROM "patients_patient" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                               [ctx.clinic_id, late_ids])) if late_ids else {}
    shift_of = _shifts(ctx, rows[-1]["at"], names)
    seen = ctx.membership.alerts_seen_at
    out = []
    for r in rows:
        d, action = r["details"] or {}, r["action"]
        before, after = d.get("before") or {}, d.get("after") or {}
        item = {"id": r["id"], "at": local_iso(ctx.clinic, r["at"]), "who": names.get(r["membership_id"], _("The system")),
                "new": not seen or r["at"] > seen, "amount": None, "patient": None, "patient_id": None, "what": "",
                "detail": "", "level": "warn", "shift": shift_of(r["at"])}
        if r["id"] in told:  # a file changed after its visit ended
            text = told[r["id"]]
            item["what"] = f"{text['what']} · {_('After the visit ended')}"
            item["detail"], item["amount"] = text["detail"], text["amount"]
            item["patient_id"] = r["patient_id"]
            item["patient"] = late_names.get(r["patient_id"])
        elif action == "visit_procedure.remove":
            item["what"] = _("Removed a recorded procedure")
            item["detail"] = procedures.get(before.get("procedure_id"), "") + (f" · {before['tooth']}" if before.get("tooth") else "")
            item["amount"] = before.get("amount")
            item["patient_id"] = before.get("patient_id")
            item["patient"] = patients.get(item["patient_id"])
        elif action.startswith("invoice."):
            number, item["patient_id"], item["patient"] = invoices.get(r["target_id"], (None, None, None))
            bill = _("Bill %(number)s") % {"number": number} if number else ""
            if action == "invoice.line_remove":
                item["what"] = _("Removed a line from a bill")
                item["detail"] = " · ".join(x for x in (bill, before.get("description", "")) if x)
                item["amount"] = before.get("amount")
            elif action == "invoice.discount":
                item["what"] = _("Gave a discount")
                item["detail"] = bill
                item["amount"] = after.get("discount")
                item["level"] = "info"
            else:
                item["what"] = _("Voided a bill")
                item["detail"] = bill
                item["amount"] = before.get("total")
                item["level"] = "bad"
        elif action == "payment.refund":
            item["what"] = _("Refunded money to a patient")
            item["amount"] = d.get("amount")
            item["patient_id"], item["patient"] = payments.get(r["target_id"], (None, None))
            item["level"] = "bad"
        elif action == "shift.close":
            difference = after.get("difference") or 0
            item["what"] = _("The register was short") if difference < 0 else _("The register was over")
            item["amount"] = abs(difference)
            item["level"] = "bad" if difference < 0 else "info"
        elif action in ("cash.in", "cash.out"):
            # Cash the desk put in or took out that is no patient's payment nor an expense (Saged, 2026-11-24).
            item["what"] = _("Added money to the drawer") if action == "cash.in" else _("Took money out of the drawer")
            item["detail"] = d.get("reason", "")
            item["amount"] = d.get("amount")
            item["level"] = "info" if action == "cash.in" else "warn"
        elif action == "procedure.price":
            item["what"] = _("Changed a price")
            item["detail"] = procedures.get(r["target_id"], "")
            if "price" in before:
                item["amount"] = after.get("price")
                item["before_amount"] = before.get("price")
        elif action == "expense.void":
            item["what"] = _("Voided an expense")
            item["amount"] = before.get("amount")
        elif action in ("patient.hide", "patient.delete"):
            item["what"] = _("Deleted a patient")
            item["detail"] = f"{before.get('full_name', '')} · {_('File')} {before.get('file_no', '')}"
            item["level"] = "bad"
        elif action == "item.count":
            item["what"] = _("A stock count changed the quantity")
            item["detail"] = f"{stock.get(r['target_id'], '')} · {before.get('quantity')} → {after.get('quantity')}"
        out.append(item)
    return out


def _shifts(ctx, oldest, names):
    """In whose register shift each alert happened (Saged, 2026-11-22): who opened it and when, who closed it and when.
    One read of the shifts that overlap the alerts (index shift_list), then a lookup per alert."""
    rows = sql.rows(ctx, 'SELECT opened_at, opened_by_id, closed_at, closed_by_id FROM "finance_shift" '
                         "WHERE clinic_id = %s AND (closed_at IS NULL OR closed_at >= %s) ORDER BY opened_at DESC, id DESC "
                         "LIMIT 500", [ctx.clinic_id, oldest])

    def find(at):
        for opened_at, opened_by, closed_at, closed_by in rows:
            if opened_at <= at and (closed_at is None or at <= closed_at):
                return {"opened_by": names.get(opened_by, _("The system")), "opened_at": local_iso(ctx.clinic, opened_at),
                        "closed_by": names.get(closed_by, _("The system")) if closed_at else None,
                        "closed_at": local_iso(ctx.clinic, closed_at) if closed_at else None}
        return None
    return find


@require_GET
@page()
@clinic_page
def alerts_page(request, ctx):
    if not sees(ctx):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    found = items(ctx)
    from clinics.models import Membership

    now = timezone.now()
    try:  # marking them seen never stops the page (a long write holding the row: they stay new until next time)
        with transaction.atomic(using=ctx.alias):
            Membership.objects.filter(clinic_id=ctx.clinic_id, id=ctx.membership_id).update(alerts_seen_at=now)
        ctx.membership.alerts_seen_at = now  # the side bar on this page counts none
    except OperationalError:
        pass
    off = off_kinds(ctx)
    labels = _labels()
    kinds = [{"key": key, "label": labels[key], "on": key not in off} for key in KEYS]
    return render(request, "web/alerts.html", {"ctx": ctx, "nav_active": "alerts", "items": found, "days": DAYS,
                                               "kinds": kinds, "some_off": bool(off)})


@api("PUT", key=False)
def alert_settings_view(request, ctx, data):
    """PUT {off: [kind, ...]}: the kinds of alerts the owner doesn't want (absolute: a resend changes nothing)."""
    if not sees(ctx):
        raise ApiError("forbidden", _("Only the account's owner can change this."), 403)
    off = data.get("off")
    if not isinstance(off, list) or not all(isinstance(k, str) and k in KEYS for k in off):
        raise ApiError("invalid", "off must list alert kinds", fields={"off": "invalid"})
    off = sorted(set(off))
    from clinics.models import Membership

    before = sorted(off_kinds(ctx))
    Membership.objects.filter(clinic_id=ctx.clinic_id, id=ctx.membership_id).update(alerts_off=off or None)
    if before != off:
        record_change(ctx, "alerts.settings", "membership", ctx.membership_id, before={"off": before},
                      after={"off": off})
    return Result({"off": off})
