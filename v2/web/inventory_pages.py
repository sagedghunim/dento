"""Inventory (design F2): items and their stock, purchases, counts, waste, use, each item's history and what each
procedure uses. Actions go through /api/v1/inventory/ from inventory.js; this file only renders.

Someone with `inventory.use` sees the list, records use and reads an item's history. `inventory.manage` adds the
rest. The server checks every action again.
"""

import json

from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.dates import WEEKDAYS
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core import paging
from core.api import page
from core.days import zone
from core.i18n import pick
from inventory import catalog, orders

from . import format as fmt
from . import places
from .http import clinic_page, fragment

CODES = ("inventory.use", "inventory.manage", "inventory.order")
MOVEMENTS_PAGE = 15


def _perms(ctx):
    return {"use": ctx.can("inventory.use"), "manage": ctx.can("inventory.manage"),
            # Purchase orders and suppliers (design H1); paying a supplier is an expense too.
            "order": ctx.can("inventory.order"), "pay": ctx.can("expenses.manage"), "drawer": ctx.can("cash.collect"),
            "owner": ctx.membership.is_owner,
            "view_patients": ctx.can("patients.view"),
            # Costs and purchases are money (Saged, 2026-11-16).
            "money": ctx.can("money.view")}


def _items_context(ctx, removed=False):
    """The item cards, the low-stock line and each card's values for the dialogs (escaped in the attribute)."""
    from inventory import services

    sections = places.sections(ctx)
    names = {s["id"]: s["name"] for s in sections}
    items = services.items(ctx, include_inactive=removed)
    if removed:
        items = [i for i in items if not i["is_active"]]
    for item in items:
        item["section_name"] = names.get(item["section_id"], "") if len(sections) > 1 else ""
        item["json"] = json.dumps({k: item[k] for k in (
            "id", "name", "name_ar", "name_en", "category", "sku", "unit", "quantity", "threshold", "unit_cost", "notes",
            "allow_negative", "section_id", "is_active", "supplier_id", "backup_supplier_id", "max_qty")}, ensure_ascii=False)
        # The bar shows the stock against four times the minimum, as the demo does (never wider than full).
        top = max(item["threshold"] * 4, item["quantity"], 1)
        item["bar_max"] = top
        item["bar_value"] = max(item["quantity"], 0)
    low = [i for i in items if i["is_active"] and i["low"]]
    return {"items": items, "low": low, "removed": removed, "perms": _perms(ctx)}


def _materials(ctx):
    """Each procedure with what recording it takes from stock (two queries, for the managers only)."""
    from clinical import selectors
    from inventory.models import Item, Material

    sections = places.sections(ctx)
    names = {s["id"]: s["name"] for s in sections}
    items = {row[0]: row[1:] for row in Item.objects.filter(clinic_id=ctx.clinic_id)
             .values_list("id", "name", "name_en", "unit", "is_active")}
    lines = {}
    for procedure_id, item_id, qty in (Material.objects.filter(clinic_id=ctx.clinic_id)
                                       .order_by("procedure_id", "item_id").values_list("procedure_id", "item_id", "qty")):
        name, name_en, unit, active = items.get(item_id, ("", "", "", False))
        lines.setdefault(procedure_id, []).append({"item_id": item_id, "name": pick(name, name_en), "unit": unit,
                                                   "qty": qty, "is_active": active})
    procedures = []
    for p in selectors.procedures(ctx)["items"]:
        procedures.append({"id": p["id"], "name": p["name"], "lines": lines.get(p["id"], []),
                           "section_name": names.get(p["section_id"], "") if len(sections) > 1 else "",
                           "json": json.dumps({"id": p["id"], "name": p["name"], "lines": [
                               {"item_id": line["item_id"], "qty": line["qty"]} for line in lines.get(p["id"], [])]},
                               ensure_ascii=False)})
    return procedures


