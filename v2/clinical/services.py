"""The doctor's visit (design 4.6, 5.5, 5.6, E3): writes. Every function runs inside the request's shard transaction.

Lock order (design 5.0, rows added for M2):
- visit note: key -> the note row (conditional on its version);
- vitals: key -> the vitals row (conditional on its version);
- prescription: key -> [medicines added from it, inserted] -> counter prescription;
- recording a procedure: key -> appointment -> plan item -> patient package (F7) -> invoice (or counter invoice)
  -> inventory items (its materials, F2) -> tooth row -> doctor_day -> clinic_day;
- a tooth's state: key -> tooth row;
- treatment plan: key -> plan row -> its items;
- library rows (medicines, templates, procedures): key -> the row.
Nothing clinical is deleted: library rows and templates are deactivated, plan items dropped, notes keep revisions.
"""

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from billing import services as billing
from clinics.services import take_numbers
from core import sql
from core.days import business_day
from core.errors import ApiError
from core.numbers import parse_int, parse_money
from core.text import normalize_name
from core.validate import choice, ident, text
from summaries.services import Deltas

from .models import CONDITIONS, PLAN_ITEM_STATUSES, SURFACES, TEETH, UNITS

MAX_RX_ITEMS = 30
MAX_PLAN_ITEMS = 60
SURFACE_CONDITIONS = ("caries", "filling")  # the conditions that take surfaces (design E3)
# Plan item moves (design E3). done and dropped are final; recording a procedure moves an item to done.
PLAN_MOVES = {"planned": {"in_progress", "done", "dropped"}, "in_progress": {"planned", "done", "dropped"}}


def not_found(what):
    messages = {
        "appointment": _("This appointment wasn't found."),
        "patient": _("This patient wasn't found."),
        "section": _("This section wasn't found."),
        "medicine": _("This medicine wasn't found."),
        "template": _("This prescription template wasn't found."),
        "procedure": _("This procedure wasn't found."),
        "prescription": _("This prescription wasn't found."),
        "plan": _("This treatment plan wasn't found."),
        "plan_item": _("This plan item wasn't found."),
        "attachment": _("This attachment wasn't found."),
    }
    return ApiError("not_found", messages[what], 404)


def _constraint(error):
    return getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)


def _savepoint_insert(ctx, statement, params, duplicate):
    """An insert that may hit a unique index: a savepoint keeps the request's transaction usable for the 409."""
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        if _constraint(error) in duplicate:
            raise duplicate[_constraint(error)]
        raise


def _section(ctx, section_id):
    found = sql.row(ctx, 'SELECT id, specialty_code FROM "clinics_section" WHERE clinic_id = %s AND id = %s AND is_active',
                    [ctx.clinic_id, section_id])
    if found is None:
        raise not_found("section")
    return {"id": found[0], "specialty": found[1]}


def default_section(ctx, data, name="section_id"):
    """The section a library write names; a place with one section may leave it out."""
    if data.get(name) is not None:
        return _section(ctx, ident(data.get(name), name))
    found = sql.rows(ctx, 'SELECT id, specialty_code FROM "clinics_section" WHERE clinic_id = %s AND is_active '
                          "ORDER BY sort, id LIMIT 2", [ctx.clinic_id])
    if len(found) != 1:
        raise ApiError("invalid", _("Pick the section."), fields={name: "required"})
    return {"id": found[0][0], "specialty": found[0][1]}


def _patient(ctx, patient_id, lock=False):
    from scheduling.rules import patient_sql

    mine, params = patient_sql(ctx, "p.clinic_id", "p.id")  # another doctor's patient is 404 to a doctor
    found = sql.row(ctx, f'SELECT p.id, p.allergies FROM "patients_patient" p WHERE p.clinic_id = %s AND p.id = %s{mine}',
                    [ctx.clinic_id, patient_id, *params])
    if found is None:
        raise not_found("patient")
    return {"id": found[0], "allergies": found[1]}


def _appointment(ctx, appointment_id):
    found = sql.row(ctx, 'SELECT id, status, day, doctor_id, patient_id, section_id FROM "scheduling_appointment" '
                         "WHERE clinic_id = %s AND id = %s", [ctx.clinic_id, appointment_id])
    if found is None:
        raise not_found("appointment")
    appointment = dict(zip(("id", "status", "day", "doctor_id", "patient_id", "section_id"), found))
    # Every caller writes clinical data on this visit: the member's sections, and a doctor's own visits (design G1).
    from scheduling.rules import check_scope

    check_scope(ctx, appointment["section_id"], appointment["doctor_id"])
    return appointment


def _open_visit(appointment):
    """Clinical records go on a visit that took place or is taking place, never on a cancelled one."""
    if appointment["status"] in ("cancelled", "no_show"):
        raise ApiError("visit_closed", _("This visit was cancelled or missed."), 409, fields={"status": appointment["status"]})


# --- Copying a specialty into a section (design E4) ------------------------------------------------------------


# Columns added after clinical 0002 (design G3, then the follow-up recipe): that older migration skips them.
ENGLISH_COLUMNS = {"clinical_procedure": {"description_en", "follow_up"},
                   "clinical_medicine": {"form_en", "default_dose_en", "default_note_en"},
                   "clinical_prescription_template": {"name_en", "notes_en"}}


def _missing_english(alias):
    """The design G3 columns a shard doesn't have yet. Only clinical 0002, an older migration that calls this code,
    meets a shard without them; everywhere else this is empty."""
    from django.db import connections

    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT table_name, column_name FROM information_schema.columns "
                       "WHERE table_name = ANY(%s) AND column_name = ANY(%s)",
                       [list(ENGLISH_COLUMNS), sorted(set().union(*ENGLISH_COLUMNS.values()))])
        found = {(table, column) for table, column in cursor.fetchall()}
    return {(table, column) for table, columns in ENGLISH_COLUMNS.items() for column in columns
            if (table, column) not in found}


def _insert(model, objs, skip):
    """bulk_create without the (table, column) pairs in `skip`; sets each object's id. For the migration path only."""
    from django.db import connections

    if not objs:
        return objs
    alias = model.objects.db
    connection = connections[alias]
    table = model._meta.db_table
    fields = [f for f in model._meta.concrete_fields if not f.primary_key and (table, f.column) not in skip]
    params = [f.get_db_prep_save(f.pre_save(obj, True), connection) for obj in objs for f in fields]
    row = "(" + ", ".join(["%s"] * len(fields)) + ")"
    with connection.cursor() as cursor:
        cursor.execute(f'INSERT INTO "{model._meta.db_table}" ({", ".join(f.column for f in fields)}) '
                       f"VALUES {', '.join([row] * len(objs))} RETURNING id", params)
        for obj, (new_id,) in zip(objs, cursor.fetchall()):
            obj.id = obj.pk = new_id
    return objs


