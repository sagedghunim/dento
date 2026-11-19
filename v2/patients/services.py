"""Creating and editing patient files (design 4.3, 5.7).

A new file needs a three-part name, an Egyptian mobile, the sex and an age (Saged, 2026-11-17). The number is the
patient's own or a guardian's: one own number belongs to one file, a guardian's may be shared (siblings), and the
same name on the same number is always a duplicate. `check_phone` decides under a transaction-level advisory lock
on (clinic, number), so two desks registering at once can't both pass: the second reads the first's committed file.
"""

import datetime as dt

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.errors import ApiError
from core.numbers import parse_int
from core.text import clean_name, is_egypt_mobile, normalize_name, normalize_phone
from core.validate import text
from summaries.services import bump

from .models import Patient

FIELDS = ("full_name", "phone", "phone_owner", "guardian_relation", "sex", "birth_date", "address", "notes",
          "allergies", "chronic_conditions")
# Identity edits go to the change log with before and after (design 8).
IDENTITY = ("full_name", "phone", "phone_owner", "guardian_relation", "sex", "birth_date", "birth_date_estimated")
OWNERS = ("self", "guardian")
RELATIONS = ("father", "mother", "other")


def _birth_date_from_age(age, today):
    try:
        return today.replace(year=today.year - age)
    except ValueError:  # 29 February
        return today.replace(year=today.year - age, day=28)


def clean_fields(data, today, *, partial=False):
    """Validate patient fields from a request body. Returns column values (only those sent, when partial).

    A new file needs all of name (three parts), mobile, sex and age (Saged, 2026-11-17); an edit may leave a field
    out but never clears one of them or sets it to something a new file couldn't have.
    """
    values = {}
    if not partial or "full_name" in data:
        name = clean_name(text(data, "full_name", 120, required=False))
        if not name or not normalize_name(name):
            raise ApiError("invalid", _("Enter the patient's name."), fields={"full_name": "required"})
        if len(name.split()) < 3:
            raise ApiError("invalid", _("Enter the full three-part name."), fields={"full_name": "three_names"})
        values["full_name"] = name
        values["name_norm"] = normalize_name(name)[:240]
    if not partial or "phone" in data:
        phone_norm = normalize_phone(text(data, "phone", 20, required=False))
        if not phone_norm:
            raise ApiError("invalid", _("Enter the mobile number."), fields={"phone": "required"})
        if not is_egypt_mobile(phone_norm):
            raise ApiError("invalid", _("Enter a valid mobile number: 11 digits starting with 01."),
                           fields={"phone": "phone"})
        values["phone"] = phone_norm
        values["phone_norm"] = phone_norm
    if not partial or "phone_owner" in data or "guardian_relation" in data:
        owner = data.get("phone_owner") or "self"
        if owner not in OWNERS:
            raise ApiError("invalid", "phone_owner must be self or guardian", fields={"phone_owner": "choice"})
        relation = data.get("guardian_relation") or ""
        if owner == "guardian" and relation not in RELATIONS:
            raise ApiError("invalid", _("Whose number is it: the father, the mother or someone else?"),
                           fields={"guardian_relation": "required"})
        values["phone_owner"] = owner
        values["guardian_relation"] = relation if owner == "guardian" else ""
    if not partial or "sex" in data:
        if data.get("sex") not in ("m", "f"):
            raise ApiError("invalid", _("Pick male or female."), fields={"sex": "required"})
        values["sex"] = data["sex"]
    if "age" in data and data.get("age") not in (None, ""):
        values["birth_date"] = _birth_date_from_age(parse_int(data["age"], "age", 0, 130), today)
        values["birth_date_estimated"] = True
    elif "birth_date" in data and data.get("birth_date") not in (None, ""):
        try:
            born = dt.date.fromisoformat(str(data["birth_date"]))
        except ValueError:
            raise ApiError("invalid", "birth_date must be a date (YYYY-MM-DD)", fields={"birth_date": "date"})
        if not dt.date(1900, 1, 1) <= born <= today:
            raise ApiError("invalid", _("Check the date of birth."), fields={"birth_date": "range"})
        values["birth_date"] = born
        values["birth_date_estimated"] = False
    elif not partial or "birth_date" in data or "age" in data:
        raise ApiError("invalid", _("Enter the age."), fields={"age": "required"})
    for name, size in (("address", 200), ("notes", 2000), ("allergies", 1000), ("chronic_conditions", 1000)):
        if not partial or name in data:
            values[name] = text(data, name, size, required=False)
    return values


