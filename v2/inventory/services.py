"""Stock, movements and procedure materials (design 5.4, F2). Inside the request's shard transaction.

Lock order (design 5.0): request key -> [appointment -> invoice] -> [open shift, FOR SHARE] -> inventory items
(several in one statement by id) -> expenses -> clinic-days. Stock changes only through a conditional UPDATE: a use
needs `quantity >= qty` unless the item allows negative stock; a count needs the quantity the person saw.
Today's version moves only when an item crosses its minimum, either way, so the banner stays right.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.days import business_day
from core.errors import ApiError
from summaries.services import Deltas

ITEM_COLUMNS = ("id, section_id, name, name_en, category, sku, unit, quantity, threshold, unit_cost, notes, allow_negative, "
                "is_active")
ITEM_TEXT = {"name": 120, "name_en": 120, "category": 40, "sku": 40, "unit": 20, "notes": 500}
LOW_STOCK_LIMIT = 20


def _cols(columns):
    return [c.strip() for c in columns.split(",")]


def item_row(values):
    from core.i18n import pick

    row = dict(zip(_cols(ITEM_COLUMNS), values))
    row["name_ar"] = row["name"]
    row["name"] = pick(row["name_ar"], row["name_en"])  # design G3
    row["low"] = row["quantity"] <= row["threshold"]
    return row


def not_found(what):
    messages = {"item": _("This item wasn't found."), "procedure": _("This procedure wasn't found.")}
    return ApiError("not_found", messages[what], 404)


def _duplicate():
    return ApiError("duplicate", _("This item is already in the list."), 409, fields={"name": "duplicate"})


def _savepoint(ctx, statement, params):
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if name == "item_active_name_uniq":
            raise _duplicate()
        if name == "item_section_fk":
            raise ApiError("not_found", _("This section wasn't found."), 404)
        raise


def _crossed(before, after, threshold):
    return (before <= threshold) != (after <= threshold)


def _movement(ctx, item_id, kind, qty, balance, unit_cost, *, patient_id=None, appointment_id=None,
              visit_procedure_id=None, expense_id=None, reason="", at=None):
    return (ctx.clinic_id, item_id, kind, qty, balance, unit_cost, patient_id, appointment_id, visit_procedure_id,
            expense_id, reason[:200], ctx.membership_id, at or timezone.now())


def _insert_movements(ctx, movements):
    """All of a flow's movements in one statement."""
    if not movements:
        return []
    values = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(movements))
    return [r[0] for r in sql.rows(ctx, f"""
        INSERT INTO "inventory_movement" (clinic_id, item_id, kind, qty, balance_after, unit_cost, patient_id,
                                          appointment_id, visit_procedure_id, expense_id, reason, created_by_id, at)
        VALUES {values} RETURNING id""", [p for m in movements for p in m])]


# --- Items -----------------------------------------------------------------------------------------------------


def items(ctx, section_id=None, include_inactive=False, low_only=False):
    """A place's items (a few hundred at most), by name. `section_id` keeps that section's and the shared ones."""
    where, params = "", []
    if not include_inactive:
        where += " AND is_active"
    if section_id is not None:
        where += " AND (section_id = %s OR section_id IS NULL)"
        params.append(section_id)
    if low_only:
        where += " AND quantity <= threshold"
    from billing import visibility  # the cost is money (Saged, 2026-11-16)

    return visibility.rows(ctx, [item_row(r) for r in sql.rows(
        ctx, f'SELECT {ITEM_COLUMNS} FROM "inventory_item" WHERE clinic_id = %s{where} ORDER BY name, id',
        [ctx.clinic_id, *params])], "unit_cost")


def low_stock(ctx, limit=LOW_STOCK_LIMIT):
    """Today's banner (F2): active items at or below their minimum. One statement over the place's items."""
    from core.i18n import pick

    return [{"id": r[0], "name": pick(r[1], r[5]), "unit": r[2], "quantity": r[3], "threshold": r[4]} for r in sql.rows(ctx, """
        SELECT id, name, unit, quantity, threshold, name_en FROM "inventory_item"
         WHERE clinic_id = %s AND is_active AND quantity <= threshold ORDER BY quantity - threshold, name LIMIT %s""",
                                                                                         [ctx.clinic_id, limit])]