def copy_specialty(clinic_id, section, language="ar"):
    """Add the section's specialty procedures, medicines and prescription templates it doesn't have yet.

    Runs inside the clinic's transaction (directory.flows.ensure_clinic_on_shard). Never deletes or changes a row
    the clinic has (design 5.8), so running it again is harmless. Both languages are copied (design G3).
    """
    from clinics.catalogue.english import english
    from clinics.specialties import SPECIALTIES
    from core.routing import current_scope

    from .models import Medicine, PrescriptionTemplate, PrescriptionTemplateItem, Procedure

    spec = SPECIALTIES.get(section.specialty_code)
    if spec is None:
        return
    skip = _missing_english(current_scope().alias)

    def create(model, objs):
        return _insert(model, objs, skip) if skip else model.objects.bulk_create(objs)

    have = set(Procedure.objects.filter(clinic_id=clinic_id, section_id=section.id, is_active=True)
               .values_list("name_ar", flat=True))
    create(Procedure, [
        Procedure(clinic_id=clinic_id, section_id=section.id, name_ar=p["name_ar"], name_en=p["name_en"],
                  category=p["category"], unit=p["unit"], price=p["price"], typical_min=p["typical_min"],
                  typical_max=p["typical_max"], description=p.get("description", ""),
                  description_en=p.get("description_en", ""), condition=p["condition"],
                  follow_up=p.get("follow_up", []), sort=sort)
        for sort, p in enumerate(spec["procedures"]) if p["name_ar"] not in have])
    have = set(Medicine.objects.filter(clinic_id=clinic_id, section_id=section.id, is_active=True)
               .values_list("name", "strength"))
    create(Medicine, [
        Medicine(clinic_id=clinic_id, section_id=section.id, name=m["name"], generic=m["generic"], strength=m["strength"],
                 form=m["form"], default_dose=m["default_dose"], default_note=m["default_note"], form_en=m["form_en"],
                 default_dose_en=m["default_dose_en"], default_note_en=m["default_note_en"], sort=sort)
        for sort, m in enumerate(spec["medicines"]) if (m["name"], m["strength"]) not in have])
    medicines = {}
    library = Medicine.objects.filter(clinic_id=clinic_id, section_id=section.id, is_active=True).order_by("id")
    for medicine in library.only("id", "name", "strength", "form", "default_dose", "default_note"):
        medicines.setdefault((medicine.name, medicine.strength), medicine)
        medicines.setdefault((medicine.name, ""), medicine)
    have = set(PrescriptionTemplate.objects.filter(clinic_id=clinic_id, section_id=section.id, is_active=True)
               .values_list("name", flat=True))
    for name, lines, notes in spec["templates"]:
        if name in have:
            continue
        template = create(PrescriptionTemplate, [PrescriptionTemplate(
            clinic_id=clinic_id, section_id=section.id, name=name, name_en=english(name), notes=notes[:1000],
            notes_en=english(notes)[:1000])])[0]
        items = []
        for position, (trade_name, strength) in enumerate(lines, start=1):
            medicine = medicines.get((trade_name, strength)) or medicines.get((trade_name, ""))
            items.append(PrescriptionTemplateItem(
                clinic_id=clinic_id, template=template, position=position, medicine=medicine, name=trade_name,
                strength=medicine.strength if medicine else strength, form=medicine.form if medicine else "",
                dose=medicine.default_dose if medicine else "", note=medicine.default_note if medicine else ""))
        PrescriptionTemplateItem.objects.bulk_create(items)


# --- Medicines library (design E3) -----------------------------------------------------------------------------

MEDICINE_FIELDS = {"name": 120, "generic": 200, "strength": 60, "form": 60, "default_dose": 200, "default_note": 200,
                   "form_en": 60, "default_dose_en": 200, "default_note_en": 200}
MEDICINE_COLUMNS = ("id, section_id, name, generic, strength, form, default_dose, default_note, is_active, sort, "
                    "form_en, default_dose_en, default_note_en")
MEDICINE_TEXT = ("form", "default_dose", "default_note")  # shown in the request's language (design G3)


def clean_medicine(data, partial=False):
    values = {}
    for name, size in MEDICINE_FIELDS.items():
        if not partial or name in data:
            values[name] = text(data, name, size, required=name == "name")
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ApiError("invalid", "is_active must be true or false", fields={"is_active": "boolean"})
        values["is_active"] = data["is_active"]
    return values


def _duplicate_medicine():
    return ApiError("duplicate", _("This medicine is already in the list."), 409, fields={"name": "duplicate"})


def medicine_row(values):
    """The row with form, dose and note in the request's language, and both raw texts (`<field>_ar`, `<field>_en`)."""
    from core.i18n import pick

    row = dict(zip(("id", "section_id", "name", "generic", "strength", "form", "default_dose", "default_note",
                    "is_active", "sort", "form_en", "default_dose_en", "default_note_en"), values))
    for name in MEDICINE_TEXT:
        row[f"{name}_ar"] = row[name]
        row[name] = pick(row[name], row[f"{name}_en"])
    return row


def create_medicine(ctx, section, values):
    found = _savepoint_insert(ctx, f"""
        INSERT INTO "clinical_medicine" (clinic_id, section_id, name, generic, strength, form, default_dose,
                                         default_note, form_en, default_dose_en, default_note_en, is_active, sort,
                                         created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, 1000, %s) RETURNING {MEDICINE_COLUMNS}""", [
        ctx.clinic_id, section["id"], values["name"], values.get("generic", ""), values.get("strength", ""),
        values.get("form", ""), values.get("default_dose", ""), values.get("default_note", ""),
        values.get("form_en", ""), values.get("default_dose_en", ""), values.get("default_note_en", ""), timezone.now()],
        {"medicine_active_name_uniq": _duplicate_medicine()})
    return medicine_row(found)


