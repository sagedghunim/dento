"""Stock endpoints (design 5.4, F2). Views validate, check codes and call one service."""

from core import paging, validate
from core.api import Result, api
from core.errors import ApiError
from core.numbers import parse_int, parse_money

from billing import visibility

from . import services

MAX_QTY = 1_000_000
MAX_LINES = 30


def _item_id(value):
    return validate.path_id(value, services.not_found("item"))


def _replay_item(ctx, stored):
    return {"item": visibility.fields(ctx, services.get_item(ctx, stored["target_id"]), "unit_cost")}


def _cost_needs_money(ctx, data, new=False):
    """The unit cost is money: only holders of money.view set or change it (Saged, 2026-11-16). A new item may say 0."""
    if "unit_cost" in data and not (new and data["unit_cost"] in (None, "", 0, "0")):
        visibility.require(ctx)


def _item_values(data, partial):
    values = {}
    for name, size in services.ITEM_TEXT.items():
        if name in data or (not partial and name == "name"):
            values[name] = validate.text(data, name, size, required=name == "name")
    if "unit" in values and not values["unit"]:
        values["unit"] = "unit"
    if "threshold" in data:
        values["threshold"] = parse_int(data.get("threshold"), "threshold", 0, MAX_QTY)
    if "unit_cost" in data:
        values["unit_cost"] = parse_money(data.get("unit_cost"), "unit_cost")
    if "allow_negative" in data:
        values["allow_negative"] = validate.boolean(data, "allow_negative")
    if "section_id" in data:
        section = data.get("section_id")
        values["section_id"] = validate.ident(section, "section_id") if section is not None else None
    if partial and "is_active" in data:
        values["is_active"] = validate.boolean(data, "is_active")
    return values


def _lines(data):
    """[{item_id, qty}] -> {item_id: qty}; 1 to 30 lines, an item once."""
    raw = data.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise ApiError("invalid", f"lines must be a list of 1 to {MAX_LINES} items", fields={"lines": "list"})
    lines = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ApiError("invalid", "each line needs item_id and qty", fields={"lines": "item"})
        item_id = validate.ident(entry.get("item_id"), "item_id")
        if item_id in lines:
            raise ApiError("invalid", "an item appears twice", fields={"lines": "duplicate"})
        lines[item_id] = parse_int(entry.get("qty"), "qty", 1, MAX_QTY)
    return lines


@api("GET", perm="inventory.use")
def items_view(request, ctx, data):
    """?section=<id> (that section's and the shared items), ?low=1, ?all=1 (inactive too)."""
    section = request.GET.get("section")
    return {"items": services.items(ctx, validate.ident(section, "section") if section else None,
                                    include_inactive=request.GET.get("all") == "1",
                                    low_only=request.GET.get("low") == "1")}


@api("POST", perm="inventory.manage", replay=_replay_item)
def create_item_view(request, ctx, data):
    """POST {name, category?, sku?, unit?, threshold?, unit_cost?, notes?, section_id?, allow_negative?, quantity?}."""
    quantity = parse_int(data.get("quantity", 0), "quantity", 0, MAX_QTY)
    _cost_needs_money(ctx, data, new=True)
    item = services.create_item(ctx, _item_values(data, partial=False), quantity)
    return Result({"item": visibility.fields(ctx, item, "unit_cost")}, status=201, target_type="item", target_id=item["id"])


@api("PATCH", perm="inventory.manage", replay=_replay_item)
def update_item_view(request, ctx, data, item_id):
    item_id = _item_id(item_id)
    if "quantity" in data:
        raise ApiError("invalid", "the quantity changes through a purchase, a use or a count",
                       fields={"quantity": "not_allowed"})
    _cost_needs_money(ctx, data)
    item = services.update_item(ctx, item_id, _item_values(data, partial=True))
    return Result({"item": visibility.fields(ctx, item, "unit_cost")}, target_type="item", target_id=item_id)


@api("GET", perm="inventory.use")
def movements_view(request, ctx, data, item_id):
    item_id = _item_id(item_id)
    size = paging.limit(request)
    rows = services.movements(ctx, item_id, size, paging.time_cursor(request))
    return paging.page(rows, size, lambda m: paging.encode_time(m["at"], m["id"]))


