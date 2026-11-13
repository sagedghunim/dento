"""Follow-ups (Saged, 2026-11-13): what the doctor says comes next, and the desk's list of what to book.

A follow-up is a plan item with a suggested day (`due_on`), the visit that suggested it (`visit_id`) and, once the
desk books it, its appointment (`appointment_id`). A procedure's recipe (`Procedure.follow_up`) offers its usual
sessions in the visit. The desk's list shows the unbooked ones once their visit is finished; a booking that was
cancelled or missed puts its follow-up back on the list.

Lock order: key -> plan row -> its items (as the treatment plan's own writes).
"""

import datetime as dt

from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.days import business_day
from core.errors import ApiError
from core.i18n import pick
from core.numbers import parse_int
from core.validate import ident, text

MAX_STEPS = 6
MAX_DAYS = 730
MAX_ADD = 12
LIST_SIZE = 100
SNOOZE_DAYS = 7
TOP = 6


def clean_recipe(value):
    """[{"days", "label", "label_en"}], at most six steps, soonest first. [] turns the recipe off."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_STEPS:
        raise ApiError("invalid", _("A follow-up recipe has up to 6 sessions."), fields={"follow_up": "count"})
    steps = []
    for index, step in enumerate(value):
        if not isinstance(step, dict):
            raise ApiError("invalid", "each step must be an object", fields={f"follow_up.{index}": "object"})
        try:
            steps.append({"days": parse_int(step.get("days"), "days", 1, MAX_DAYS),
                          "label": text(step, "label", 80), "label_en": text(step, "label_en", 80, required=False)})
        except ApiError as error:
            error.fields = {f"follow_up.{index}.{field}": code for field, code in error.fields.items()}
            raise
    return sorted(steps, key=lambda s: s["days"])


def _visible(ctx):
    """(sql, params) on `a`, the visit that suggested the follow-up: an assistant's sections, a doctor's own visits."""
    from scheduling.rules import own_doctor, section_scope

    where, params = "", []
    scope = section_scope(ctx)
    if scope is not None:
        where, params = " AND a.section_id = ANY(%s::bigint[])", [sorted(scope)]
    doctor = own_doctor(ctx)
    if doctor is not None:
        where, params = where + " AND a.doctor_id = %s", [*params, doctor]
    return where, params


# --- In the visit ----------------------------------------------------------------------------------------------


def upcoming(ctx, patient_id, today=None):
    """What comes next for this patient: the follow-ups not booked yet, and the booked ones still ahead."""
    today = today or business_day(ctx.clinic)
    rows = sql.dicts(ctx, """
        SELECT i.id, i.procedure_id, c.name_ar, c.name_en, i.label, i.tooth, i.due_on, i.visit_id,
               (b.id IS NOT NULL) AS booked, b.day AS booked_day
          FROM "clinical_plan" p
          JOIN "clinical_plan_item" i ON i.clinic_id = p.clinic_id AND i.plan_id = p.id
          LEFT JOIN "clinical_procedure" c ON c.clinic_id = i.clinic_id AND c.id = i.procedure_id
          LEFT JOIN "scheduling_appointment" b ON b.clinic_id = i.clinic_id AND b.id = i.appointment_id
               AND b.status NOT IN ('cancelled', 'no_show')
         WHERE p.clinic_id = %s AND p.patient_id = %s AND i.status = 'planned' AND i.due_on IS NOT NULL
           AND (b.id IS NULL OR b.day >= %s)
         ORDER BY i.due_on, i.id LIMIT 60""", [ctx.clinic_id, patient_id, today])
    return [{"id": r["id"], "procedure_id": r["procedure_id"], "name": pick(r["name_ar"], r["name_en"]) or r["label"],
             "tooth": r["tooth"], "due_on": r["due_on"].isoformat(), "days": (r["due_on"] - today).days,
             "visit_id": r["visit_id"], "booked": r["booked"],
             "booked_day": r["booked_day"].isoformat() if r["booked_day"] else None} for r in rows]


def clean_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ADD:
        raise ApiError("invalid", _("Pick what comes next and when."), fields={"items": "count"})
    cleaned = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError("invalid", "each item must be an object", fields={f"items.{index}": "object"})
        try:
            procedure = item.get("procedure_id")
            cleaned.append({"procedure_id": ident(procedure, "procedure_id") if procedure is not None else None,
                            "label": text(item, "label", 80, required=procedure is None),
                            "days": parse_int(item.get("days"), "days", 1, MAX_DAYS)})
        except ApiError as error:
            error.fields = {f"items.{index}.{field}": code for field, code in error.fields.items()}
            raise
    return cleaned


def add(ctx, appointment_id, items=None, recipe_of=None, tooth=None):
    """Follow-ups from this visit: `items` [{procedure_id | label, days}], or the recipe of a procedure recorded in
    it (`recipe_of`). Days count from the visit's day. They join the patient's plan in the visit's section (a plan is
    opened when there is none). Returns the patient's id."""
    from . import services

    appointment = services._appointment(ctx, appointment_id)  # the member's sections, a doctor's own visits
    if appointment["status"] in ("booked", "cancelled", "no_show"):
        raise ApiError("visit_closed", _("Follow-ups are added in a visit that has arrived."), 409,
                       fields={"status": appointment["status"]})
    rows = []  # (procedure_id, label, days, price)
    if recipe_of is not None:
        procedure = services._procedure(ctx, recipe_of, active=False)
        if procedure["section_id"] != appointment["section_id"]:
            raise ApiError("invalid", _("This procedure belongs to another section."), fields={"recipe_of": "section"})
        steps = procedure["follow_up"] or []
        if not steps:
            raise ApiError("invalid", _("This procedure has no follow-up sessions."), fields={"recipe_of": "empty"})
        rows = [(None, pick(step["label"], step.get("label_en", "")), step["days"], 0) for step in steps]
    else:
        cleaned = clean_items(items)
        ids = sorted({item["procedure_id"] for item in cleaned if item["procedure_id"] is not None})
        known = {r[0]: r for r in sql.rows(
            ctx, 'SELECT id, section_id, price, is_active FROM "clinical_procedure" WHERE clinic_id = %s '
                 "AND id = ANY(%s::bigint[])", [ctx.clinic_id, ids])} if ids else {}
        for index, item in enumerate(cleaned):
            if item["procedure_id"] is None:
                rows.append((None, item["label"], item["days"], 0))
                continue
            procedure = known.get(item["procedure_id"])
            if procedure is None:
                raise services.not_found("procedure")
            if procedure[1] != appointment["section_id"] or not procedure[3]:
                raise ApiError("invalid", _("This procedure belongs to another section."),
                               fields={f"items.{index}.procedure_id": "section"})
            rows.append((procedure[0], "", item["days"], procedure[2]))  # the list price, never one from the screen
    plan = sql.row(ctx, 'SELECT id FROM "clinical_plan" WHERE clinic_id = %s AND patient_id = %s AND section_id = %s '
                        "ORDER BY created_at DESC, id DESC LIMIT 1 FOR NO KEY UPDATE",
                   [ctx.clinic_id, appointment["patient_id"], appointment["section_id"]])
    now = timezone.now()
    if plan is None:
        plan = sql.row(ctx, 'INSERT INTO "clinical_plan" (clinic_id, patient_id, section_id, title, created_at, '
                            "created_by_id) VALUES (%s, %s, %s, '', %s, %s) RETURNING id",
                       [ctx.clinic_id, appointment["patient_id"], appointment["section_id"], now, ctx.membership_id])
    count, last = sql.row(ctx, 'SELECT count(*), coalesce(max(position), 0) FROM "clinical_plan_item" '
                               "WHERE clinic_id = %s AND plan_id = %s", [ctx.clinic_id, plan[0]])
    if count + len(rows) > services.MAX_PLAN_ITEMS:
        raise ApiError("invalid", _("A plan has 1 to 60 items."), fields={"items": "count"})
    values, params = [], []
    for offset, (procedure_id, label, days, price) in enumerate(rows, 1):
        values.append("(%s, %s, %s, %s, %s, %s, '', %s, 'planned', %s, %s, %s)")
        params += [ctx.clinic_id, plan[0], procedure_id, label, last + offset, tooth, price,
                   appointment["day"] + dt.timedelta(days=days), appointment_id, now]
    sql.run(ctx, 'INSERT INTO "clinical_plan_item" (clinic_id, plan_id, procedure_id, label, position, tooth, area, '
                 f"price, status, due_on, visit_id, updated_at) VALUES {', '.join(values)}", params)
    return appointment["patient_id"]