@require_GET
@ensure_csrf_cookie
@page()
@clinic_page
def inventory_page(request, ctx):
    if not any(ctx.can(code) for code in CODES):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    perms = _perms(ctx)
    sections = places.sections(ctx)
    context = _items_context(ctx)
    # Ready items to pick from when adding one (Saged, 2026-11-25), without the ones the clinic already has.
    have = [name for i in context["items"] for name in (i["name"], i.get("name_ar") or "")]
    return render(request, "web/inventory.html", {
        "ctx": ctx, "nav_active": "inventory", **context,
        "ready_items": [{**r, "price_text": fmt.money_plain(r["cost"]) if r["price"] else ""}
                        for r in catalog.ready_items(have)] if perms["manage"] else [],
        "sections": sections if len(sections) > 1 else [],
        "procedures": _materials(ctx) if perms["manage"] else [],
        "suppliers": orders.suppliers(ctx, include_inactive=True) if perms["manage"] or perms["order"] else [],
        # The supplier's usual days, Saturday first as the week runs in Egypt (Python's weekday numbers).
        "supplier_days": [(d, WEEKDAYS[d]) for d in (5, 6, 0, 1, 2, 3, 4)],
        # The desk opens on its orders; the owner on the items (design H1 flow 2).
        "orders_first": perms["order"] and not perms["owner"],
        "show_items": perms["use"] or perms["manage"],
        # For the orders' pick lists and their first view (orders.js); prices only with money.view.
        "order_data": {
            "items": [{k: i[k] for k in ("id", "name", "unit", "quantity", "threshold", "supplier_id", "backup_supplier_id")}
                      for i in context["items"] if i["is_active"]],
            "orders": orders.orders(ctx),
            "needs": orders.needs(ctx),  # the "need a supplier" box, shown at once (design H1 backup)
            "perms": {k: perms[k] for k in ("manage", "order", "money", "pay", "drawer", "owner")},
            # The days on the orders are the clinic's, not the browser's.
            "zone": str(zone(ctx.clinic)),
        } if perms["order"] else None,
    })


@fragment(any_of=CODES)
def inventory_items(request, ctx):
    """GET /inventory/items[?removed=1]: the cards again after a save (removed items for the managers)."""
    removed = request.GET.get("removed") == "1" and ctx.can("inventory.manage")
    return HttpResponse(render_to_string("web/_inventory_items.html", _items_context(ctx, removed), request=request))


@fragment(any_of=CODES)
def item_movements(request, ctx, item_id):
    """GET /inventory/items/<id>/movements[?after=]: an item's history, newest first, in the clinic's time zone."""
    from clinics.models import Membership
    from inventory import services
    from patients.models import Patient

    if item_id > 2**63 - 1:
        raise services.not_found("item")
    rows = services.movements(ctx, item_id, MOVEMENTS_PAGE, paging.time_cursor(request))
    found = paging.page(rows, MOVEMENTS_PAGE, lambda m: paging.encode_time(m["at"], m["id"]))
    people = dict(Membership.objects.filter(clinic_id=ctx.clinic_id, id__in={m["created_by_id"] for m in found["items"]})
                  .values_list("id", "display_name"))
    patient_ids = {m["patient_id"] for m in found["items"] if m["patient_id"]}
    patients = dict(Patient.objects.filter(clinic_id=ctx.clinic_id, id__in=patient_ids)
                    .values_list("id", "full_name")) if patient_ids and ctx.can("patients.view") else {}
    local = zone(ctx.clinic)
    items = []
    for m in found["items"]:
        at = m["at"].astimezone(local)
        items.append({**m, "day": fmt.day(at.date(), with_weekday=False), "time": fmt.clock(at),
                      "by": people.get(m["created_by_id"], ""), "patient": patients.get(m["patient_id"], "")})
    response = HttpResponse(render_to_string("web/_movement_rows.html", {"items": items}, request=request))
    response["X-Next"] = found["next"] or ""
    response["X-Count"] = str(len(items))
    return response

