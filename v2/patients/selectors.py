"""Reading patient files: search (design 5.7), duplicate phones and the file."""

from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError
from core.text import ascii_digits, looks_like_phone, normalize_name, normalize_phone

from .models import Patient

SEARCH_LIMIT = 20
SEARCH_TIMEOUT_MS = 300
_SUMMARY = "id, full_name, phone, file_no"


def age(birth_date, today):
    if birth_date is None:
        return None
    years = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    return max(years, 0)


def _next_prefix(prefix):
    """The smallest string after every string that starts with `prefix`, under COLLATE "C"."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def phone_prefix(raw):
    """Typed digits as the start of a stored national number: +20, 0020 and 20 become 0."""
    text = ascii_digits(raw).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits.startswith("0020"):
        return "0" + digits[4:]
    if text.startswith("+20") or (digits.startswith("201") and len(digits) >= 4):
        return "0" + digits[2:]
    return digits


def search(ctx, query, today):
    """At most 20 files, as 1-2 index-only scans plus one statement for the details of what was found.

    The searches run under a 300 ms statement timeout (design 5.7). SET LOCAL would otherwise last for
    the rest of the request's transaction, so a successful search puts the role's timeout back. After a
    timeout the transaction is rolled back, which undoes the SET LOCAL with it.
    """
    found = _search(ctx, query, today)
    sql.statement_timeout(ctx, None)
    return found


def _search(ctx, query, today):
    query = (query or "").strip()
    if looks_like_phone(query):
        digits = "".join(ch for ch in ascii_digits(query) if ch.isdigit())
        if len(digits) < 2:
            return []
        sql.statement_timeout(ctx, SEARCH_TIMEOUT_MS)
        prefix = phone_prefix(query)
        file_no = int(digits) if len(digits) <= 15 else 0
        hits = sql.rows(ctx, f"""
            SELECT {_SUMMARY} FROM (
                (SELECT {_SUMMARY}, 0 AS rank FROM "patients_patient" WHERE clinic_id = %s AND file_no = %s)
                UNION ALL
                (SELECT {_SUMMARY}, 1 AS rank FROM "patients_patient"
                  WHERE clinic_id = %s AND phone_norm >= %s AND phone_norm < %s ORDER BY phone_norm LIMIT %s)
            ) hits ORDER BY rank""", [ctx.clinic_id, file_no, ctx.clinic_id, prefix, _next_prefix(prefix), SEARCH_LIMIT])
        seen, unique = set(), []
        for hit in hits:
            if hit[0] not in seen:
                seen.add(hit[0])
                unique.append(hit)
        return _with_details(ctx, unique[:SEARCH_LIMIT], today)
    name = normalize_name(query)
    if len(name) < 2:
        return []
    sql.statement_timeout(ctx, SEARCH_TIMEOUT_MS)
    # 1. The name starts with the query: a range, so it keeps its index range under row-level security.
    hits = sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" '
                         "WHERE clinic_id = %s AND name_norm >= %s AND name_norm < %s ORDER BY name_norm LIMIT %s",
                    [ctx.clinic_id, name, _next_prefix(name), SEARCH_LIMIT])
    if len(hits) < SEARCH_LIMIT:
        # 2. A later word starts with it: LIKE isn't leakproof, so this scans the clinic's part of the index.
        hits += sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" '
                              "WHERE clinic_id = %s AND name_norm LIKE %s ESCAPE '\\' AND NOT (id = ANY(%s::bigint[])) "
                              "ORDER BY name_norm LIMIT %s",
                         [ctx.clinic_id, "% " + _escape_like(name) + "%", [hit[0] for hit in hits],
                          SEARCH_LIMIT - len(hits)])
    return _with_details(ctx, hits, today)


def _with_details(ctx, hits, today):
    """Age, sex, any booking today and the last visit, for at most 20 files (one statement)."""
    if not hits:
        return []
    details = {row[0]: row[1:] for row in sql.rows(ctx, """
        SELECT p.id, p.sex, p.birth_date, p.birth_date_estimated, t.id, t.queue_no, t.doctor_id, t.status, l.day
          FROM "patients_patient" p
          LEFT JOIN LATERAL (
              SELECT a.id, a.queue_no, a.doctor_id, a.status FROM "scheduling_appointment" a
               WHERE a.clinic_id = p.clinic_id AND a.patient_id = p.id AND a.day = %s AND a.status <> 'cancelled'
               ORDER BY a.id LIMIT 1) t ON true
          LEFT JOIN LATERAL (
              SELECT a.day FROM "scheduling_appointment" a
               WHERE a.clinic_id = p.clinic_id AND a.patient_id = p.id AND a.day < %s AND a.status = 'done'
               ORDER BY a.day DESC, a.id DESC LIMIT 1) l ON true
         WHERE p.clinic_id = %s AND p.id = ANY(%s::bigint[])""", [today, today, ctx.clinic_id, [h[0] for h in hits]])}
    items = []
    for pid, full_name, phone, file_no in hits:
        sex, born, estimated, appt_id, queue_no, doctor_id, status, last_day = details.get(pid, (None,) * 8)
        items.append({
            "id": pid, "name": full_name, "phone": phone, "file_no": file_no, "sex": sex, "age": age(born, today),
            "today": {"appointment_id": appt_id, "queue_no": queue_no, "doctor_id": doctor_id, "status": status}
            if appt_id else None,
            "last_visit": last_day.isoformat() if last_day else None,
        })
    return items


def by_ids(ctx, patient_ids, today):
    """Search results for known ids, in that order (the tag filter, F6): two statements."""
    if not patient_ids:
        return []
    found = {r[0]: r for r in sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" WHERE clinic_id = %s '
                                            "AND id = ANY(%s::bigint[])", [ctx.clinic_id, list(patient_ids)])}
    return _with_details(ctx, [found[i] for i in patient_ids if i in found], today)


def by_phone(ctx, phone):
    """This clinic's files with this phone, for the Book dialog's duplicate step (R4.2). Never blocks."""
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ApiError("invalid", _("Enter a valid mobile number."), fields={"phone": "phone"})
    rows = sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" WHERE clinic_id = %s AND phone_norm = %s '
                         "ORDER BY id LIMIT 20", [ctx.clinic_id, phone_norm])
    return [{"id": r[0], "name": r[1], "phone": r[2], "file_no": r[3]} for r in rows]


def get(ctx, patient_id):
    patient = Patient.objects.filter(clinic_id=ctx.clinic_id, id=patient_id).first()
    if patient is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    return patient


def exists(ctx, patient_id):
    if not Patient.objects.filter(clinic_id=ctx.clinic_id, id=patient_id).exists():
        raise ApiError("not_found", _("This patient wasn't found."), 404)


def file_payload(patient, today):
    return {
        "id": patient.id, "file_no": patient.file_no, "full_name": patient.full_name, "phone": patient.phone,
        "sex": patient.sex, "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
        "birth_date_estimated": patient.birth_date_estimated, "age": age(patient.birth_date, today),
        "address": patient.address, "notes": patient.notes, "allergies": patient.allergies,
        "chronic_conditions": patient.chronic_conditions,
    }


def summary_payload(patient):
    """What writes answer with: no file details beyond what the client sent (design 10, T9)."""
    return {"id": patient.id, "file_no": patient.file_no, "full_name": patient.full_name, "phone": patient.phone}