def _item(ctx, item_id):
    """A follow-up the member may act on (locked), with its patient and the visit that suggested it; else 404."""
    from . import services

    where, params = _visible(ctx)
    found = sql.dicts(ctx, f"""
        SELECT i.id, i.status, i.due_on, i.appointment_id, p.patient_id, a.day AS visit_day, a.doctor_id
          FROM "clinical_plan_item" i
          JOIN "clinical_plan" p ON p.clinic_id = i.clinic_id AND p.id = i.plan_id
          JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.visit_id
         WHERE i.clinic_id = %s AND i.id = %s{where} FOR NO KEY UPDATE OF i""", [ctx.clinic_id, item_id, *params])
    if not found:
        raise services.not_found("plan_item")
    return found[0]


def set_due(ctx, item_id, days):
    """The suggested day, in days after the visit that suggested it. Only while it is planned and not booked."""
    item = _item(ctx, item_id)
    if item["status"] != "planned" or item["appointment_id"] is not None:
        raise ApiError("status_changed", _("This follow-up was already booked or closed."), 409,
                       fields={"status": item["status"]})
    sql.run(ctx, 'UPDATE "clinical_plan_item" SET due_on = %s, snoozed_until = NULL, updated_at = %s '
                 "WHERE clinic_id = %s AND id = %s",
            [item["visit_day"] + dt.timedelta(days=days), timezone.now(), ctx.clinic_id, item_id])
    return item["patient_id"]


