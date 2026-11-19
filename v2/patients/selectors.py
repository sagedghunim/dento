"""Reading patient files: search (design 5.7), duplicate phones and the file."""

from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError
from core.text import ascii_digits, edit_distance, looks_like_phone, name_skeleton, normalize_name, normalize_phone

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


def _mine(ctx):
    """(sql, params): a doctor finds only their own patients (a visit with them, or a file they created), in the
    WHERE clause of each scan (Saged, 2026-11-13). Empty for everyone else."""
    from scheduling.rules import patient_sql

    mine, params = patient_sql(ctx, '"patients_patient".clinic_id', '"patients_patient".id')
    return ' AND "patients_patient".archived_at IS NULL' + mine, params  # a deleted file is gone (Saged, 2026-11-19)


def _search(ctx, query, today):
    query = (query or "").strip()
    mine, mine_params = _mine(ctx)
    if looks_like_phone(query):
        digits = "".join(ch for ch in ascii_digits(query) if ch.isdigit())
        if len(digits) < 2:
            return []
        sql.statement_timeout(ctx, SEARCH_TIMEOUT_MS)
        prefix = phone_prefix(query)
        file_no = int(digits) if len(digits) <= 15 else 0
        hits = sql.rows(ctx, f"""
            SELECT {_SUMMARY} FROM (
                (SELECT {_SUMMARY}, 0 AS rank FROM "patients_patient" WHERE clinic_id = %s AND file_no = %s{mine})
                UNION ALL
                (SELECT {_SUMMARY}, 1 AS rank FROM "patients_patient"
                  WHERE clinic_id = %s AND phone_norm >= %s AND phone_norm < %s{mine} ORDER BY phone_norm LIMIT %s)
            ) hits ORDER BY rank""", [ctx.clinic_id, file_no, *mine_params, ctx.clinic_id, prefix, _next_prefix(prefix),
                                      *mine_params, SEARCH_LIMIT])
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
                         f"WHERE clinic_id = %s AND name_norm >= %s AND name_norm < %s{mine} ORDER BY name_norm LIMIT %s",
                    [ctx.clinic_id, name, _next_prefix(name), *mine_params, SEARCH_LIMIT])
    if len(hits) < SEARCH_LIMIT:
        # 2. A later word starts with it: LIKE isn't leakproof, so this scans the clinic's part of the index.
        hits += sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" '
                              "WHERE clinic_id = %s AND name_norm LIKE %s ESCAPE '\\' AND NOT (id = ANY(%s::bigint[]))"
                              f"{mine} ORDER BY name_norm LIMIT %s",
                         [ctx.clinic_id, "% " + _escape_like(name) + "%", [hit[0] for hit in hits], *mine_params,
                          SEARCH_LIMIT - len(hits)])
    if len(hits) < SEARCH_LIMIT:
        hits += _with_a_slip(ctx, name_skeleton(name), [hit[0] for hit in hits], mine, mine_params,
                             SEARCH_LIMIT - len(hits))
    return _with_details(ctx, hits, today)


FUZZY_CANDIDATES = 200


def _with_a_slip(ctx, skeleton, seen, mine, mine_params, room):
    """Names typed with a slip (Saged, 2026-11-17), after the exact matches: two ranges on the skeleton index
    (patient_skel_search), which keep their index ranges under row-level security as pg_trgm's operators wouldn't.

    3. The name's skeleton starts with the query's: a missing or extra vowel or hamza, a doubled letter, a space
       ("عبد الله", "عبدالله").
    4. One more slip in any other letter (two in a long query): at most 200 names whose skeleton shares the
       query's first two letters, compared in Python. A slip in those first two letters isn't found.
    """
    if len(skeleton) < 2:
        return []
    found = sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" '
                          "WHERE clinic_id = %s AND name_skel >= %s AND name_skel < %s AND NOT (id = ANY(%s::bigint[]))"
                          f"{mine} ORDER BY name_skel LIMIT %s",
                     [ctx.clinic_id, skeleton, _next_prefix(skeleton), seen, *mine_params, room])
    if len(found) >= room or len(skeleton) < 3:
        return found
    seen = [*seen, *(row[0] for row in found)]
    head = skeleton[:2]
    candidates = sql.rows(ctx, f'SELECT {_SUMMARY}, name_skel FROM "patients_patient" '
                               "WHERE clinic_id = %s AND name_skel >= %s AND name_skel < %s "
                               f"AND NOT (id = ANY(%s::bigint[])){mine} ORDER BY name_skel LIMIT %s",
                          [ctx.clinic_id, head, _next_prefix(head), seen, *mine_params, FUZZY_CANDIDATES])
    limit = 2 if len(skeleton) >= 7 else 1
    scored = []
    for *summary, skel in candidates:
        # The typed part against the same length of the name, give or take the letters a slip adds or drops.
        distance = min(edit_distance(skeleton, skel[:size], limit)
                       for size in range(max(len(skeleton) - limit, 1), len(skeleton) + limit + 1))
        if distance <= limit:
            scored.append((distance, skel, tuple(summary)))
    scored.sort(key=lambda item: (item[0], item[1]))
    return found + [summary for _d, _s, summary in scored[:room - len(found)]]


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
    mine, mine_params = _mine(ctx)
    found = {r[0]: r for r in sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" WHERE clinic_id = %s '
                                            f"AND id = ANY(%s::bigint[]){mine}",
                                       [ctx.clinic_id, list(patient_ids), *mine_params])}
    return _with_details(ctx, [found[i] for i in patient_ids if i in found], today)


