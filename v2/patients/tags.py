"""Patient tags (design F6): a place's editable list (VIP, insurance, a referral source) and each patient's tags.

Lock order: key -> the tag row (an edit) ; key -> the patient's tag rows (replaced in one delete and one insert).
"""

import re

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.errors import ApiError

TAG_COLUMNS = "id, name, color, is_active, sort, name_en"
MAX_TAGS_PER_PATIENT = 20
BY_TAG_LIMIT = 20
COLOR = re.compile(r"^(#[0-9a-fA-F]{6})?$")


def _tag(values):
    from core.i18n import pick

    tag = dict(zip(("id", "name", "color", "is_active", "sort", "name_en"), values))
    tag["name_ar"] = tag["name"]
    tag["name"] = pick(tag["name_ar"], tag["name_en"] or "")  # design G3
    return tag


def not_found():
    return ApiError("not_found", _("This tag wasn't found."), 404)


def _duplicate():
    return ApiError("duplicate", _("This tag is already in the list."), 409, fields={"name": "duplicate"})


def _savepoint(ctx, statement, params):
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == "tag_active_name_uniq":
            raise _duplicate()
        raise


def check_color(value):
    if not isinstance(value, str) or not COLOR.match(value):
        raise ApiError("invalid", "color must be #RRGGBB or empty", fields={"color": "color"})
    return value


def tags(ctx, include_inactive=False):
    where = "" if include_inactive else " AND is_active"
    return [_tag(r) for r in sql.rows(ctx, f'SELECT {TAG_COLUMNS} FROM "patients_tag" WHERE clinic_id = %s{where} '
                                           "ORDER BY sort, name, id", [ctx.clinic_id])]