def update_medicine(ctx, medicine_id, values):
    """Absolute values (design 5.0 PATCH). A library row has no history: the prescriptions keep their copies."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    assignments = ", ".join(f"{name} = %s" for name in values)
    found = _savepoint_insert(ctx, f'UPDATE "clinical_medicine" SET {assignments} WHERE clinic_id = %s AND id = %s '
                                   f"RETURNING {MEDICINE_COLUMNS}", [*values.values(), ctx.clinic_id, medicine_id],
                              {"medicine_active_name_uniq": _duplicate_medicine()})
    if found is None:
        raise not_found("medicine")
    return medicine_row(found)


def add_medicine_from_rx(ctx, section_id, line):
    """A medicine typed in a prescription joins the section's library (design E3). An active row with the same name
    and strength is reused, so two doctors adding it at once make one row."""
    sql.run(ctx, """
        INSERT INTO "clinical_medicine" (clinic_id, section_id, name, generic, strength, form, default_dose,
                                         default_note, is_active, sort, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, 1000, %s)
        ON CONFLICT (clinic_id, section_id, name, strength) WHERE is_active DO NOTHING""", [
        ctx.clinic_id, section_id, line["name"], line["generic"], line["strength"], line["form"], line["dose"],
        line["note"], timezone.now()])
    found = sql.row(ctx, f'SELECT {MEDICINE_COLUMNS} FROM "clinical_medicine" WHERE clinic_id = %s AND section_id = %s '
                         "AND name = %s AND strength = %s AND is_active", [ctx.clinic_id, section_id, line["name"],
                                                                          line["strength"]])
    return medicine_row(found)


# --- Prescription templates (design E3) ------------------------------------------------------------------------


def clean_rx_lines(data, name="items", low=1):
    items = data.get(name)
    if not isinstance(items, list) or not low <= len(items) <= MAX_RX_ITEMS:
        raise ApiError("invalid", _("A prescription has 1 to 30 lines."), fields={name: "count"})
    lines = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError("invalid", "each line must be an object", fields={f"{name}.{index}": "object"})
        try:
            medicine_id = ident(item["medicine_id"], "medicine_id") if item.get("medicine_id") is not None else None
            line = {"medicine_id": medicine_id, "name": text(item, "name", 120, required=medicine_id is None),
                    "generic": text(item, "generic", 200, required=False),
                    "strength": text(item, "strength", 60, required=False),
                    "form": text(item, "form", 60, required=False), "dose": text(item, "dose", 200, required=False),
                    "note": text(item, "note", 200, required=False),
                    "add_to_library": bool(item.get("add_to_library") is True)}
        except ApiError as error:
            error.fields = {f"{name}.{index}.{field}": code for field, code in error.fields.items()}
            raise
        lines.append(line)
    return lines


def _fill_from_library(ctx, lines, section_id):
    """Lines that name a library medicine take its text where the client left it blank."""
    ids = sorted({line["medicine_id"] for line in lines if line["medicine_id"]})
    library = {}
    if ids:
        library = {r[0]: medicine_row(r) for r in sql.rows(
            ctx, f'SELECT {MEDICINE_COLUMNS} FROM "clinical_medicine" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
            [ctx.clinic_id, ids])}
    for index, line in enumerate(lines):
        if line["medicine_id"] is None:
            continue
        medicine = library.get(line["medicine_id"])
        if medicine is None:
            raise not_found("medicine")
        for field, source in (("name", "name"), ("generic", "generic"), ("strength", "strength"), ("form", "form"),
                              ("dose", "default_dose"), ("note", "default_note")):
            if not line[field]:
                line[field] = medicine.get(f"{source}_ar", medicine[source])  # the typed text, as before G3
    return lines


def create_template(ctx, section, name, notes, lines, name_en="", notes_en=""):
    lines = _fill_from_library(ctx, lines, section["id"])
    found = _savepoint_insert(ctx, """
        INSERT INTO "clinical_prescription_template" (clinic_id, section_id, name, notes, name_en, notes_en, is_active,
                                                      version, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, true, 1, %s) RETURNING id""",
        [ctx.clinic_id, section["id"], name, notes, name_en, notes_en, timezone.now()],
        {"rx_template_active_name_uniq": _duplicate_template()})
    _insert_template_items(ctx, found[0], lines)
    return found[0]


def _duplicate_template():
    return ApiError("duplicate", _("A template with this name already exists."), 409, fields={"name": "duplicate"})


def _insert_template_items(ctx, template_id, lines):
    values, params = [], []
    for position, line in enumerate(lines, start=1):
        values.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s)")
        params += [ctx.clinic_id, template_id, position, line["medicine_id"], line["name"], line["strength"],
                   line["form"], line["dose"], line["note"]]
    sql.run(ctx, 'INSERT INTO "clinical_prescription_template_item" (clinic_id, template_id, position, medicine_id, name, '
                 f"strength, form, dose, note) VALUES {', '.join(values)}", params)


def update_template(ctx, template_id, version, name, notes, lines, is_active=True, name_en="", notes_en=""):
    """Replace a template's name, notes and lines, conditional on the version it was read at (If-Match)."""
    current = sql.row(ctx, 'SELECT section_id, version, is_active FROM "clinical_prescription_template" '
                           "WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE", [ctx.clinic_id, template_id])
    if current is None:
        raise not_found("template")
    if current[1] != version:
        raise ApiError("stale", _("Someone changed this template meanwhile. Reload and try again."), 409,
                       fields={"version": current[1]})
    lines = _fill_from_library(ctx, lines, current[0])
    _savepoint_insert(ctx, 'UPDATE "clinical_prescription_template" SET name = %s, notes = %s, name_en = %s, '
                           "notes_en = %s, is_active = %s, version = version + 1 WHERE clinic_id = %s AND id = %s "
                           "RETURNING id", [name, notes, name_en, notes_en, is_active, ctx.clinic_id, template_id],
                      {"rx_template_active_name_uniq": _duplicate_template()})
    sql.run(ctx, 'DELETE FROM "clinical_prescription_template_item" WHERE clinic_id = %s AND template_id = %s',
            [ctx.clinic_id, template_id])
    _insert_template_items(ctx, template_id, lines)
    return template_id


def deactivate_template(ctx, template_id):
    found = sql.row(ctx, 'UPDATE "clinical_prescription_template" SET is_active = false, version = version + 1 '
                         "WHERE clinic_id = %s AND id = %s RETURNING id", [ctx.clinic_id, template_id])
    if found is None:
        raise not_found("template")


# --- Prescriptions (design 5.5, E3) ----------------------------------------------------------------------------


def allergy_terms(allergies):
    """The patient's recorded allergies as normalised terms: split on commas, semicolons, slashes and new lines."""
    raw = allergies or ""
    for mark in ("،", "؛", ";", "/", "\n", "+", "|"):
        raw = raw.replace(mark, ",")
    terms = []
    for part in raw.split(","):
        term = normalize_name(part)
        if len(term) >= 3 and term not in ("لا يوجد", "لايوجد", "none", "no", "nil", "nka", "nkda"):
            terms.append(term)
    return terms


def allergy_warnings(allergies, lines):
    """Each line whose name or active ingredient matches a recorded allergy (design E3). A warning, never a refusal:
    interaction checks need a drug database and come later."""
    terms = allergy_terms(allergies)
    warnings = []
    if not terms:
        return warnings
    for position, line in enumerate(lines, start=1):
        names = [normalize_name(line.get("name", "")), normalize_name(line.get("generic", ""))]
        words = {word for name in names for word in name.replace("+", " ").split() if len(word) >= 3}
        for term in terms:
            if any(name and (term in name or name in term) for name in names) or term in words:
                warnings.append({"position": position, "name": line.get("name", ""), "allergy": term})
                break
    return warnings