def drop(ctx, item_id):
    """The doctor takes a follow-up back. A repeat changes nothing."""
    item = _item(ctx, item_id)
    if item["status"] == "planned":
        sql.run(ctx, "UPDATE \"clinical_plan_item\" SET status = 'dropped', updated_at = %s WHERE clinic_id = %s AND id = %s",
                [timezone.now(), ctx.clinic_id, item_id])
    elif item["status"] != "dropped":
        raise ApiError("status_changed", _("This follow-up was already booked or closed."), 409,
                       fields={"status": item["status"]})
    return item["patient_id"]


# --- At the desk -----------------------------------------------------------------------------------------------


def _ids(value, name="item_ids"):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ADD:
        raise ApiError("invalid", f"{name} must list 1 to {MAX_ADD} ids", fields={name: "count"})
    return sorted({ident(v, name) for v in value})


def snooze(ctx, item_ids, days=SNOOZE_DAYS):
    """The desk's "later": these follow-ups leave the list for some days. A repeat changes nothing more."""
    until = business_day(ctx.clinic) + dt.timedelta(days=days)
    for item_id in _ids(item_ids):  # in id order: one lock order for every caller
        item = _item(ctx, item_id)
        if item["status"] != "planned":
            continue
        sql.run(ctx, 'UPDATE "clinical_plan_item" SET snoozed_until = %s, updated_at = %s WHERE clinic_id = %s AND id = %s',
                [until, timezone.now(), ctx.clinic_id, item_id])
        record_change(ctx, "follow_up.snooze", "plan_item", item_id, after={"snoozed_until": until.isoformat()})
    return until