def _lock_phone(ctx, phone_norm):
    """Registrations and edits of one number in one clinic go one at a time (a transaction-level advisory lock).

    Only these flows take it: after any appointment, doctor or patient row lock and before the patient_file
    counter (design 5.0), and nothing waits for it while holding a lock taken after it, so it closes no cycle."""
    sql.row(ctx, "SELECT pg_advisory_xact_lock(hashtextextended('patient_phone:' || %s || ':' || %s, 0))",
            [ctx.clinic_id, phone_norm])


def _shown(ctx, row):
    """The file a refusal names, if the member may see it (a doctor sees only their own patients): else None."""
    from scheduling.rules import own_doctor, patient_sql

    if own_doctor(ctx) is not None:
        mine, params = patient_sql(ctx, "p.clinic_id", "p.id")
        if not sql.row(ctx, f'SELECT 1 FROM "patients_patient" p WHERE p.clinic_id = %s AND p.id = %s{mine}',
                       [ctx.clinic_id, row[0], *params]):
            return None
    return {"id": row[0], "name": row[1], "file_no": row[2], "phone": row[3]}


def check_phone(ctx, name_norm, phone_norm, owner, exclude_id=None):
    """Refuse a file that breaks the number rules (Saged, 2026-11-17), under the number's lock: the same name on
    the same number is a duplicate, whoever owns it; a number that is already a file's own can't be another's own."""
    _lock_phone(ctx, phone_norm)
    # A statement after the lock, so it sees a file committed while this one waited (index patient_phone_search).
    rows = sql.rows(ctx, 'SELECT id, full_name, file_no, phone, name_norm, phone_owner FROM "patients_patient" '
                         "WHERE clinic_id = %s AND phone_norm = %s AND id <> %s AND archived_at IS NULL ORDER BY id LIMIT 50",
                    [ctx.clinic_id, phone_norm, exclude_id or 0])
    same = next((r for r in rows if r[4] == name_norm), None)
    if same is not None:
        shown = _shown(ctx, same)
        message = (_("%(name)s already has a file with this number.") % {"name": same[1]} if shown
                   else _("This patient already has a file in this clinic."))
        raise ApiError("duplicate_patient", message, 409, fields={"full_name": "duplicate"}, extra={"patient": shown})
    own = next((r for r in rows if r[5] == "self"), None)
    if owner == "self" and own is not None:
        shown = _shown(ctx, own)
        message = (_("This number already has a file: %(name)s.") % {"name": own[1]} if shown
                   else _("This number already has a file in this clinic."))
        raise ApiError("phone_taken", message, 409, fields={"phone": "taken"}, extra={"patient": shown})


def create(ctx, values):
    """A new file with the next file number, in one statement (counter patient_file first in the lock order), once
    the number rules pass under the number's lock."""
    check_phone(ctx, values["name_norm"], values["phone_norm"], values["phone_owner"])
    now = timezone.now()
    row = sql.row(ctx, """
        WITH n AS (
            INSERT INTO "clinics_counter" AS k (clinic_id, name, value) VALUES (%s, 'patient_file', 1)
            ON CONFLICT (clinic_id, name) DO UPDATE SET value = k.value + 1 RETURNING value)
        INSERT INTO "patients_patient" (clinic_id, file_no, full_name, name_norm, phone, phone_norm, phone_owner,
            guardian_relation, sex, birth_date, birth_date_estimated, address, notes, allergies, chronic_conditions,
            created_at, created_by_id, updated_at)
        SELECT %s, n.value, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s FROM n
        RETURNING id, file_no""", [
        ctx.clinic_id, ctx.clinic_id, values["full_name"], values["name_norm"], values["phone"], values["phone_norm"],
        values["phone_owner"], values["guardian_relation"], values["sex"], values["birth_date"], values["birth_date_estimated"], values["address"], values["notes"],
        values["allergies"], values["chronic_conditions"], now, ctx.user_id, now])
    return {"id": row[0], "file_no": row[1], **values}