def get_item(ctx, item_id):
    found = sql.row(ctx, f'SELECT {ITEM_COLUMNS} FROM "inventory_item" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, item_id])
    if found is None:
        raise not_found("item")
    return item_row(found)


def create_item(ctx, values, quantity=0):
    """A new item; a starting quantity is recorded as a count (an `adjust` movement)."""
    found = _savepoint(ctx, f"""
        INSERT INTO "inventory_item" (clinic_id, section_id, name, name_en, category, sku, unit, quantity, threshold,
                                      unit_cost, notes, allow_negative, is_active, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s) RETURNING {ITEM_COLUMNS}""", [
        ctx.clinic_id, values.get("section_id"), values["name"], values.get("name_en", ""), values.get("category", ""), values.get("sku", ""),
        values.get("unit") or "unit", quantity, values.get("threshold", 0), values.get("unit_cost", 0),
        values.get("notes", ""), values.get("allow_negative", False), timezone.now()])
    item = item_row(found)
    if quantity:
        _insert_movements(ctx, [_movement(ctx, item["id"], "adjust", quantity, quantity, item["unit_cost"],
                                          reason=_("Opening stock"))])
    if item["low"]:
        Deltas().clinic_day(business_day(ctx.clinic)).write_clinics(ctx)
    return item


def update_item(ctx, item_id, values):
    """Absolute values for the item's details. The quantity changes only through movements; a cost change is audited."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    before = sql.row(ctx, 'SELECT quantity, threshold, unit_cost, is_active FROM "inventory_item" '
                          "WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE", [ctx.clinic_id, item_id])
    if before is None:
        raise not_found("item")
    quantity, threshold, unit_cost, active = before
    if values.get("allow_negative") is False and quantity < 0:
        raise ApiError("negative_stock", _("Count the stock back to zero or more first."), 409)
    assignments = ", ".join(f"{name} = %s" for name in values)
    item = item_row(_savepoint(ctx, f'UPDATE "inventory_item" SET {assignments} WHERE clinic_id = %s AND id = %s '
                                    f"RETURNING {ITEM_COLUMNS}", [*values.values(), ctx.clinic_id, item_id]))
    if "unit_cost" in values and values["unit_cost"] != unit_cost:
        record_change(ctx, "item.cost", "item", item_id, before={"unit_cost": unit_cost},
                      after={"unit_cost": values["unit_cost"]})
    was_low = active and quantity <= threshold
    if was_low != (item["is_active"] and item["low"]):
        Deltas().clinic_day(business_day(ctx.clinic)).write_clinics(ctx)
    return item


def _lock_items(ctx, item_ids):
    """The items in one statement by id (design 5.0). Missing or inactive ones are 404 / 400."""
    rows = {r[0]: dict(zip(("id", "name", "unit", "quantity", "threshold", "unit_cost", "allow_negative",
                            "is_active"), r)) for r in sql.rows(ctx, """
        SELECT id, name, unit, quantity, threshold, unit_cost, allow_negative, is_active FROM "inventory_item"
         WHERE clinic_id = %s AND id = ANY(%s::bigint[]) ORDER BY id FOR NO KEY UPDATE""",
                                                                 [ctx.clinic_id, sorted(item_ids)])}
    for item_id in item_ids:
        if item_id not in rows:
            raise not_found("item")
        if not rows[item_id]["is_active"]:
            raise ApiError("invalid", _("This item isn't used any more."), fields={"item_id": "inactive"})
    return rows


def _out_of_stock(short):
    return ApiError("out_of_stock", _("Not enough stock."), 409,
                    extra={"items": [{"id": i["id"], "name": i["name"], "quantity": i["quantity"], "needed": q}
                                     for i, q in short]})


def take(ctx, lines, kind="use", *, patient_id=None, appointment_id=None, visit_procedure_id=None, reason="",
         deltas=None):
    """Take stock out (use or waste): lock the items in one statement, then one conditional UPDATE per item.

    `lines` is {item_id: qty}. Any item short (and not allowed negative) refuses the whole flow with 409
    out_of_stock listing them. Returns ({item_id: (qty, unit_cost)}, movement ids). With `deltas` the caller
    writes the clinic-day; otherwise this does, and only when an item crossed its minimum.
    """
    if not lines:
        return {}, []
    locked = _lock_items(ctx, list(lines))
    short = [(locked[i], q) for i, q in sorted(lines.items())
             if not locked[i]["allow_negative"] and locked[i]["quantity"] < q]
    if short:
        raise _out_of_stock(short)
    crossed, taken, movements = False, {}, []
    now = timezone.now()
    for item_id in sorted(lines):
        qty = lines[item_id]
        found = sql.row(ctx, 'UPDATE "inventory_item" SET quantity = quantity - %s WHERE clinic_id = %s AND id = %s '
                             "AND (allow_negative OR quantity >= %s) RETURNING quantity, unit_cost, threshold",
                        [qty, ctx.clinic_id, item_id, qty])
        if found is None:  # unreachable under the lock; the condition still guards
            raise _out_of_stock([(locked[item_id], qty)])
        balance, unit_cost, threshold = found
        crossed = crossed or _crossed(balance + qty, balance, threshold)
        taken[item_id] = (qty, unit_cost)
        movements.append(_movement(ctx, item_id, kind, -qty, balance, unit_cost, patient_id=patient_id,
                                   appointment_id=appointment_id, visit_procedure_id=visit_procedure_id, reason=reason,
                                   at=now))
    ids = _insert_movements(ctx, movements)
    if crossed:
        if deltas is not None:
            deltas.clinic_day(business_day(ctx.clinic))
        else:
            Deltas().clinic_day(business_day(ctx.clinic)).write_clinics(ctx)
    return taken, ids


def use(ctx, lines, patient_id=None, appointment_id=None, reason=""):
    """POST inventory/uses (design 5.4). The patient and visit, when given, must be this place's."""
    if appointment_id is not None:
        found = sql.row(ctx, 'SELECT patient_id FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s',
                        [ctx.clinic_id, appointment_id])
        if found is None:
            raise ApiError("not_found", _("This appointment wasn't found."), 404)
        patient_id = patient_id or found[0]
        if patient_id != found[0]:
            raise ApiError("invalid", _("This visit belongs to another patient."), fields={"patient_id": "mismatch"})
    elif patient_id is not None and sql.row(ctx, 'SELECT 1 FROM "patients_patient" WHERE clinic_id = %s AND id = %s',
                                            [ctx.clinic_id, patient_id]) is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    return take(ctx, lines, "use", patient_id=patient_id, appointment_id=appointment_id, reason=reason)


def waste(ctx, item_id, qty, reason):
    return take(ctx, {item_id: qty}, "waste", reason=reason)


def average_cost(quantity, unit_cost, qty, price):
    """The weighted average after buying `qty` at `price` each, rounded half up; stock at or below 0 restarts it."""
    if quantity <= 0:
        return price
    return (quantity * unit_cost + qty * price + (quantity + qty) // 2) // (quantity + qty)


def purchase(ctx, item_id, qty, unit_price, *, record_expense=True, from_shift=False, note=""):
    """Stock in at a price (design 5.4): quantity up, the average cost moved, an expense (purchase = true) in the
    materials category, a `purchase` movement, and clinic_day.expenses. Lock order: [shift] -> item -> expense.
    """
    from finance import services as finance

    shift_needed = record_expense and from_shift
    if shift_needed:
        finance.shift_for(ctx, "cash")  # the shift before the item (design 5.0, 8a before 9)
    locked = _lock_items(ctx, [item_id])[item_id]
    new_cost = average_cost(locked["quantity"], locked["unit_cost"], qty, unit_price)
    balance, threshold = sql.row(ctx, 'UPDATE "inventory_item" SET quantity = quantity + %s, unit_cost = %s '
                                      "WHERE clinic_id = %s AND id = %s RETURNING quantity, threshold",
                                 [qty, new_cost, ctx.clinic_id, item_id])
    deltas = Deltas()
    expense = None
    today = business_day(ctx.clinic)
    if record_expense and qty * unit_price > 0:
        expense = finance.add_expense(ctx, finance.category_by_code(ctx, "materials"), qty * unit_price, today,
                                      (note or f"{locked['name']} × {qty}")[:200], from_shift, purchase=True,
                                      deltas=deltas)
    _insert_movements(ctx, [_movement(ctx, item_id, "purchase", qty, balance, new_cost,
                                      expense_id=expense["id"] if expense else None, reason=note)])
    if _crossed(balance - qty, balance, threshold):
        deltas.clinic_day(today)
    deltas.write_clinics(ctx)
    return get_item(ctx, item_id), expense


def adjust(ctx, item_id, counted, expected, reason):
    """A stock count (design 5.4): `quantity = counted` only where it still is `expected`. Audited."""
    found = sql.row(ctx, 'UPDATE "inventory_item" SET quantity = %s WHERE clinic_id = %s AND id = %s AND quantity = %s '
                         "AND (allow_negative OR %s >= 0) RETURNING unit_cost, threshold",
                    [counted, ctx.clinic_id, item_id, expected, counted])
    if found is None:
        current = sql.row(ctx, 'SELECT quantity FROM "inventory_item" WHERE clinic_id = %s AND id = %s',
                          [ctx.clinic_id, item_id])
        if current is None:
            raise not_found("item")
        if current[0] == counted:
            return get_item(ctx, item_id)  # already so: a harmless repeat
        if current[0] != expected:
            raise ApiError("stale", _("The quantity changed meanwhile. Reload and try again."), 409,
                           fields={"quantity": current[0]})
        raise ApiError("invalid", _("The quantity can't be below zero."), fields={"counted": "range"})
    unit_cost, threshold = found
    if counted != expected:
        _insert_movements(ctx, [_movement(ctx, item_id, "adjust", counted - expected, counted, unit_cost, reason=reason)])
        record_change(ctx, "item.count", "item", item_id, before={"quantity": expected}, after={"quantity": counted},
                      reason=reason)
        if _crossed(expected, counted, threshold):
            Deltas().clinic_day(business_day(ctx.clinic)).write_clinics(ctx)
    return get_item(ctx, item_id)


def movements(ctx, item_id, size, after):
    """An item's ledger, newest first, keyset on (at, id)."""
    get_item(ctx, item_id)
    where, params = "", []
    if after is not None:
        where, params = " AND (at, id) < (%s, %s)", list(after)
    from billing import visibility

    return visibility.rows(ctx, sql.dicts(ctx, f"""
        SELECT id, kind, qty, balance_after, unit_cost, patient_id, appointment_id, visit_procedure_id, expense_id, reason,
               created_by_id, at FROM "inventory_movement"
         WHERE clinic_id = %s AND item_id = %s{where} ORDER BY at DESC, id DESC LIMIT %s""",
                     [ctx.clinic_id, item_id, *params, size + 1]), "unit_cost")


# --- Materials (F2) --------------------------------------------------------------------------------------------


def materials(ctx, procedure_id):
    if sql.row(ctx, 'SELECT 1 FROM "clinical_procedure" WHERE clinic_id = %s AND id = %s',
               [ctx.clinic_id, procedure_id]) is None:
        raise not_found("procedure")
    from billing import visibility

    return visibility.rows(ctx, sql.dicts(ctx, """
        SELECT m.item_id, i.name, i.unit, m.qty, i.unit_cost, i.is_active FROM "inventory_material" m
          JOIN "inventory_item" i ON i.clinic_id = m.clinic_id AND i.id = m.item_id
         WHERE m.clinic_id = %s AND m.procedure_id = %s ORDER BY i.name, m.item_id""", [ctx.clinic_id, procedure_id]),
                           "unit_cost")


def set_materials(ctx, procedure_id, lines):
    """Replace a procedure's materials (absolute). Lock order: the procedure row, then its material rows."""
    if sql.row(ctx, 'SELECT 1 FROM "clinical_procedure" WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE',
               [ctx.clinic_id, procedure_id]) is None:
        raise not_found("procedure")
    if lines:
        found = {r[0] for r in sql.rows(ctx, 'SELECT id FROM "inventory_item" WHERE clinic_id = %s AND is_active '
                                             "AND id = ANY(%s::bigint[])", [ctx.clinic_id, list(lines)])}
        missing = sorted(set(lines) - found)
        if missing:
            raise ApiError("invalid", _("This item isn't used any more."), fields={"items": "unknown"},
                           extra={"item_ids": missing})
    sql.run(ctx, 'DELETE FROM "inventory_material" WHERE clinic_id = %s AND procedure_id = %s',
            [ctx.clinic_id, procedure_id])
    if lines:
        values = ", ".join(["(%s, %s, %s, %s)"] * len(lines))
        sql.run(ctx, f'INSERT INTO "inventory_material" (clinic_id, procedure_id, item_id, qty) VALUES {values}',
                [p for item_id in sorted(lines) for p in (ctx.clinic_id, procedure_id, item_id, lines[item_id])])
    return materials(ctx, procedure_id)


def return_materials(ctx, visit_procedure_id, *, patient_id, appointment_id, deltas):
    """Removing a procedure recorded by mistake (Saged, 2026-11-17): what its recording took goes back to stock.

    Each item gets back what was taken for this record, net of anything already returned, in a `return` movement
    at the cost it left at; the item's average cost takes it in at that cost (unchanged when nothing was bought
    since). History stays: nothing is deleted. Items are locked in one statement by id (lock order 9); an item
    deactivated since still takes its stock back. Returns {item_id: qty returned}.
    """
    taken = sql.rows(ctx, """
        SELECT item_id, -sum(qty), sum(-qty * unit_cost) FILTER (WHERE kind = 'use'), -sum(qty) FILTER (WHERE kind = 'use')
          FROM "inventory_movement"
         WHERE clinic_id = %s AND visit_procedure_id = %s AND kind IN ('use', 'return')
         GROUP BY item_id HAVING sum(qty) < 0 ORDER BY item_id""", [ctx.clinic_id, visit_procedure_id])
    if not taken:
        return {}
    sql.rows(ctx, 'SELECT id FROM "inventory_item" WHERE clinic_id = %s AND id = ANY(%s::bigint[]) ORDER BY id '
                  "FOR NO KEY UPDATE", [ctx.clinic_id, [r[0] for r in taken]])
    crossed, movements, returned = False, [], {}
    now = timezone.now()
    for item_id, qty, cost_sum, used in taken:
        cost = (cost_sum + used // 2) // used  # the cost it left at (one use movement per record: exact)
        found = sql.row(ctx, """
            UPDATE "inventory_item"
               SET quantity = quantity + %(qty)s,
                   unit_cost = CASE WHEN quantity <= 0 THEN %(cost)s
                                    ELSE (quantity * unit_cost + %(qty)s * %(cost)s + (quantity + %(qty)s) / 2)
                                         / (quantity + %(qty)s) END
             WHERE clinic_id = %(clinic)s AND id = %(item)s RETURNING quantity, threshold""",
                        {"qty": qty, "cost": cost, "clinic": ctx.clinic_id, "item": item_id})
        if found is None:
            continue  # the item row is gone (never deleted today); its history stays as it is
        balance, threshold = found
        crossed = crossed or _crossed(balance - qty, balance, threshold)
        returned[item_id] = qty
        movements.append(_movement(ctx, item_id, "return", qty, balance, cost, patient_id=patient_id,
                                   appointment_id=appointment_id, visit_procedure_id=visit_procedure_id, at=now))
    _insert_movements(ctx, movements)
    if crossed:
        deltas.clinic_day(business_day(ctx.clinic))
    return returned


def add_defaults(ctx):
    """POST inventory/defaults: the default items and dental materials the place doesn't have yet (F2).

    Not done at sign-up: a material at zero stock would refuse every recording of its procedure (409
    out_of_stock) in a place that hasn't started counting its stock. The place opts in when it starts.
    """
    from clinics.models import Section

    seed_place(ctx.clinic_id, list(Section.objects.filter(clinic_id=ctx.clinic_id, is_active=True)),
               ctx.clinic.settings.get("print_language", "ar"))
    Deltas().clinic_day(business_day(ctx.clinic)).write_clinics(ctx)  # new items start low: Today's banner


# --- Defaults per specialty -------------------------------------------------------------------------------------

# (key, Arabic, English, category, unit, minimum, cost in pounds). Shared items have no section.
SHARED_ITEMS = [
    ("gloves", "جوانتي (زوج)", "Gloves (pair)", "consumables", "pair", 50, 3),
    ("gauze", "شاش معقم (عبوة)", "Sterile gauze (pack)", "consumables", "pack", 20, 5),
    ("mask", "ماسك طبي", "Face mask", "consumables", "piece", 50, 2),
]
DENTAL_ITEMS = [
    ("anesthetic", "بنج (كارتريدج)", "Local anaesthetic (cartridge)", "anaesthesia", "cartridge", 20, 15),
    ("needle", "سن بنج", "Dental needle", "anaesthesia", "piece", 30, 2),
    ("composite", "كومبوزيت (كبسولة)", "Composite (compule)", "restorative", "compule", 10, 40),
    ("glass_ionomer", "جلاس أيونومر (كبسولة)", "Glass ionomer (capsule)", "restorative", "capsule", 10, 35),
    ("rotary_file", "مبرد روتاري", "Rotary file", "endodontics", "piece", 6, 150),
    ("gutta_percha", "جوتا بيركا (قمع)", "Gutta-percha point", "endodontics", "point", 30, 3),
    ("fibre_post", "بوست فايبر", "Fibre post", "restorative", "piece", 5, 120),
    ("suture", "خيط جراحي", "Suture", "surgery", "pack", 10, 40),
]
# Procedure (by English name) -> [(item key, qty)].
DENTAL_MATERIALS = {
    "Composite filling, one surface": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("composite", 1)],
    "Composite filling, two surfaces": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("composite", 2)],
    "Composite filling, three or more surfaces": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("composite", 3)],
    "Glass ionomer filling": [("gloves", 1), ("glass_ionomer", 1)],
    "Root canal treatment, one canal": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("rotary_file", 1),
                                        ("gutta_percha", 1)],
    "Root canal treatment, two canals": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("rotary_file", 1),
                                         ("gutta_percha", 2)],
    "Root canal treatment, molar": [("anesthetic", 2), ("needle", 1), ("gloves", 1), ("rotary_file", 2),
                                    ("gutta_percha", 3)],
    "Simple extraction": [("anesthetic", 1), ("needle", 1), ("gloves", 1), ("gauze", 1)],
    "Fibre post and core": [("gloves", 1), ("fibre_post", 1), ("composite", 1)],
    "Scaling and polishing": [("gloves", 1)],
}


