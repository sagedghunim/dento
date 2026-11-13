"""The doctor's visit (design E3): reads. A fixed number of indexed queries per request, whatever the history size.

Views that return clinical data write an access event (design 4.10); these functions only read.
"""

import datetime as dt

from core import paging, sql
from core.i18n import pick
from core.days import local_iso, zone

from . import services
from .models import TEETH

LIST_LIMIT = 500  # a section's library: a few hundred rows at most
HISTORY_LIMIT = 100


def _iso(ctx, instant):
    return local_iso(ctx.clinic, instant) if instant else None


# --- Libraries -------------------------------------------------------------------------------------------------


def _section_filter(section_id, include_inactive, alias=""):
    where = f" AND {alias}is_active" if not include_inactive else ""
    params = []
    if section_id is not None:
        where += f" AND {alias}section_id = %s"
        params.append(section_id)
    return where, params


def medicines(ctx, section_id=None, query="", include_inactive=False):
    where, params = _section_filter(section_id, include_inactive)
    rows = sql.rows(ctx, f'SELECT {services.MEDICINE_COLUMNS} FROM "clinical_medicine" WHERE clinic_id = %s{where} '
                         "ORDER BY section_id, sort, name, id LIMIT %s", [ctx.clinic_id, *params, LIST_LIMIT])
    items = [services.medicine_row(r) for r in rows]
    if query:
        needle = query.casefold()
        items = [m for m in items if needle in m["name"].casefold() or needle in m["generic"].casefold()]
    return {"items": items}


def procedures(ctx, section_id=None, include_inactive=False):
    where, params = _section_filter(section_id, include_inactive)
    rows = sql.rows(ctx, f'SELECT {services.PROCEDURE_COLUMNS} FROM "clinical_procedure" WHERE clinic_id = %s{where} '
                         "ORDER BY section_id, sort, id LIMIT %s", [ctx.clinic_id, *params, LIST_LIMIT])
    return {"items": [services.procedure_row(r) for r in rows]}


def templates(ctx, section_id=None, include_inactive=False, template_id=None):
    """Templates with their lines, in two queries."""
    where, params = _section_filter(section_id, include_inactive)
    if template_id is not None:
        where, params = " AND id = %s", [template_id]
    rows = sql.dicts(ctx, 'SELECT id, section_id, name, notes, name_en, notes_en, is_active, version '
                          f'FROM "clinical_prescription_template" WHERE clinic_id = %s{where} '
                          "ORDER BY section_id, name, id LIMIT %s", [ctx.clinic_id, *params, LIST_LIMIT])
    for r in rows:  # the request's language, and both raw texts for editing (design G3)
        r["name_ar"], r["notes_ar"] = r["name"], r["notes"]
        r["name"], r["notes"] = pick(r["name_ar"], r["name_en"]), pick(r["notes_ar"], r["notes_en"])
    if template_id is not None and not rows:
        raise services.not_found("template")
    lines = {}
    if rows:
        for r in sql.dicts(ctx, 'SELECT template_id, position, medicine_id, name, strength, form, dose, note '
                                'FROM "clinical_prescription_template_item" WHERE clinic_id = %s '
                                "AND template_id = ANY(%s::bigint[]) ORDER BY template_id, position",
                           [ctx.clinic_id, [r["id"] for r in rows]]):
            lines.setdefault(r.pop("template_id"), []).append(r)
    items = [{**r, "items": lines.get(r["id"], [])} for r in rows]
    return items[0] if template_id is not None else {"items": items}


# --- Prescriptions ---------------------------------------------------------------------------------------------