def update(ctx, patient_id, changes, expected, today):
    """Absolute values with the values they replace (design 5.0). Identity edits are audited; Today's version moves."""
    patient = Patient.objects.select_for_update(no_key=True).filter(clinic_id=ctx.clinic_id, id=patient_id).first()
    if patient is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    stale = [name for name in changes if name in expected and _comparable(expected[name]) != _comparable(getattr(patient, name))]
    if stale:
        raise ApiError("stale", _("Someone changed this patient meanwhile. Reload and try again."), 409,
                       fields={name: "changed" for name in stale})
    if {"full_name", "phone", "phone_owner"} & set(changes):
        check_phone(ctx, changes.get("name_norm", patient.name_norm), changes.get("phone_norm", patient.phone_norm),
                    changes.get("phone_owner", patient.phone_owner), exclude_id=patient.id)
    before = {name: _comparable(getattr(patient, name)) for name in changes}
    for name, value in changes.items():
        setattr(patient, name, value)
    patient.updated_at = timezone.now()
    Patient.objects.filter(clinic_id=ctx.clinic_id, id=patient.id).update(**changes, updated_at=patient.updated_at)
    identity = [name for name in changes if name in IDENTITY and before[name] != _comparable(changes[name])]
    if identity:
        record_change(ctx, "patient.identity", "patient", patient.id,
                      before={name: before[name] for name in identity},
                      after={name: _comparable(changes[name]) for name in identity})
    bump(ctx, today)  # names and phones show on Today (design 5.11)
    return patient


def _comparable(value):
    return value.isoformat() if isinstance(value, dt.date) else value


# --- Deleting a file (Saged, 2026-11-19): the owner only ---------------------------------------------------------

HISTORY_TABLES = ("scheduling_appointment", "billing_invoice", "billing_payment", "clinical_plan", "clinical_attachment",
                  "clinical_prescription", "clinical_tooth", "billing_installment_plan", "billing_patient_package")


def delete(ctx, patient_id):
    """A file with no history at all (a mistake, a double) goes for good; one with visits, bills or records is
    hidden everywhere instead, so the money and the records stay right, and the owner can bring it back.
    Returns "deleted" or "hidden". Audited."""
    if not ctx.membership.is_owner:
        raise ApiError("forbidden", _("Only the account's owner can delete a patient."), 403)
    row = sql.row(ctx, 'SELECT id, full_name, file_no FROM "patients_patient" WHERE clinic_id = %s AND id = %s '
                       "AND archived_at IS NULL FOR UPDATE", [ctx.clinic_id, patient_id])
    if row is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    has_history = any(sql.row(ctx, f'SELECT 1 FROM "{table}" WHERE clinic_id = %s AND patient_id = %s LIMIT 1',
                              [ctx.clinic_id, patient_id]) for table in HISTORY_TABLES)
    before = {"full_name": row[1], "file_no": row[2]}
    if not has_history:
        try:  # anything else still pointing at the file (an access log row, a follow-up): hidden instead
            with transaction.atomic(using=ctx.alias):
                sql.run(ctx, 'DELETE FROM "patients_patient_tag" WHERE clinic_id = %s AND patient_id = %s',
                        [ctx.clinic_id, patient_id])
                sql.run(ctx, 'DELETE FROM "patients_patient" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, patient_id])
            record_change(ctx, "patient.delete", "patient", patient_id, before=before)
            return "deleted"
        except IntegrityError:
            has_history = True
    if has_history:
        sql.run(ctx, 'UPDATE "patients_patient" SET archived_at = %s, archived_by_id = %s WHERE clinic_id = %s AND id = %s',
                [timezone.now(), ctx.membership_id, ctx.clinic_id, patient_id])
        record_change(ctx, "patient.hide", "patient", patient_id, before=before)
    return "hidden"


def restore(ctx, patient_id):
    """The owner brings a hidden file back."""
    if not ctx.membership.is_owner:
        raise ApiError("forbidden", _("Only the account's owner can delete a patient."), 403)
    found = sql.row(ctx, 'UPDATE "patients_patient" SET archived_at = NULL, archived_by_id = NULL WHERE clinic_id = %s '
                         "AND id = %s AND archived_at IS NOT NULL RETURNING full_name", [ctx.clinic_id, patient_id])
    if found is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    record_change(ctx, "patient.restore", "patient", patient_id, after={"full_name": found[0]})


def hidden(ctx, limit=50):
    """The owner's deleted files that can come back, newest first."""
    return [{"id": r[0], "name": r[1], "file_no": r[2], "at": r[3]} for r in sql.rows(ctx, """
        SELECT id, full_name, file_no, archived_at FROM "patients_patient" WHERE clinic_id = %s
           AND archived_at IS NOT NULL ORDER BY archived_at DESC LIMIT %s""", [ctx.clinic_id, limit])]