def seed_place(clinic_id, sections, language="ar"):
    """The default stock items and dental materials a place doesn't have yet (by name). Never edits its rows.

    Runs inside the clinic's transaction after the sections' procedures are copied. Items start at zero
    stock with allow_negative on, so an uncounted material never refuses a recording (409 out_of_stock); the
    place counts what it has and turns allow_negative off per item (Appendix F). `sections` are the place's
    section rows.
    """
    from clinical.models import Procedure

    from .models import Item, Material

    def name(arabic, english):
        return arabic if language == "ar" else english

    have = {n: i for n, i in Item.objects.filter(clinic_id=clinic_id, is_active=True).values_list("name", "id")}
    keys = {}
    wanted = [(None, row) for row in SHARED_ITEMS]
    dental = [s for s in sections if s.specialty_code == "dental"]
    if dental:
        wanted += [(dental[0].id, row) for row in DENTAL_ITEMS]
    for section_id, (key, arabic, english, category, unit, minimum, pounds) in wanted:
        label = name(arabic, english)
        if label not in have:
            have[label] = Item.objects.create(clinic_id=clinic_id, section_id=section_id, name=label, name_en=english,
                                              category=category,
                                              unit=unit, threshold=minimum, unit_cost=pounds * 100,
                                              allow_negative=True).id
        keys[key] = have[label]
    for section in dental:
        procedures = dict(Procedure.objects.filter(clinic_id=clinic_id, section_id=section.id, is_active=True)
                          .values_list("name_en", "id"))
        existing = set(Material.objects.filter(clinic_id=clinic_id, procedure_id__in=procedures.values())
                       .values_list("procedure_id", flat=True))
        Material.objects.bulk_create([
            Material(clinic_id=clinic_id, procedure_id=procedures[proc], item_id=keys[key], qty=qty)
            for proc, lines in DENTAL_MATERIALS.items() if proc in procedures and procedures[proc] not in existing
            for key, qty in lines])