def prescription(ctx, prescription_id):
    from scheduling.rules import patient_sql

    mine, params = patient_sql(ctx, "r.clinic_id", "r.patient_id")  # another doctor's patient's is 404 to a doctor
    found = sql.dicts(ctx, f"""
        SELECT r.id, r.number, r.patient_id, r.appointment_id, r.doctor_id, r.section_id, r.notes, r.created_at,
               p.full_name AS patient_name, p.file_no AS patient_file_no, p.allergies, m.display_name AS doctor_name
          FROM "clinical_prescription" r
          JOIN "patients_patient" p ON p.clinic_id = r.clinic_id AND p.id = r.patient_id
          JOIN "clinics_membership" m ON m.clinic_id = r.clinic_id AND m.id = r.doctor_id
         WHERE r.clinic_id = %s AND r.id = %s{mine}""", [ctx.clinic_id, prescription_id, *params])
    if not found:
        raise services.not_found("prescription")
    r = found[0]
    lines = sql.dicts(ctx, 'SELECT position, medicine_id, name, generic, strength, form, dose, note '
                           'FROM "clinical_prescription_item" WHERE clinic_id = %s AND prescription_id = %s ORDER BY position',
                      [ctx.clinic_id, prescription_id])
    return {"id": r["id"], "number": r["number"], "patient": {"id": r["patient_id"], "name": r["patient_name"],
                                                              "file_no": r["patient_file_no"]},
            "appointment_id": r["appointment_id"], "doctor": {"id": r["doctor_id"], "name": r["doctor_name"]},
            "section_id": r["section_id"], "notes": r["notes"], "created_at": _iso(ctx, r["created_at"]),
            "items": lines, "warnings": services.allergy_warnings(r["allergies"], lines)}


def patient_prescriptions(ctx, patient_id, size, after):
    """Newest first on (clinic_id, patient_id, created_at DESC, id DESC)."""
    params, where = [ctx.clinic_id, patient_id], ""
    if after:
        where, params = " AND (created_at, id) < (%s, %s)", [*params, *after]
    rows = sql.dicts(ctx, 'SELECT id, number, appointment_id, doctor_id, section_id, notes, created_at '
                          f'FROM "clinical_prescription" WHERE clinic_id = %s AND patient_id = %s{where} '
                          "ORDER BY created_at DESC, id DESC LIMIT %s", [*params, size + 1])
    lines = {}
    page = rows[:size]
    if page:
        for r in sql.dicts(ctx, 'SELECT prescription_id, position, name, strength, form, dose, note '
                                'FROM "clinical_prescription_item" WHERE clinic_id = %s '
                                "AND prescription_id = ANY(%s::bigint[]) ORDER BY prescription_id, position",
                           [ctx.clinic_id, [r["id"] for r in page]]):
            lines.setdefault(r.pop("prescription_id"), []).append(r)
    items = [{**r, "created_at": _iso(ctx, r["created_at"]), "items": lines.get(r["id"], [])} for r in page]
    return {"items": items,
            "next": paging.encode_time(page[-1]["created_at"], page[-1]["id"]) if len(rows) > size else None}


# --- The visit record ------------------------------------------------------------------------------------------


def note(ctx, appointment_id):
    found = sql.row(ctx, 'SELECT id, complaint, diagnosis, notes, version, updated_at FROM "clinical_visit_note" '
                         "WHERE clinic_id = %s AND appointment_id = %s", [ctx.clinic_id, appointment_id])
    return note_payload(ctx, services._note_row(found)) if found else None


def note_payload(ctx, row):
    return {**row, "updated_at": _iso(ctx, row["updated_at"])} if row else None


def vitals(ctx, appointment_id):
    found = sql.row(ctx, f'SELECT {services.VITALS_COLUMNS} FROM "clinical_vitals" WHERE clinic_id = %s AND appointment_id = %s',
                    [ctx.clinic_id, appointment_id])
    return vitals_payload(ctx, services.vitals_row(found))


def vitals_payload(ctx, row):
    return {**row, "recorded_at": _iso(ctx, row["recorded_at"])} if row else None


def attachment_payload(ctx, r):
    return {"id": r["id"], "patient_id": r["patient_id"], "appointment_id": r["appointment_id"], "title": r["title"],
            "file_name": r["file_name"], "content_type": r["content_type"], "size": r["size"],
            "created_at": _iso(ctx, r["created_at"]), "url": f"/api/v1/attachments/{r['id']}"}


ATTACHMENT_COLUMNS = "id, patient_id, appointment_id, title, file_name, content_type, size, created_at"