def get_tag(ctx, tag_id):
    found = sql.row(ctx, f'SELECT {TAG_COLUMNS} FROM "patients_tag" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, tag_id])
    if found is None:
        raise not_found()
    return _tag(found)


def create_tag(ctx, name, color="", name_en=""):
    return _tag(_savepoint(ctx, f'INSERT INTO "patients_tag" (clinic_id, name, name_en, color, is_active, sort) '
                                f"VALUES (%s, %s, %s, %s, true, 0) RETURNING {TAG_COLUMNS}",
                           [ctx.clinic_id, name, name_en, color]))


def update_tag(ctx, tag_id, values):
    """Absolute values for name, color, is_active, sort. Removing a tag deactivates it; patients keep the row."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    assignments = ", ".join(f"{name} = %s" for name in values)
    found = _savepoint(ctx, f'UPDATE "patients_tag" SET {assignments} WHERE clinic_id = %s AND id = %s '
                            f"RETURNING {TAG_COLUMNS}", [*values.values(), ctx.clinic_id, tag_id])
    if found is None:
        raise not_found()
    return _tag(found)


def patient_tags(ctx, patient_id):
    """The patient's active tags (one statement)."""
    return [_tag(r) for r in sql.rows(ctx, """
        SELECT t.id, t.name, t.color, t.is_active, t.sort, t.name_en FROM "patients_patient_tag" pt
          JOIN "patients_tag" t ON t.clinic_id = pt.clinic_id AND t.id = pt.tag_id
         WHERE pt.clinic_id = %s AND pt.patient_id = %s AND t.is_active ORDER BY t.sort, t.name, t.id""",
                                      [ctx.clinic_id, patient_id])]


def set_patient_tags(ctx, patient_id, tag_ids):
    """Replace the patient's tags with these (absolute, so a repeat is harmless). Inactive tags they carry stay.

    Each tag must be one of the place's active tags (404 otherwise). Audited: a tag can say "insurance".
    """
    if len(tag_ids) > MAX_TAGS_PER_PATIENT:
        raise ApiError("invalid", f"at most {MAX_TAGS_PER_PATIENT} tags", fields={"tag_ids": "too_many"})
    if sql.row(ctx, 'SELECT 1 FROM "patients_patient" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, patient_id]) is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    wanted = sorted(set(tag_ids))
    if wanted:
        found = {r[0] for r in sql.rows(ctx, 'SELECT id FROM "patients_tag" WHERE clinic_id = %s AND is_active '
                                             "AND id = ANY(%s::bigint[])", [ctx.clinic_id, wanted])}
        if len(found) != len(wanted):
            raise not_found()
    before = [r[0] for r in sql.rows(ctx, """
        DELETE FROM "patients_patient_tag" pt USING "patients_tag" t
         WHERE pt.clinic_id = %s AND pt.patient_id = %s AND t.clinic_id = pt.clinic_id AND t.id = pt.tag_id
           AND t.is_active AND NOT (pt.tag_id = ANY(%s::bigint[])) RETURNING pt.tag_id""",
                                         [ctx.clinic_id, patient_id, wanted])]
    added = []
    if wanted:
        now = timezone.now()
        added = [r[0] for r in sql.rows(ctx, f"""
            INSERT INTO "patients_patient_tag" (clinic_id, patient_id, tag_id, created_at, created_by_id)
            VALUES {", ".join(["(%s, %s, %s, %s, %s)"] * len(wanted))} ON CONFLICT DO NOTHING RETURNING tag_id""",
                                        [p for t in wanted for p in (ctx.clinic_id, patient_id, t, now,
                                                                      ctx.membership_id)])]
    if before or added:
        record_change(ctx, "patient.tags", "patient", patient_id, before={"removed": sorted(before)},
                      after={"added": sorted(added)})
    return patient_tags(ctx, patient_id)


def patients_with_tag(ctx, tag_id, after_id=None, size=BY_TAG_LIMIT):
    """Patient ids carrying a tag, by id, keyset (the patient_tag_by_tag index)."""
    get_tag(ctx, tag_id)
    where, params = "", []
    if after_id is not None:
        where, params = " AND patient_id > %s", [after_id]
    return [r[0] for r in sql.rows(ctx, f'SELECT patient_id FROM "patients_patient_tag" WHERE clinic_id = %s '
                                        f"AND tag_id = %s{where} ORDER BY patient_id LIMIT %s",
                                   [ctx.clinic_id, tag_id, *params, size])]


def keep_tagged(ctx, patient_ids, tag_id):
    """Of these patients, those carrying the tag (one statement)."""
    if not patient_ids:
        return set()
    return {r[0] for r in sql.rows(ctx, 'SELECT patient_id FROM "patients_patient_tag" WHERE clinic_id = %s '
                                        "AND tag_id = %s AND patient_id = ANY(%s::bigint[])",
                                   [ctx.clinic_id, tag_id, list(patient_ids)])}


# The tags a place starts with (Saged, 2026-11-19); the desk and the owner add their own.
DEFAULT_TAGS = (
    ("مميز VIP", "VIP", "#C8A400"),
    ("قايمة سودا", "Blocklist", "#C62828"),
    ("كتير الغياب", "Often misses visits", "#EF6C00"),
    ("بيدفع متأخر", "Pays late", "#AD1457"),
    ("تأمين", "Insurance", "#2E7D32"),
    ("إحالة من دكتور", "Referred by a doctor", "#6A1B9A"),
    ("من فيسبوك", "Facebook", "#1565C0"),
    ("قريب أو صاحب", "Friend or family", "#00838F"),
    ("بيخاف من العلاج", "Anxious patient", "#5D4037"),
)


def add_defaults(clinic_id, using):
    """Add the starting tags a place doesn't have yet (by name). Never changes or removes one."""
    from .models import Tag

    have = set(Tag.objects.using(using).filter(clinic_id=clinic_id).values_list("name", flat=True))
    Tag.objects.using(using).bulk_create([
        Tag(clinic_id=clinic_id, name=name, name_en=english, color=color, sort=sort)
        for sort, (name, english, color) in enumerate(DEFAULT_TAGS) if name not in have])