def visit_uses(ctx, appointment_id):
    """What this visit already took from stock (net of what came back), by item."""
    from core.i18n import pick

    return [{"item_id": r[0], "name": pick(r[1], r[2]), "unit": r[3], "qty": r[4]} for r in sql.rows(ctx, """
        SELECT m.item_id, i.name, i.name_en, i.unit, -sum(m.qty) FROM "inventory_movement" m
          JOIN "inventory_item" i ON i.clinic_id = m.clinic_id AND i.id = m.item_id
         WHERE m.clinic_id = %s AND m.appointment_id = %s AND m.kind IN ('use', 'return')
         GROUP BY m.item_id, i.name, i.name_en, i.unit HAVING sum(m.qty) < 0 ORDER BY min(m.at), m.item_id""",
                                                                                        [ctx.clinic_id, appointment_id])]


def recipes(ctx, section_id):
    """Each procedure's materials, for the visit screen to add when one is recorded: {procedure_id: [{item_id, qty}]}."""
    found = {}
    for procedure_id, item_id, qty in sql.rows(ctx, """
        SELECT m.procedure_id, m.item_id, m.qty FROM "inventory_material" m
          JOIN "inventory_item" i ON i.clinic_id = m.clinic_id AND i.id = m.item_id AND i.is_active
          JOIN "clinical_procedure" p ON p.clinic_id = m.clinic_id AND p.id = m.procedure_id
         WHERE m.clinic_id = %s AND (p.section_id = %s OR p.section_id IS NULL) ORDER BY m.procedure_id, i.name""",
                                               [ctx.clinic_id, section_id]):
        found.setdefault(procedure_id, []).append({"item_id": item_id, "qty": qty})
    return found