def attachments(ctx, patient_id, size, after):
    params, where = [ctx.clinic_id, patient_id], ""
    if after:
        where, params = " AND (created_at, id) < (%s, %s)", [*params, *after]
    rows = sql.dicts(ctx, f'SELECT {ATTACHMENT_COLUMNS} FROM "clinical_attachment" WHERE clinic_id = %s AND patient_id = %s'
                          f"{where} ORDER BY created_at DESC, id DESC LIMIT %s", [*params, size + 1])
    page = rows[:size]
    return {"items": [attachment_payload(ctx, r) for r in page],
            "next": paging.encode_time(page[-1]["created_at"], page[-1]["id"]) if len(rows) > size else None}


def attachment(ctx, attachment_id):
    from scheduling.rules import patient_sql

    mine, params = patient_sql(ctx, "x.clinic_id", "x.patient_id")  # another doctor's patient's is 404 to a doctor
    found = sql.dicts(ctx, f'SELECT {ATTACHMENT_COLUMNS}, storage_key FROM "clinical_attachment" x '
                           f"WHERE x.clinic_id = %s AND x.id = %s{mine}", [ctx.clinic_id, attachment_id, *params])
    if not found:
        raise services.not_found("attachment")
    return found[0]


def visit_procedures(ctx, appointment_id):
    rows = sql.dicts(ctx, """
        SELECT v.id, v.procedure_id, c.name_ar, c.name_en, v.plan_item_id, v.tooth, v.surfaces, v.area, v.qty, v.unit_price,
               v.amount, v.invoice_line_id, v.note, v.created_at
          FROM "clinical_visit_procedure" v
          JOIN "clinical_procedure" c ON c.clinic_id = v.clinic_id AND c.id = v.procedure_id
         WHERE v.clinic_id = %s AND v.appointment_id = %s ORDER BY v.id LIMIT %s""",
                     [ctx.clinic_id, appointment_id, HISTORY_LIMIT])
    return [{**r, "surfaces": list(r["surfaces"]), "created_at": _iso(ctx, r["created_at"])} for r in rows]


def visit_procedure(ctx, record_id):
    found = sql.dicts(ctx, """
        SELECT v.id, v.appointment_id, v.patient_id, v.procedure_id, c.name_ar, c.name_en, v.plan_item_id, v.tooth,
               v.surfaces, v.area, v.qty, v.unit_price, v.amount, v.invoice_line_id, v.note, v.created_at,
               (v.tooth IS NOT NULL AND c.condition <> '') AS sets_tooth
          FROM "clinical_visit_procedure" v
          JOIN "clinical_procedure" c ON c.clinic_id = v.clinic_id AND c.id = v.procedure_id
         WHERE v.clinic_id = %s AND v.id = %s""", [ctx.clinic_id, record_id])
    r = found[0]
    return {**r, "surfaces": list(r["surfaces"]), "created_at": _iso(ctx, r["created_at"])}


def patient_tooth(ctx, patient_id, number):
    found = sql.row(ctx, 'SELECT id, tooth, condition, surfaces, note, updated_at FROM "clinical_tooth" '
                         "WHERE clinic_id = %s AND patient_id = %s AND tooth = %s", [ctx.clinic_id, patient_id, number])
    return tooth_payload(ctx, services.tooth_row(found)) if found else None