def _replay_items(ctx, stored):
    return {"replayed": True, "target": {"type": stored["target_type"], "id": stored["target_id"]}}


@api("POST", perm="inventory.use", replay=_replay_items)
def use_view(request, ctx, data):
    """POST {lines: [{item_id, qty}], patient_id?, appointment_id?, reason?}: all or nothing (409 out_of_stock)."""
    patient = data.get("patient_id")
    appointment = data.get("appointment_id")
    taken, ids = services.use(ctx, _lines(data), validate.ident(patient, "patient_id") if patient is not None else None,
                              validate.ident(appointment, "appointment_id") if appointment is not None else None,
                              validate.text(data, "reason", 200, required=False))
    items = [services.get_item(ctx, i) for i in sorted(taken)] if len(taken) <= 5 else []
    return Result({"movement_ids": ids, "items": items}, status=201, target_type="movement",
                  target_id=ids[0] if ids else None)


@api("POST", perm="inventory.manage", replay=_replay_item)
def purchase_view(request, ctx, data, item_id):
    """POST {qty, unit_price, record_expense?, from_shift?, note?}: stock in, the average cost and an expense.
    A price and an expense are money: money.view too (Saged, 2026-11-16)."""
    visibility.require(ctx)
    item_id = _item_id(item_id)
    item, expense = services.purchase(ctx, item_id, parse_int(data.get("qty"), "qty", 1, MAX_QTY),
                                      parse_money(data.get("unit_price"), "unit_price"),
                                      record_expense=validate.boolean(data, "record_expense", True),
                                      from_shift=validate.boolean(data, "from_shift", False),
                                      note=validate.text(data, "note", 200, required=False))
    return Result({"item": item, "expense_id": expense["id"] if expense else None}, status=201, target_type="item",
                  target_id=item_id)


@api("POST", perm="inventory.manage", replay=_replay_item)
def count_view(request, ctx, data, item_id):
    """POST {counted, expected, reason?}: a stock count against the quantity the person saw (409 stale)."""
    item_id = _item_id(item_id)
    item = services.adjust(ctx, item_id, parse_int(data.get("counted"), "counted", -MAX_QTY, MAX_QTY),
                           parse_int(data.get("expected"), "expected", -MAX_QTY, MAX_QTY),
                           validate.text(data, "reason", 200, required=False))
    return Result({"item": visibility.fields(ctx, item, "unit_cost")}, target_type="item", target_id=item_id)


@api("POST", perm="inventory.manage", replay=_replay_item)
def waste_view(request, ctx, data, item_id):
    """POST {qty, reason}: expired or broken stock out."""
    item_id = _item_id(item_id)
    services.waste(ctx, item_id, parse_int(data.get("qty"), "qty", 1, MAX_QTY), validate.text(data, "reason", 200))
    return Result({"item": visibility.fields(ctx, services.get_item(ctx, item_id), "unit_cost")}, status=201, target_type="item", target_id=item_id)


def _procedure_id(value):
    return validate.path_id(value, services.not_found("procedure"))


@api("GET", perm="inventory.use")
def materials_view(request, ctx, data, procedure_id):
    return {"items": services.materials(ctx, _procedure_id(procedure_id))}


@api("PUT", perm="inventory.manage")
def set_materials_view(request, ctx, data, procedure_id):
    """PUT {lines: [{item_id, qty}]} (an empty list clears them): what recording the procedure uses (F2)."""
    procedure_id = _procedure_id(procedure_id)
    lines = {} if data.get("lines") == [] else _lines(data)
    if any(q > 10000 for q in lines.values()):
        raise ApiError("invalid", "qty must be between 1 and 10000", fields={"qty": "range"})
    return Result({"items": services.set_materials(ctx, procedure_id, lines)}, target_type="procedure",
                  target_id=procedure_id)


@api("POST", perm="inventory.manage")
def defaults_view(request, ctx, data):
    """Add the default items (and, for dental, what each procedure uses) that the place doesn't have yet."""
    services.add_defaults(ctx)
    return {"items": services.items(ctx)}