def create_prescription(ctx, patient_id, appointment_id, section_id, notes, lines):
    """1 to 30 lines; the number from the counter; one insert for the prescription and one for its lines (design 5.5).

    A line with `add_to_library` and no medicine joins the section's library (needs settings.medicines).
    Returns (prescription id, warnings).
    """
    patient = _patient(ctx, patient_id)
    doctor_id = ctx.membership_id
    if appointment_id is not None:
        appointment = _appointment(ctx, appointment_id)
        if appointment["patient_id"] != patient["id"]:
            raise not_found("appointment")
        _open_visit(appointment)
        section_id, doctor_id = appointment["section_id"], appointment["doctor_id"]
    elif section_id is None:
        raise ApiError("invalid", _("Pick the section."), fields={"section_id": "required"})
    else:
        _section(ctx, section_id)
    lines = _fill_from_library(ctx, lines, section_id)
    for line in lines:
        if line["medicine_id"] is None and line["add_to_library"]:
            ctx.require("settings.medicines")
            line["medicine_id"] = add_medicine_from_rx(ctx, section_id, line)["id"]
    number = take_numbers(ctx, "prescription")[0]
    now = timezone.now()
    prescription_id = sql.row(ctx, """
        INSERT INTO "clinical_prescription" (clinic_id, number, patient_id, appointment_id, doctor_id, section_id, notes,
                                             created_at, created_by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, number, patient["id"], appointment_id, doctor_id, section_id, notes, now, ctx.membership_id])[0]
    values, params = [], []
    for position, line in enumerate(lines, start=1):
        values.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
        params += [ctx.clinic_id, prescription_id, position, line["medicine_id"], line["name"], line["generic"],
                   line["strength"], line["form"], line["dose"], line["note"]]
    sql.run(ctx, 'INSERT INTO "clinical_prescription_item" (clinic_id, prescription_id, position, medicine_id, name, '
                 f"generic, strength, form, dose, note) VALUES {', '.join(values)}", params)
    return prescription_id, allergy_warnings(patient["allergies"], lines)


# --- Visit note (design 5.6, E3) -------------------------------------------------------------------------------

NOTE_FIELDS = {"complaint": 500, "diagnosis": 500, "notes": 5000}


def stale_note(current):
    return ApiError("stale", _("Someone changed this note meanwhile. Compare and save again."), 409,
                    fields={"version": current["version"]}, extra={"note": current})


def _note_row(values):
    return dict(zip(("id", "complaint", "diagnosis", "notes", "version", "updated_at"), values))


def save_note(ctx, appointment_id, version, values):
    """PUT appointments/{id}/note with If-Match: <version> (0 for a visit without a note yet).

    The update is conditional on the version; a mismatch is 409 with the current text. An edit after the visit's
    business day first copies the old text into a revision. Sending the text the note already has is a harmless
    repeat. Returns (note, changed).
    """
    appointment = _appointment(ctx, appointment_id)
    _open_visit(appointment)
    current = sql.row(ctx, 'SELECT id, complaint, diagnosis, notes, version, updated_at FROM "clinical_visit_note" '
                           "WHERE clinic_id = %s AND appointment_id = %s FOR NO KEY UPDATE", [ctx.clinic_id, appointment_id])
    now = timezone.now()
    if current is None:
        if version != 0:
            raise ApiError("stale", _("Someone changed this note meanwhile. Compare and save again."), 409,
                           fields={"version": 0}, extra={"note": None})
        found = sql.row(ctx, """
            INSERT INTO "clinical_visit_note" (clinic_id, appointment_id, patient_id, complaint, diagnosis, notes, version,
                                               created_at, updated_at, updated_by_id)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
            ON CONFLICT (clinic_id, appointment_id) DO NOTHING
            RETURNING id, complaint, diagnosis, notes, version, updated_at""", [
            ctx.clinic_id, appointment_id, appointment["patient_id"], values["complaint"], values["diagnosis"],
            values["notes"], now, now, ctx.membership_id])
        if found is None:  # another writer created it between our read and insert
            current = sql.row(ctx, 'SELECT id, complaint, diagnosis, notes, version, updated_at FROM "clinical_visit_note" '
                                   "WHERE clinic_id = %s AND appointment_id = %s", [ctx.clinic_id, appointment_id])
            raise stale_note(_note_row(current))
        return _note_row(found), True
    current = _note_row(current)
    if all(current[name] == values[name] for name in NOTE_FIELDS):
        return current, False
    if current["version"] != version:
        raise stale_note(current)
    if business_day(ctx.clinic) > appointment["day"]:
        sql.run(ctx, """
            INSERT INTO "clinical_visit_note_revision" (clinic_id, note_id, version, complaint, diagnosis, notes,
                                                        replaced_at, replaced_by_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", [
            ctx.clinic_id, current["id"], current["version"], current["complaint"], current["diagnosis"],
            current["notes"], now, ctx.membership_id])
    found = sql.row(ctx, 'UPDATE "clinical_visit_note" SET complaint = %s, diagnosis = %s, notes = %s, '
                         "version = version + 1, updated_at = %s, updated_by_id = %s "
                         "WHERE clinic_id = %s AND id = %s AND version = %s "
                         "RETURNING id, complaint, diagnosis, notes, version, updated_at", [
        values["complaint"], values["diagnosis"], values["notes"], now, ctx.membership_id, ctx.clinic_id, current["id"],
        version])
    return _note_row(found), True


# --- Vitals (design E3) ----------------------------------------------------------------------------------------

# name: (kind, low, high). Decimals keep one place.
VITALS = {
    "bp_systolic": ("int", 40, 300), "bp_diastolic": ("int", 20, 200), "pulse": ("int", 20, 300),
    "temperature": ("dec", Decimal("25"), Decimal("45")), "weight": ("dec", Decimal("0.3"), Decimal("400")),
    "height": ("dec", Decimal("20"), Decimal("260")), "spo2": ("int", 30, 100), "blood_sugar": ("int", 10, 1500),
}
VITALS_COLUMNS = "id, " + ", ".join(VITALS) + ", version, recorded_at, recorded_by_id"