def visit_record(ctx, appointment_id):
    """The visit screen in one answer: the visit, complaint and notes, vitals, recorded procedures, this visit's
    prescriptions and attachments, the patient's allergies, open plan items and balance. Nine queries."""
    from billing.selectors import patient_balance, visit_invoice
    from scheduling.selectors import appointment as appointment_payload

    visit = appointment_payload(ctx, appointment_id)
    patient_id = visit["patient"]["id"]
    allergies = sql.row(ctx, 'SELECT allergies, chronic_conditions FROM "patients_patient" WHERE clinic_id = %s AND id = %s',
                        [ctx.clinic_id, patient_id])
    prescriptions = sql.dicts(ctx, 'SELECT id, number, created_at FROM "clinical_prescription" WHERE clinic_id = %s '
                                   "AND patient_id = %s AND appointment_id = %s ORDER BY id",
                              [ctx.clinic_id, patient_id, appointment_id])
    files = sql.dicts(ctx, f'SELECT {ATTACHMENT_COLUMNS} FROM "clinical_attachment" WHERE clinic_id = %s AND patient_id = %s '
                           "AND appointment_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                      [ctx.clinic_id, patient_id, appointment_id, HISTORY_LIMIT])
    return {
        "appointment": visit,
        "patient": {"id": patient_id, "allergies": allergies[0], "chronic_conditions": allergies[1]},
        "note": note(ctx, appointment_id),
        "vitals": vitals(ctx, appointment_id),
        "procedures": visit_procedures(ctx, appointment_id),
        "prescriptions": [{**r, "created_at": _iso(ctx, r["created_at"])} for r in prescriptions],
        "attachments": [attachment_payload(ctx, r) for r in files],
        "open_plan_items": open_plan_items(ctx, patient_id),
        "invoice": visit_invoice(ctx, appointment_id),
        "balance": patient_balance(ctx, patient_id),
    }


# --- The dental chart and plans --------------------------------------------------------------------------------


def open_plan_items(ctx, patient_id):
    rows = sql.dicts(ctx, """
        SELECT i.id, i.plan_id, i.procedure_id, coalesce(c.name_ar, i.label) AS name_ar,
               coalesce(c.name_en, '') AS name_en, i.tooth, i.area, i.price, i.status, i.due_on
          FROM "clinical_plan" p
          JOIN "clinical_plan_item" i ON i.clinic_id = p.clinic_id AND i.plan_id = p.id
          LEFT JOIN "clinical_procedure" c ON c.clinic_id = i.clinic_id AND c.id = i.procedure_id
         WHERE p.clinic_id = %s AND p.patient_id = %s AND i.status IN ('planned', 'in_progress')
         ORDER BY p.created_at, p.id, i.position LIMIT %s""", [ctx.clinic_id, patient_id, HISTORY_LIMIT])
    return [{**r, "due_on": r["due_on"].isoformat() if r["due_on"] else None} for r in rows]


def tooth_payload(ctx, row):
    if row is None:
        return None
    return {**row, "surfaces": list(row["surfaces"]), "updated_at": _iso(ctx, row["updated_at"])}


def tooth(ctx, tooth_id):
    found = sql.row(ctx, 'SELECT id, tooth, condition, surfaces, note, updated_at FROM "clinical_tooth" '
                         "WHERE clinic_id = %s AND id = %s", [ctx.clinic_id, tooth_id])
    return tooth_payload(ctx, services.tooth_row(found)) if found else None


def chart(ctx, patient_id):
    """Every tooth with a recorded state, and which teeth are in an open plan item ("in progress", design E3)."""
    rows = sql.rows(ctx, 'SELECT id, tooth, condition, surfaces, note, updated_at FROM "clinical_tooth" '
                         "WHERE clinic_id = %s AND patient_id = %s ORDER BY tooth", [ctx.clinic_id, patient_id])
    planned = {}
    for item in open_plan_items(ctx, patient_id):
        if item["tooth"] is not None:
            planned.setdefault(item["tooth"], []).append(item["id"])
    teeth = []
    for r in rows:
        row = tooth_payload(ctx, services.tooth_row(r))
        teeth.append({**row, "in_progress": row["tooth"] in planned, "plan_items": planned.get(row["tooth"], [])})
    recorded = {t["tooth"] for t in teeth}
    for tooth in sorted(set(planned) - recorded):
        teeth.append({"id": None, "tooth": tooth, "condition": "healthy", "surfaces": [], "note": "", "updated_at": None,
                      "in_progress": True, "plan_items": planned[tooth]})
    teeth.sort(key=lambda t: t["tooth"])
    return {"teeth": teeth, "numbers": {"permanent": [t for t in TEETH if t < 50], "primary": [t for t in TEETH if t > 50]}}


def tooth_history(ctx, patient_id, tooth, size, after):
    """That tooth's rows, newest first (index (clinic_id, patient_id, tooth, at DESC, id DESC))."""
    params, where = [ctx.clinic_id, patient_id, tooth], ""
    if after:
        where, params = " AND (h.at, h.id) < (%s, %s)", [*params, *after]
    rows = sql.dicts(ctx, f"""
        SELECT h.id, h.tooth, h.condition, h.surfaces, h.appointment_id, a.day, h.procedure_id, c.name_ar, c.name_en,
               h.visit_procedure_id, h.note, h.at, h.by_id
          FROM "clinical_tooth_history" h
          LEFT JOIN "scheduling_appointment" a ON a.clinic_id = h.clinic_id AND a.id = h.appointment_id
          LEFT JOIN "clinical_procedure" c ON c.clinic_id = h.clinic_id AND c.id = h.procedure_id
         WHERE h.clinic_id = %s AND h.patient_id = %s AND h.tooth = %s{where}
         ORDER BY h.at DESC, h.id DESC LIMIT %s""", [*params, size + 1])
    page = rows[:size]
    items = [{**r, "surfaces": list(r["surfaces"]), "day": r["day"].isoformat() if r["day"] else None,
              "at": _iso(ctx, r["at"])} for r in page]
    return {"items": items, "next": paging.encode_time(page[-1]["at"], page[-1]["id"]) if len(rows) > size else None}


def plans(ctx, patient_id, plan_id=None):
    """A patient's plans with their items, estimated totals and open counts, newest first. Two queries."""
    params, where = [ctx.clinic_id, patient_id], ""
    if plan_id is not None:
        where, params = " AND id = %s", [*params, plan_id]
    rows = sql.dicts(ctx, 'SELECT id, section_id, title, created_at FROM "clinical_plan" WHERE clinic_id = %s '
                          f"AND patient_id = %s{where} ORDER BY created_at DESC, id DESC LIMIT %s",
                     [*params, HISTORY_LIMIT])
    if plan_id is not None and not rows:
        raise services.not_found("plan")
    items = {}
    if rows:
        for r in sql.dicts(ctx, """
            SELECT i.plan_id, i.id, i.position, i.procedure_id, coalesce(c.name_ar, i.label) AS name_ar,
                   coalesce(c.name_en, '') AS name_en, coalesce(c.unit, 'session') AS unit, i.label, i.tooth, i.area,
                   i.price, i.status, i.done_appointment_id, i.due_on, i.appointment_id
              FROM "clinical_plan_item" i
              LEFT JOIN "clinical_procedure" c ON c.clinic_id = i.clinic_id AND c.id = i.procedure_id
             WHERE i.clinic_id = %s AND i.plan_id = ANY(%s::bigint[]) ORDER BY i.plan_id, i.position""",
                           [ctx.clinic_id, [r["id"] for r in rows]]):
            r["due_on"] = r["due_on"].isoformat() if r["due_on"] else None
            items.setdefault(r.pop("plan_id"), []).append(r)
    result = []
    for r in rows:
        mine = items.get(r["id"], [])
        live = [i for i in mine if i["status"] != "dropped"]
        open_items = [i for i in mine if i["status"] in ("planned", "in_progress")]
        result.append({**r, "created_at": _iso(ctx, r["created_at"]), "items": mine,
                       "estimated_total": sum(i["price"] for i in live),
                       "done_total": sum(i["price"] for i in mine if i["status"] == "done"),
                       "open_count": len(open_items), "unfinished": bool(open_items)})
    return result[0] if plan_id is not None else {"items": result,
                                                  "unfinished": any(p["unfinished"] for p in result)}


# --- The timeline ----------------------------------------------------------------------------------------------

# Each source has a rank, so events at the same instant have one order across tables: (at, rank, id) DESC.
RANKS = {"visit": 1, "payment": 2, "prescription": 3, "procedure": 4}


def timeline_cursor(raw):
    if not raw:
        return None
    try:
        micros, rank, ident = raw.split(".")
        at = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=int(micros))
        rank, ident = int(rank), int(ident)
        if rank not in RANKS.values() or not 0 <= ident < 2**63:
            raise ValueError(raw)
        return at, rank, ident
    except (ValueError, OverflowError):
        from core.errors import ApiError

        raise ApiError("invalid", "after must be a cursor from a previous page", fields={"after": "cursor"})


