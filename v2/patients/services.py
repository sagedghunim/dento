"""Creating and editing patient files (design 4.3, 5.7)."""

import datetime as dt

from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.errors import ApiError
from core.numbers import parse_int
from core.text import clean_name, is_valid_phone, normalize_name, normalize_phone
from core.validate import choice, text
from summaries.services import bump

from .models import Patient

FIELDS = ("full_name", "phone", "sex", "birth_date", "address", "notes", "allergies", "chronic_conditions")
# Identity edits go to the change log with before and after (design 8).
IDENTITY = ("full_name", "phone", "sex", "birth_date", "birth_date_estimated")


def _birth_date_from_age(age, today):
    try:
        return today.replace(year=today.year - age)
    except ValueError:  # 29 February
        return today.replace(year=today.year - age, day=28)


def clean_fields(data, today, *, partial=False):
    """Validate patient fields from a request body. Returns column values (only those sent, when partial)."""
    values = {}
    if not partial or "full_name" in data:
        name = clean_name(text(data, "full_name", 120))
        if len(name) < 2 or not normalize_name(name):
            raise ApiError("invalid", _("Enter the patient's name."), fields={"full_name": "required"})
        values["full_name"] = name
        values["name_norm"] = normalize_name(name)[:240]
    if not partial or "phone" in data:
        phone = text(data, "phone", 20, required=False)
        phone_norm = normalize_phone(phone)
        if phone and not is_valid_phone(phone_norm):
            raise ApiError("invalid", _("Enter a valid mobile number."), fields={"phone": "phone"})
        values["phone"] = phone_norm
        values["phone_norm"] = phone_norm
    if not partial or "sex" in data:
        values["sex"] = choice({"sex": data.get("sex") or ""}, "sex", ["m", "f", ""])
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
        values["birth_date"] = None
        values["birth_date_estimated"] = False
    for name, size in (("address", 200), ("notes", 2000), ("allergies", 1000), ("chronic_conditions", 1000)):
        if not partial or name in data:
            values[name] = text(data, name, size, required=False)
    return values


def create(ctx, values):
    """A new file with the next file number, in one statement (counter patient_file first in the lock order)."""
    now = timezone.now()
    row = sql.row(ctx, """
        WITH n AS (
            INSERT INTO "clinics_counter" AS k (clinic_id, name, value) VALUES (%s, 'patient_file', 1)
            ON CONFLICT (clinic_id, name) DO UPDATE SET value = k.value + 1 RETURNING value)
        INSERT INTO "patients_patient" (clinic_id, file_no, full_name, name_norm, phone, phone_norm, sex, birth_date,
            birth_date_estimated, address, notes, allergies, chronic_conditions, created_at, created_by_id, updated_at)
        SELECT %s, n.value, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s FROM n
        RETURNING id, file_no""", [
        ctx.clinic_id, ctx.clinic_id, values["full_name"], values["name_norm"], values["phone"], values["phone_norm"],
        values["sex"], values["birth_date"], values["birth_date_estimated"], values["address"], values["notes"],
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