def finish_visit(ctx, appointment, uses, deltas, records=None):
    """"Done with this visit" (Saged, 2026-11-25): what the visit used comes off the stock, in the finish's transaction.

    `uses` is the visit card's list {item_id: qty} as the doctor left it; None when the visit was finished without
    the card (from the day's list), and then the recorded procedures' own materials are taken as they are. Either
    way those procedures are marked taken, so a finish after going back in takes only what is new. `records`: the
    procedures the card's list already holds; one recorded since (another tab) comes off with its own recipe.
    These movements carry no procedure, so taking a procedure off after the finish puts nothing back (the owner
    fixes the stock by a count); return_materials only serves records from before 2026-11-25.

    Never refused for short stock: the patient was already treated, so the stock's number was wrong. A short item is
    first raised to what was used (an `adjust` movement), then taken to zero, and the owner is told (item.short) so
    it gets counted. Items removed from the list since are skipped. Items are locked by id (lock order 9).
    """
    marked = sql.rows(ctx, 'UPDATE "clinical_visit_procedure" SET stock_taken = true WHERE clinic_id = %s '
                           "AND appointment_id = %s AND NOT stock_taken RETURNING procedure_id, qty, id",
                      [ctx.clinic_id, appointment["id"]])
    if uses is None:
        uses = {}
    elif records is not None:
        # A procedure recorded (in another tab) after the card was read isn't in its list: its recipe as it is.
        marked = [r for r in marked if r[2] not in records]
        uses = dict(uses)
    else:
        marked = []
    times = {}
    for procedure_id, qty, _id in marked:
        times[procedure_id] = times.get(procedure_id, 0) + qty
    if times:
        for procedure_id, item_id, qty in sql.rows(ctx, """
            SELECT procedure_id, item_id, qty FROM "inventory_material"
             WHERE clinic_id = %s AND procedure_id = ANY(%s::bigint[]) ORDER BY procedure_id, item_id""",
                                               [ctx.clinic_id, list(times)]):
            uses[item_id] = uses.get(item_id, 0) + qty * times[procedure_id]
    if not uses:
        return {}
    rows = {r[0]: r[1:] for r in sql.rows(ctx, """
        SELECT id, name, quantity, unit_cost, allow_negative FROM "inventory_item"
         WHERE clinic_id = %s AND id = ANY(%s::bigint[]) AND is_active ORDER BY id FOR NO KEY UPDATE""",
                                          [ctx.clinic_id, sorted(uses)])}
    lines = {item_id: qty for item_id, qty in uses.items() if item_id in rows}
    now = timezone.now()
    for item_id in sorted(lines):
        name, quantity, unit_cost, allow_negative = rows[item_id]
        if allow_negative or quantity >= lines[item_id]:
            continue
        sql.run(ctx, 'UPDATE "inventory_item" SET quantity = %s WHERE clinic_id = %s AND id = %s',
                [lines[item_id], ctx.clinic_id, item_id])
        _insert_movements(ctx, [_movement(ctx, item_id, "adjust", lines[item_id] - quantity, lines[item_id], unit_cost,
                                          patient_id=appointment["patient_id"], appointment_id=appointment["id"],
                                          reason="used in a visit beyond the stock's number", at=now)])
        record_change(ctx, "item.short", "item", item_id,
                      before={"quantity": quantity}, after={"used": lines[item_id]}, appointment=appointment["id"])
    taken, _ids = take(ctx, lines, "use", patient_id=appointment["patient_id"], appointment_id=appointment["id"],
                       deltas=deltas)
    return taken