def link_booked(ctx, item_ids, appointment_id):
    """These follow-ups were booked as this appointment (the same patient's). Phase B calls this from the booking
    itself; until then the Follow-ups screen calls it right after the Book dialog answers."""
    from . import services

    found = sql.row(ctx, 'SELECT patient_id, status FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, appointment_id])
    if found is None:
        raise services.not_found("appointment")
    linked = []
    for item_id in _ids(item_ids):
        item = _item(ctx, item_id)
        if item["patient_id"] != found[0]:
            raise ApiError("invalid", _("This appointment is for another patient."), fields={"appointment_id": "patient"})
        if item["status"] != "planned" or item["appointment_id"] == appointment_id:
            continue
        sql.run(ctx, 'UPDATE "clinical_plan_item" SET appointment_id = %s, snoozed_until = NULL, updated_at = %s '
                     "WHERE clinic_id = %s AND id = %s", [appointment_id, timezone.now(), ctx.clinic_id, item_id])
        record_change(ctx, "follow_up.booked", "plan_item", item_id, after={"appointment_id": appointment_id})
        linked.append(item_id)
    return linked


def to_book(ctx, today=None, after=None, size=LIST_SIZE):
    """The desk's list: planned follow-ups of finished visits, not booked (or whose booking was cancelled or missed)
    and not put off, soonest first. One row per patient and urgency: overdue, this week, later.

    Keyset on (due_on, id) through the index plan_item_follow_up. Returns (groups, next cursor or None).
    """
    today = today or business_day(ctx.clinic)
    where, params = _visible(ctx)
    if after:
        where, params = where + " AND (i.due_on, i.id) > (%s, %s)", [*params, *after]
    rows = sql.dicts(ctx, f"""
        SELECT i.id, i.due_on, i.label, i.tooth, c.name_ar, c.name_en, p.patient_id, pt.full_name, pt.phone, pt.file_no,
               a.doctor_id, a.section_id, m.display_name, m.display_name_en
          FROM "clinical_plan_item" i
          JOIN "clinical_plan" p ON p.clinic_id = i.clinic_id AND p.id = i.plan_id
          JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.visit_id
          JOIN "patients_patient" pt ON pt.clinic_id = p.clinic_id AND pt.id = p.patient_id
          JOIN "clinics_membership" m ON m.clinic_id = a.clinic_id AND m.id = a.doctor_id
          LEFT JOIN "clinical_procedure" c ON c.clinic_id = i.clinic_id AND c.id = i.procedure_id
          LEFT JOIN "scheduling_appointment" b ON b.clinic_id = i.clinic_id AND b.id = i.appointment_id
               AND b.status NOT IN ('cancelled', 'no_show')
         WHERE i.clinic_id = %s AND i.status = 'planned' AND i.due_on IS NOT NULL AND a.status = 'done'
           AND b.id IS NULL AND (i.snoozed_until IS NULL OR i.snoozed_until <= %s){where}
         ORDER BY i.due_on, i.id LIMIT %s""", [ctx.clinic_id, today, *params, size + 1])
    page = rows[:size]
    week = today + dt.timedelta(days=7)  # "in a week" is this week's work
    groups = {"overdue": {}, "week": {}, "later": {}}
    for r in page:
        bucket = "overdue" if r["due_on"] < today else "week" if r["due_on"] <= week else "later"
        row = groups[bucket].setdefault((r["patient_id"], r["doctor_id"]), {
            "patient": {"id": r["patient_id"], "name": r["full_name"], "phone": r["phone"], "file_no": r["file_no"]},
            "doctor_id": r["doctor_id"], "doctor": pick(r["display_name"], r["display_name_en"]),
            "section_id": r["section_id"], "day": r["due_on"], "items": []})
        row["items"].append({"id": r["id"], "name": pick(r["name_ar"], r["name_en"]) or r["label"], "tooth": r["tooth"],
                             "due_on": r["due_on"]})
    cursor = (page[-1]["due_on"].isoformat(), page[-1]["id"]) if len(rows) > size else None
    return [{"key": key, "rows": list(found.values())} for key, found in groups.items() if found], cursor


# --- The doctor's most used procedures --------------------------------------------------------------------------


def top_procedures(ctx, doctor_id, section_id, today=None, count=TOP):
    """The ids of the doctor's most recorded procedures in the last 90 days (their visits by day, then each visit's
    records), most used first; the section's first procedures fill the rest."""
    today = today or business_day(ctx.clinic)
    used = [r[0] for r in sql.rows(ctx, """
        SELECT v.procedure_id FROM "scheduling_appointment" a
          JOIN "clinical_visit_procedure" v ON v.clinic_id = a.clinic_id AND v.appointment_id = a.id
          JOIN "clinical_procedure" c ON c.clinic_id = v.clinic_id AND c.id = v.procedure_id
         WHERE a.clinic_id = %s AND a.day >= %s AND a.doctor_id = %s AND c.section_id = %s AND c.is_active
         GROUP BY v.procedure_id ORDER BY count(*) DESC, v.procedure_id LIMIT %s""",
                                   [ctx.clinic_id, today - dt.timedelta(days=90), doctor_id, section_id, count])]
    if len(used) < count:
        used += [r[0] for r in sql.rows(ctx, 'SELECT id FROM "clinical_procedure" WHERE clinic_id = %s AND section_id = %s '
                                             "AND is_active AND NOT (id = ANY(%s::bigint[])) ORDER BY sort, id LIMIT %s",
                                        [ctx.clinic_id, section_id, used, count - len(used)])]
    return used