def _decimal(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError("invalid", f"{name} must be a number", fields={name: "number"})
    from core.text import ascii_digits

    raw = ascii_digits(str(value)).strip().replace("٫", ".").replace(",", ".")
    try:
        number = Decimal(raw).quantize(Decimal("0.1"))
    except InvalidOperation:
        raise ApiError("invalid", f"{name} must be a number", fields={name: "number"})
    if not number.is_finite() or not low <= number <= high:
        raise ApiError("invalid", f"{name} must be between {low} and {high}", fields={name: "range"})
    return number


def clean_vitals(data):
    """Every vital is optional; a value out of its range is 400 with the field named. Blood pressure takes both."""
    values = {}
    for name, (kind, low, high) in VITALS.items():
        raw = data.get(name)
        if raw is None or raw == "":
            values[name] = None
        elif kind == "int":
            values[name] = parse_int(raw, name, low, high)
        else:
            values[name] = _decimal(raw, name, low, high)
    if (values["bp_systolic"] is None) != (values["bp_diastolic"] is None):
        missing = "bp_diastolic" if values["bp_diastolic"] is None else "bp_systolic"
        raise ApiError("invalid", _("Enter both blood pressure numbers."), fields={missing: "required"})
    if values["bp_systolic"] is not None and values["bp_diastolic"] >= values["bp_systolic"]:
        raise ApiError("invalid", _("Check the blood pressure numbers."), fields={"bp_diastolic": "range"})
    return values


def vitals_row(values):
    if values is None:
        return None
    names = ("id", *VITALS, "version", "recorded_at", "recorded_by_id")
    row = dict(zip(names, values))
    for name, (kind, _low, _high) in VITALS.items():
        if kind == "dec" and row[name] is not None:
            row[name] = str(row[name])
    return row


def save_vitals(ctx, appointment_id, version, values):
    """PUT appointments/{id}/vitals with If-Match (0 for none yet): absolute values, conditional on the version."""
    appointment = _appointment(ctx, appointment_id)
    _open_visit(appointment)
    now = timezone.now()
    names = list(VITALS)
    if version == 0:
        found = sql.row(ctx, f"""
            INSERT INTO "clinical_vitals" (clinic_id, appointment_id, patient_id, {', '.join(names)}, version, recorded_at,
                                           recorded_by_id)
            VALUES (%s, %s, %s, {', '.join(['%s'] * len(names))}, 1, %s, %s)
            ON CONFLICT (clinic_id, appointment_id) DO NOTHING RETURNING {VITALS_COLUMNS}""", [
            ctx.clinic_id, appointment_id, appointment["patient_id"], *[values[n] for n in names], now, ctx.membership_id])
    else:
        found = sql.row(ctx, f'UPDATE "clinical_vitals" SET {", ".join(f"{n} = %s" for n in names)}, '
                             "version = version + 1, recorded_at = %s, recorded_by_id = %s "
                             f"WHERE clinic_id = %s AND appointment_id = %s AND version = %s RETURNING {VITALS_COLUMNS}",
                        [*[values[n] for n in names], now, ctx.membership_id, ctx.clinic_id, appointment_id, version])
    if found is None:
        current = sql.row(ctx, f'SELECT {VITALS_COLUMNS} FROM "clinical_vitals" WHERE clinic_id = %s AND appointment_id = %s',
                          [ctx.clinic_id, appointment_id])
        current = vitals_row(current)
        if current is not None and all(str(current[n]) == str(values[n]) if values[n] is not None else current[n] is None
                                       for n in names):
            return current, False  # already so: a harmless repeat
        raise ApiError("stale", _("Someone changed the vital signs meanwhile. Reload and try again."), 409,
                       fields={"version": current["version"] if current else 0}, extra={"vitals": current})
    return vitals_row(found), True


# --- Attachments (design E3) -----------------------------------------------------------------------------------


def add_attachment(ctx, patient_id, appointment_id, title, upload):
    """Store the file through the storage class, then its row. The type comes from the bytes, never the client."""
    from .files import MAX_BYTES, sniff, storage

    patient = _patient(ctx, patient_id)
    if appointment_id is not None:
        appointment = _appointment(ctx, appointment_id)
        if appointment["patient_id"] != patient["id"]:
            raise not_found("appointment")
    if upload is None:
        raise ApiError("invalid", _("Choose a file."), fields={"file": "required"})
    if upload.size <= 0 or upload.size > MAX_BYTES:
        raise ApiError("too_large", _("The file is larger than 10 MB."), 413, fields={"file": "too_large"})
    head = upload.read(16)
    upload.seek(0)
    content_type = sniff(head)
    if content_type is None:
        raise ApiError("file_type", _("Only images (JPEG, PNG, WebP) and PDF files can be attached."), 415,
                       fields={"file": "type"})
    file_name = "".join(ch for ch in (upload.name or "file") if ch.isprintable() and ch not in '\\/"<>')[-200:] or "file"
    store = storage()
    key, size, digest = store.save(ctx.clinic_id, upload.chunks())
    try:
        found = sql.row(ctx, """
            INSERT INTO "clinical_attachment" (clinic_id, patient_id, appointment_id, title, file_name, content_type, size,
                                               sha256, storage_key, created_at, created_by_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
            ctx.clinic_id, patient["id"], appointment_id, title, file_name, content_type, size, digest, key,
            timezone.now(), ctx.membership_id])
    except Exception:
        store.delete(key)
        raise
    # A rollback after this point (a later refusal in the request) leaves an unreferenced file; the storage key
    # starts with the clinic id, and a sweep can remove files no row names.
    return found[0]


# --- Procedure catalogue (design E3) ---------------------------------------------------------------------------

PROCEDURE_TEXT = {"name_ar": 160, "name_en": 160, "category": 40, "description": 500, "description_en": 500}
PROCEDURE_COLUMNS = ("id, section_id, name_ar, name_en, category, unit, price, typical_min, typical_max, description, "
                     "internal_cost, condition, is_active, sort, description_en, follow_up")


def procedure_row(values):
    """The raw columns, plus `name` and `description` in the request's language (design G3)."""
    from core.i18n import pick

    row = dict(zip([c.strip() for c in PROCEDURE_COLUMNS.split(",")], values))
    if isinstance(row["follow_up"], str):  # a raw cursor hands jsonb over as text
        import json

        row["follow_up"] = json.loads(row["follow_up"])
    row["name"] = pick(row["name_ar"], row["name_en"])
    row["description_ar"] = row["description"]
    row["description"] = pick(row["description_ar"], row["description_en"])
    return row


def clean_procedure(data, partial=False):
    values = {}
    for name, size in PROCEDURE_TEXT.items():
        if not partial or name in data:
            values[name] = text(data, name, size, required=name == "name_ar")
    if not partial or "unit" in data:
        values["unit"] = choice({"unit": data.get("unit") or "unit"}, "unit", UNITS)
    if not partial or "condition" in data:
        values["condition"] = choice({"condition": data.get("condition") or ""}, "condition", ["", *CONDITIONS])
    for name in ("price", "internal_cost"):
        if not partial or name in data:
            values[name] = parse_money(data.get(name) or 0, name)
    for name in ("typical_min", "typical_max"):
        if not partial or name in data:
            values[name] = parse_money(data[name], name) if data.get(name) not in (None, "") else None
    if "sort" in data:
        values["sort"] = parse_int(data["sort"], "sort", 0, 32000)
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ApiError("invalid", "is_active must be true or false", fields={"is_active": "boolean"})
        values["is_active"] = data["is_active"]
    if "follow_up" in data:
        from psycopg.types.json import Jsonb

        from .followups import clean_recipe

        values["follow_up"] = Jsonb(clean_recipe(data["follow_up"]))
    return values


def price_locked(ctx):
    """A doctor records at the clinic's list price (Saged, 2026-11-13): never another price, whatever was granted.
    The desk and the owner may still change it under billing.discount."""
    if ctx.membership is not None and ctx.membership.role == "doctor":
        raise ApiError("price_locked", _("The price comes from the clinic's price list."), 403,
                       fields={"unit_price": "locked"})


def _check_range(row):
    low, high = row.get("typical_min"), row.get("typical_max")
    if low is not None and high is not None and high < low:
        raise ApiError("invalid", _("The typical range is upside down."), fields={"typical_max": "range"})


def _duplicate_procedure():
    return ApiError("duplicate", _("This procedure is already in the list."), 409, fields={"name_ar": "duplicate"})


def _check_recipe(ctx, section_id, values):
    if "follow_up" in values:
        from .followups import check_recipe

        check_recipe(ctx, section_id, values["follow_up"].obj)


def create_procedure(ctx, section, values):
    _check_range(values)
    _check_recipe(ctx, section["id"], values)
    found = _savepoint_insert(ctx, f"""
        INSERT INTO "clinical_procedure" (clinic_id, section_id, name_ar, name_en, category, unit, price, typical_min,
                                          typical_max, description, description_en, internal_cost, condition,
                                          is_active, sort, created_at, follow_up)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s) RETURNING {PROCEDURE_COLUMNS}""", [
        ctx.clinic_id, section["id"], values["name_ar"], values["name_en"], values["category"], values["unit"],
        values["price"], values["typical_min"], values["typical_max"], values["description"],
        values.get("description_en", ""), values["internal_cost"],
        values["condition"], values.get("sort", 1000), timezone.now(), values.get("follow_up", "[]")],
        {"procedure_active_name_uniq": _duplicate_procedure(),
         "procedure_typical_range_valid": ApiError("invalid", _("The typical range is upside down."),
                                                   fields={"typical_max": "range"})})
    return procedure_row(found)


def update_procedure(ctx, procedure_id, values):
    """Absolute values. A price change goes to the change log with before and after (design 8)."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    before = sql.row(ctx, 'SELECT price, internal_cost, typical_min, typical_max, section_id FROM "clinical_procedure" '
                          "WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE", [ctx.clinic_id, procedure_id])
    if before is None:
        raise not_found("procedure")
    before = dict(zip(("price", "internal_cost", "typical_min", "typical_max", "section_id"), before))
    _check_recipe(ctx, before.pop("section_id"), values)
    _check_range({**{k: v for k, v in before.items() if k.startswith("typical")}, **values})
    assignments = ", ".join(f"{name} = %s" for name in values)
    found = _savepoint_insert(ctx, f'UPDATE "clinical_procedure" SET {assignments} WHERE clinic_id = %s AND id = %s '
                                   f"RETURNING {PROCEDURE_COLUMNS}", [*values.values(), ctx.clinic_id, procedure_id],
                              {"procedure_active_name_uniq": _duplicate_procedure(),
                               "procedure_typical_range_valid": ApiError("invalid", _("The typical range is upside down."),
                                                                         fields={"typical_max": "range"})})
    if found is None:
        raise not_found("procedure")
    row = procedure_row(found)
    money = [name for name in ("price", "internal_cost") if before[name] != row[name]]
    if money:
        record_change(ctx, "procedure.price", "procedure", procedure_id, before={n: before[n] for n in money},
                      after={n: row[n] for n in money})
    return row


def _procedure(ctx, procedure_id, active=True):
    found = sql.row(ctx, f'SELECT {PROCEDURE_COLUMNS} FROM "clinical_procedure" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, procedure_id])
    if found is None:
        raise not_found("procedure")
    row = procedure_row(found)
    if active and not row["is_active"]:
        raise ApiError("invalid", _("This procedure isn't offered any more."), fields={"procedure_id": "inactive"})
    return row


# --- The dental chart (design E3) ------------------------------------------------------------------------------


def clean_tooth(value, name="tooth"):
    number = parse_int(value, name, 11, 85)
    if number not in TEETH:
        raise ApiError("invalid", _("Use an FDI tooth number: 11-48 or 51-85."), fields={name: "tooth"})
    return number


def clean_surfaces(value, condition=None, name="surfaces"):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = list(value.upper())
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise ApiError("invalid", "surfaces must be a list of M, O, D, B and L", fields={name: "list"})
    surfaces = [s.upper() for s in value]
    if any(s not in SURFACES for s in surfaces) or len(set(surfaces)) != len(surfaces):
        raise ApiError("invalid", _("Surfaces are M, O, D, B and L, each once."), fields={name: "choice"})
    if surfaces and condition is not None and condition not in SURFACE_CONDITIONS:
        raise ApiError("invalid", _("Only caries and fillings take surfaces."), fields={name: "not_allowed"})
    return [s for s in SURFACES if s in surfaces]  # always in M, O, D, B, L order


def tooth_row(values):
    return dict(zip(("id", "tooth", "condition", "surfaces", "note", "updated_at"), values))


def _set_tooth(ctx, patient_id, tooth, condition, surfaces, note, appointment_id=None, procedure_id=None,
               visit_procedure_id=None):
    """The tooth's state now (an upsert, which locks the row) and its history row."""
    now = timezone.now()
    found = sql.row(ctx, """
        INSERT INTO "clinical_tooth" AS t (clinic_id, patient_id, tooth, condition, surfaces, note, updated_at, updated_by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (clinic_id, patient_id, tooth) DO UPDATE SET condition = EXCLUDED.condition,
               surfaces = EXCLUDED.surfaces, note = EXCLUDED.note, updated_at = EXCLUDED.updated_at,
               updated_by_id = EXCLUDED.updated_by_id
        RETURNING id, tooth, condition, surfaces, note, updated_at""", [
        ctx.clinic_id, patient_id, tooth, condition, surfaces, note[:200], now, ctx.membership_id])
    sql.run(ctx, """
        INSERT INTO "clinical_tooth_history" (clinic_id, patient_id, tooth, condition, surfaces, appointment_id, procedure_id,
                                              visit_procedure_id, note, at, by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", [
        ctx.clinic_id, patient_id, tooth, condition, surfaces, appointment_id, procedure_id, visit_procedure_id, note[:500],
        now, ctx.membership_id])
    return tooth_row(found)


def set_tooth(ctx, patient_id, tooth, condition, surfaces, note, appointment_id=None, expected=None):
    """PUT patients/{id}/teeth/{tooth}: an examination finding. `expected` is the condition the screen showed
    (None: the tooth had no row); a different state now is 409 with the current one."""
    patient = _patient(ctx, patient_id)
    if appointment_id is not None:
        appointment = _appointment(ctx, appointment_id)
        if appointment["patient_id"] != patient["id"]:
            raise not_found("appointment")
    current = sql.row(ctx, 'SELECT id, tooth, condition, surfaces, note, updated_at FROM "clinical_tooth" '
                           "WHERE clinic_id = %s AND patient_id = %s AND tooth = %s FOR NO KEY UPDATE",
                      [ctx.clinic_id, patient["id"], tooth])
    if current is not None and (current[2], list(current[3]), current[4]) == (condition, surfaces, note):
        return tooth_row(current), False
    if expected is not False and (current[2] if current else None) != expected:
        raise ApiError("stale", _("Someone changed this tooth meanwhile. Reload and try again."), 409,
                       fields={"condition": current[2] if current else None},
                       extra={"tooth": tooth_row(current) if current else None})
    return _set_tooth(ctx, patient["id"], tooth, condition, surfaces, note, appointment_id), True


# --- Recording a procedure in a visit (design E3) ---------------------------------------------------------------


def record_procedure(ctx, appointment_id, request):
    """POST appointments/{id}/procedures: an invoice line, the record, and the tooth's new state with its history.

    `request`: procedure_id, tooth, surfaces, area, qty, unit_price (None: the list or plan price), plan_item_id,
    note. A price other than the list's (or the plan item's) needs billing.discount, as any invoice line does.
    Returns (record, invoice, tooth state or None, version of the visit's day).
    """
    appointment = billing.lock_appointment(ctx, appointment_id)
    from scheduling.rules import check_scope

    procedure = _procedure(ctx, request["procedure_id"])  # another place's id is 404 before any other answer
    check_scope(ctx, appointment["section_id"], appointment["doctor_id"])
    status = appointment["status"]
    if status in ("booked", "cancelled", "no_show"):
        raise ApiError("visit_closed", _("Record procedures on a visit that has arrived."), 409, fields={"status": status})
    if procedure["section_id"] != appointment["section_id"]:
        raise ApiError("invalid", _("This procedure belongs to another section."), fields={"procedure_id": "section"})
    tooth, surfaces = request["tooth"], request["surfaces"]
    list_price = procedure["price"]
    plan_item = None
    if request["plan_item_id"] is not None:
        found = sql.row(ctx, """
            SELECT i.id, i.status, i.price, i.procedure_id, i.tooth FROM "clinical_plan_item" i
              JOIN "clinical_plan" p ON p.clinic_id = i.clinic_id AND p.id = i.plan_id
             WHERE i.clinic_id = %s AND i.id = %s AND p.patient_id = %s FOR NO KEY UPDATE OF i""",
                        [ctx.clinic_id, request["plan_item_id"], appointment["patient_id"]])
        if found is None:
            raise not_found("plan_item")
        plan_item = dict(zip(("id", "status", "price", "procedure_id", "tooth"), found))
        if plan_item["status"] not in PLAN_MOVES:
            raise ApiError("status_changed", _("This plan item is already done or dropped."), 409,
                           fields={"status": plan_item["status"]})
        if plan_item["procedure_id"] != procedure["id"]:
            raise ApiError("invalid", _("This plan item is for another procedure."), fields={"plan_item_id": "procedure"})
        if tooth is None:
            tooth = plan_item["tooth"]
        list_price = plan_item["price"]
    # The tooth is optional (Saged, 2026-11-13): the visit screen records in one tap; the chart always names it.
    if surfaces and tooth is None:
        raise ApiError("invalid", _("Pick the tooth."), fields={"tooth": "required"})
    if surfaces and procedure["condition"] and procedure["condition"] not in SURFACE_CONDITIONS:
        raise ApiError("invalid", _("Only caries and fillings take surfaces."), fields={"surfaces": "not_allowed"})
    qty = request["qty"]
    package = None
    if qty == 1 and request.get("use_package", True) and request["unit_price"] is None:
        # A prepaid session of this procedure (F7): the oldest package with sessions left, used at no charge.
        package = billing.use_session(ctx, appointment["patient_id"], procedure["id"])
    if package is not None:
        unit_price, from_list = 0, True
    else:
        unit_price = list_price if request["unit_price"] is None else request["unit_price"]
        from_list = unit_price == list_price
    if not from_list:
        price_locked(ctx)
        ctx.require("billing.discount")
    description = procedure["name_ar"] + (f" - {tooth}" if tooth else "") + (f" ({''.join(surfaces)})" if surfaces else "")
    if package is not None:
        description = f"{description} - {package['name']}"
    line_id, invoice = billing.add_visit_line(ctx, appointment, "procedure", description, qty, unit_price, from_list)
    now = timezone.now()
    record_id = sql.row(ctx, """
        INSERT INTO "clinical_visit_procedure" (clinic_id, appointment_id, patient_id, procedure_id, plan_item_id, tooth,
                                                surfaces, area, qty, unit_price, amount, invoice_line_id, note, created_at,
                                                created_by_id, patient_package_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, appointment_id, appointment["patient_id"], procedure["id"], plan_item["id"] if plan_item else None,
        tooth, surfaces, request["area"], qty, unit_price, qty * unit_price, line_id, request["note"], now,
        ctx.membership_id, package["id"] if package else None])[0]
    # Its materials come out of stock, and their average cost becomes the line's internal cost (F2).
    from inventory.services import deduct_materials

    deltas = Deltas()
    internal_cost = deduct_materials(ctx, procedure["id"], qty, patient_id=appointment["patient_id"],
                                     appointment_id=appointment_id, visit_procedure_id=record_id, deltas=deltas)
    if internal_cost:  # both rows in one statement (rows this flow just wrote: no new lock)
        sql.run(ctx, """
            WITH line AS (UPDATE "billing_invoice_line" SET internal_cost = %s WHERE clinic_id = %s AND id = %s)
            UPDATE "clinical_visit_procedure" SET internal_cost = %s WHERE clinic_id = %s AND id = %s""",
                [internal_cost, ctx.clinic_id, line_id, internal_cost, ctx.clinic_id, record_id])
    if plan_item is not None:
        sql.run(ctx, "UPDATE \"clinical_plan_item\" SET status = 'done', done_appointment_id = %s, updated_at = %s "
                     "WHERE clinic_id = %s AND id = %s", [appointment_id, now, ctx.clinic_id, plan_item["id"]])
    state = None
    if tooth is not None and procedure["condition"]:
        state = _set_tooth(ctx, appointment["patient_id"], tooth, procedure["condition"],
                           surfaces if procedure["condition"] in SURFACE_CONDITIONS else [], request["note"],
                           appointment_id, procedure["id"], record_id)
    elif tooth is not None:
        # A procedure that sets no state still belongs in the tooth's history; its state stays as it was.
        current = sql.row(ctx, 'SELECT condition, surfaces FROM "clinical_tooth" WHERE clinic_id = %s AND patient_id = %s '
                               "AND tooth = %s", [ctx.clinic_id, appointment["patient_id"], tooth])
        sql.run(ctx, """
            INSERT INTO "clinical_tooth_history" (clinic_id, patient_id, tooth, condition, surfaces, appointment_id,
                                                  procedure_id, visit_procedure_id, note, at, by_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", [
            ctx.clinic_id, appointment["patient_id"], tooth, current[0] if current else "healthy",
            list(current[1]) if current else [], appointment_id, procedure["id"], record_id, request["note"][:500], now,
            ctx.membership_id])
    deltas.doctor_day(appointment["day"], appointment["doctor_id"], procedures=1).clinic_day(appointment["day"])
    deltas.write_doctors(ctx)
    version = deltas.write_clinics(ctx)[appointment["day"]]
    record = {"id": record_id, "appointment_id": appointment_id, "procedure_id": procedure["id"],
              "internal_cost": internal_cost, "patient_package_id": package["id"] if package else None,
              "name_ar": procedure["name_ar"], "name_en": procedure["name_en"], "tooth": tooth, "surfaces": surfaces,
              "area": request["area"], "qty": qty, "unit_price": unit_price, "amount": qty * unit_price,
              "invoice_line_id": line_id, "plan_item_id": plan_item["id"] if plan_item else None, "note": request["note"]}
    return record, invoice, state, version


# --- Treatment plans (design E3) -------------------------------------------------------------------------------


def clean_plan_items(ctx, items, section_id, name="items", low=1):
    if not isinstance(items, list) or not low <= len(items) <= MAX_PLAN_ITEMS:
        raise ApiError("invalid", _("A plan has 1 to 60 items."), fields={name: "count"})
    ids = set()
    cleaned = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError("invalid", "each item must be an object", fields={f"{name}.{index}": "object"})
        try:
            procedure_id = ident(item.get("procedure_id"), "procedure_id")
            cleaned.append({"procedure_id": procedure_id,
                            "tooth": clean_tooth(item["tooth"]) if item.get("tooth") not in (None, "") else None,
                            "area": text(item, "area", 20, required=False),
                            "price": parse_money(item["price"], "price") if item.get("price") not in (None, "") else None})
        except ApiError as error:
            error.fields = {f"{name}.{index}.{field}": code for field, code in error.fields.items()}
            raise
        ids.add(procedure_id)
    procedures = {r[0]: procedure_row(r) for r in sql.rows(
        ctx, f'SELECT {PROCEDURE_COLUMNS} FROM "clinical_procedure" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
        [ctx.clinic_id, sorted(ids)])}
    for index, item in enumerate(cleaned):
        procedure = procedures.get(item["procedure_id"])
        if procedure is None:
            raise not_found("procedure")
        if procedure["section_id"] != section_id or not procedure["is_active"]:
            raise ApiError("invalid", _("This procedure belongs to another section."),
                           fields={f"{name}.{index}.procedure_id": "section"})
        if procedure["unit"] == "tooth" and item["tooth"] is None:
            raise ApiError("invalid", _("Pick the tooth."), fields={f"{name}.{index}.tooth": "required"})
        if item["price"] is None:
            item["price"] = procedure["price"]
        elif item["price"] != procedure["price"]:
            price_locked(ctx)
            ctx.require("billing.discount")  # a price not from the list, as on an invoice line
    return cleaned


def _insert_plan_items(ctx, plan_id, items, first_position):
    values, params, now = [], [], timezone.now()
    for offset, item in enumerate(items):
        values.append("(%s, %s, %s, %s, %s, %s, %s, 'planned', %s)")
        params += [ctx.clinic_id, plan_id, item["procedure_id"], first_position + offset, item["tooth"], item["area"],
                   item["price"], now]
    sql.run(ctx, 'INSERT INTO "clinical_plan_item" (clinic_id, plan_id, procedure_id, position, tooth, area, price, '
                 f"status, updated_at) VALUES {', '.join(values)}", params)


def create_plan(ctx, patient_id, section, title, items):
    patient = _patient(ctx, patient_id)
    cleaned = clean_plan_items(ctx, items, section["id"])
    plan_id = sql.row(ctx, 'INSERT INTO "clinical_plan" (clinic_id, patient_id, section_id, title, created_at, created_by_id) '
                           "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                      [ctx.clinic_id, patient["id"], section["id"], title, timezone.now(), ctx.membership_id])[0]
    _insert_plan_items(ctx, plan_id, cleaned, 1)
    return plan_id


def add_plan_items(ctx, plan_id, items):
    """More items on a plan. The plan row is locked first, so two adds take positions one after the other."""
    from scheduling.rules import patient_sql

    mine, params = patient_sql(ctx, "pl.clinic_id", "pl.patient_id")  # another doctor's patient's plan is 404
    found = sql.row(ctx, f'SELECT pl.id, pl.section_id FROM "clinical_plan" pl WHERE pl.clinic_id = %s AND pl.id = %s{mine} '
                         "FOR NO KEY UPDATE", [ctx.clinic_id, plan_id, *params])
    if found is None:
        raise not_found("plan")
    count, last = sql.row(ctx, 'SELECT count(*), coalesce(max(position), 0) FROM "clinical_plan_item" '
                               "WHERE clinic_id = %s AND plan_id = %s", [ctx.clinic_id, plan_id])
    cleaned = clean_plan_items(ctx, items, found[1])
    if count + len(cleaned) > MAX_PLAN_ITEMS:
        raise ApiError("invalid", _("A plan has 1 to 60 items."), fields={"items": "count"})
    _insert_plan_items(ctx, plan_id, cleaned, last + 1)
    return plan_id


def move_plan_item(ctx, item_id, status, expected):
    """planned -> in_progress, done or dropped; in_progress -> planned, done or dropped. One conditional UPDATE on the
    status the screen showed decides, so a double click takes effect once and a stale screen gets 409."""
    if status not in PLAN_ITEM_STATUSES:
        raise ApiError("invalid", "status is not a plan item status", fields={"status": "choice"})
    from scheduling.rules import own_doctor, require_patient

    if own_doctor(ctx) is not None:  # another doctor's patient's plan item is 404 to a doctor
        owner = sql.row(ctx, 'SELECT pl.patient_id FROM "clinical_plan_item" i JOIN "clinical_plan" pl '
                             "ON pl.clinic_id = i.clinic_id AND pl.id = i.plan_id WHERE i.clinic_id = %s AND i.id = %s",
                        [ctx.clinic_id, item_id])
        if owner is None:
            raise not_found("plan_item")
        try:
            require_patient(ctx, owner[0])
        except ApiError:
            raise not_found("plan_item")
    found = sql.row(ctx, 'UPDATE "clinical_plan_item" SET status = %s, updated_at = %s WHERE clinic_id = %s AND id = %s '
                         "AND status = %s AND %s = ANY(%s) RETURNING plan_id",
                    [status, timezone.now(), ctx.clinic_id, item_id, expected, status,
                     sorted(PLAN_MOVES.get(expected, ()))])
    if found is not None:
        return found[0], True
    current = sql.row(ctx, 'SELECT plan_id, status FROM "clinical_plan_item" WHERE clinic_id = %s AND id = %s',
                      [ctx.clinic_id, item_id])
    if current is None:
        raise not_found("plan_item")
    if current[1] == status:
        return current[0], False  # already so: a harmless repeat
    if current[1] != expected:
        raise ApiError("status_changed", _("This plan item changed meanwhile."), 409, fields={"status": current[1]})
    raise ApiError("invalid_transition", _("This change doesn't apply to an item in that status."), 400,
                   fields={"status": "invalid"})