def by_phone(ctx, phone, today):
    """This clinic's files with this phone, for the new-patient step (R4.2; Saged, 2026-11-17): whose number it is
    on each, so the screen offers the file that owns it, or the guardian's children to pick from.

    `taken` says the number is some file's own even when a doctor can't see that file: a new file can't have it as
    its own (patients.services.check_phone). One statement on index patient_phone_search."""
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ApiError("invalid", _("Enter a valid mobile number."), fields={"phone": "phone"})
    mine, mine_params = _mine(ctx)
    visible = f"true{mine}" if mine else "true"
    rows = sql.rows(ctx, f'SELECT id, full_name, phone, file_no, phone_owner, guardian_relation, sex, birth_date, '
                         f'({visible}) FROM "patients_patient" WHERE clinic_id = %s AND phone_norm = %s '
                         "ORDER BY id LIMIT 20", [*mine_params, ctx.clinic_id, phone_norm])
    items = [{"id": r[0], "name": r[1], "phone": r[2], "file_no": r[3], "phone_owner": r[4], "guardian_relation": r[5],
              "sex": r[6], "age": age(r[7], today)} for r in rows if r[8]]
    return {"items": items, "taken": any(r[4] == "self" for r in rows)}


def _visible(ctx, patient_id):
    """This clinic's patient with this id, if the member may see it: a doctor only their own (Saged, 2026-11-13)."""
    from django.db.models import Exists, OuterRef, Q

    from scheduling.models import Appointment
    from scheduling.rules import own_doctor

    found = Patient.objects.filter(clinic_id=ctx.clinic_id, id=patient_id, archived_at__isnull=True)
    doctor = own_doctor(ctx)
    if doctor is not None:
        visits = Appointment.objects.filter(clinic_id=OuterRef("clinic_id"), patient_id=OuterRef("id"), doctor_id=doctor)
        found = found.filter(Q(created_by_id=ctx.user_id) | Exists(visits))
    return found


def get(ctx, patient_id):
    patient = _visible(ctx, patient_id).first()
    if patient is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    return patient


def exists(ctx, patient_id):
    if not _visible(ctx, patient_id).exists():
        raise ApiError("not_found", _("This patient wasn't found."), 404)


def file_payload(patient, today):
    return {
        "id": patient.id, "file_no": patient.file_no, "full_name": patient.full_name, "phone": patient.phone,
        "phone_owner": patient.phone_owner, "guardian_relation": patient.guardian_relation, "sex": patient.sex, "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
        "birth_date_estimated": patient.birth_date_estimated, "age": age(patient.birth_date, today),
        "address": patient.address, "notes": patient.notes, "allergies": patient.allergies,
        "chronic_conditions": patient.chronic_conditions,
    }


def summary_payload(patient):
    """What writes answer with: no file details beyond what the client sent (design 10, T9)."""
    return {"id": patient.id, "file_no": patient.file_no, "full_name": patient.full_name, "phone": patient.phone}


RECENT_LIMIT = 30


def recent(ctx, today):
    """The patients seen last, newest first, with how many finished visits: what the Patients page opens on (Saged,
    2026-11-17). A doctor gets their own (their finished visits, grouped); everyone else the clinic's patient
    stats, one row per patient. Then the usual details (one statement)."""
    from scheduling.rules import own_doctor

    doctor = own_doctor(ctx)
    if doctor is not None:
        rows = sql.rows(ctx, """
            SELECT a.patient_id, max(a.day) AS last_day, count(*) FROM "scheduling_appointment" a
             WHERE a.clinic_id = %s AND a.doctor_id = %s AND a.status = 'done'
             GROUP BY a.patient_id ORDER BY last_day DESC, a.patient_id DESC LIMIT %s""",
                        [ctx.clinic_id, doctor, RECENT_LIMIT])
    else:
        rows = sql.rows(ctx, 'SELECT patient_id, last_day, visits FROM "summaries_patient" WHERE clinic_id = %s '
                             "ORDER BY last_day DESC, patient_id DESC LIMIT %s", [ctx.clinic_id, RECENT_LIMIT])
    if not rows:
        return []
    found = {r[0]: r for r in sql.rows(ctx, f'SELECT {_SUMMARY} FROM "patients_patient" WHERE clinic_id = %s '
                                            "AND id = ANY(%s::bigint[])", [ctx.clinic_id, [r[0] for r in rows]])}
    items = _with_details(ctx, [found[r[0]] for r in rows if r[0] in found], today)
    counts = {r[0]: r[2] for r in rows}
    for item in items:
        item["visits"] = counts.get(item["id"], 0)
    return items