def _before(cursor, rank, at_sql, id_sql):
    """The keyset condition for one source, so each query keeps its index order."""
    if cursor is None:
        return "", []
    at, c_rank, c_id = cursor
    if rank < c_rank:
        return f" AND {at_sql} <= %s", [at]
    if rank > c_rank:
        return f" AND {at_sql} < %s", [at]
    return f" AND ({at_sql}, {id_sql}) < (%s, %s)", [at, c_id]


def timeline(ctx, patient_id, size, cursor):
    """The patient's visits, payments, prescriptions and procedures, newest first (design E3). Four queries, each
    bounded by the page size on its patient index; merged here."""
    tz = str(zone(ctx.clinic))
    visit_at = f"coalesce(a.arrived_at, a.starts_at, (a.day::timestamp AT TIME ZONE '{tz}'))"
    where, params = _before(cursor, RANKS["visit"], visit_at, "a.id")
    visits = sql.dicts(ctx, f"""
        SELECT a.id, {visit_at} AS at, a.day, a.status, a.doctor_id, s.name AS service_name, a.fee
          FROM "scheduling_appointment" a
          JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
         WHERE a.clinic_id = %s AND a.patient_id = %s{where}
         ORDER BY at DESC, a.id DESC LIMIT %s""", [ctx.clinic_id, patient_id, *params, size + 1])
    where, params = _before(cursor, RANKS["payment"], "received_at", "id")
    payments = sql.dicts(ctx, f"""
        SELECT id, received_at AS at, kind, method, amount, invoice_id FROM "billing_payment"
         WHERE clinic_id = %s AND patient_id = %s{where} ORDER BY received_at DESC, id DESC LIMIT %s""",
                         [ctx.clinic_id, patient_id, *params, size + 1])
    where, params = _before(cursor, RANKS["prescription"], "created_at", "id")
    prescriptions = sql.dicts(ctx, f"""
        SELECT id, created_at AS at, number, appointment_id FROM "clinical_prescription"
         WHERE clinic_id = %s AND patient_id = %s{where} ORDER BY created_at DESC, id DESC LIMIT %s""",
                              [ctx.clinic_id, patient_id, *params, size + 1])
    where, params = _before(cursor, RANKS["procedure"], "v.created_at", "v.id")
    procedures_ = sql.dicts(ctx, f"""
        SELECT v.id, v.created_at AS at, v.appointment_id, c.name_ar, c.name_en, v.tooth, v.surfaces, v.amount
          FROM "clinical_visit_procedure" v
          JOIN "clinical_procedure" c ON c.clinic_id = v.clinic_id AND c.id = v.procedure_id
         WHERE v.clinic_id = %s AND v.patient_id = %s{where} ORDER BY v.created_at DESC, v.id DESC LIMIT %s""",
                            [ctx.clinic_id, patient_id, *params, size + 1])
    events = []
    for kind, rows in (("visit", visits), ("payment", payments), ("prescription", prescriptions),
                       ("procedure", procedures_)):
        for r in rows:
            events.append((r["at"], RANKS[kind], r["id"], kind, r))
    events.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
    page = events[:size]
    items = []
    for at, _rank, _id, kind, r in page:
        body = {k: v for k, v in r.items() if k != "at"}
        if "day" in body:
            body["day"] = body["day"].isoformat()
        if "surfaces" in body:
            body["surfaces"] = list(body["surfaces"])
        items.append({"type": kind, "at": _iso(ctx, at), **body})
    next_cursor = None
    if len(events) > size and page:
        at, rank, ident = page[-1][0], page[-1][1], page[-1][2]
        micros = (at - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(microseconds=1)
        next_cursor = f"{micros}.{rank}.{ident}"
    return {"items": items, "next": next_cursor}
