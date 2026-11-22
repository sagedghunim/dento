"""The desk books from the doctor's price list (Saged, 2026-11-22).

Every procedure of the price list has a booking service (`clinics_service.procedure_id`) with its name and price,
so a visit is booked as "Root canal, 2 canals" at the price the doctor records it at. `sync` keeps them in step and
runs after the price list changes (a procedure added, renamed, repriced or taken off, a section's catalogue copied).
It never deletes a service: a visit keeps the service it was booked on.

Raw SQL on the clinic's own connection, inside the caller's transaction.
"""

from django.db import connections

# 1. A procedure service of the same name, not yet tied to a procedure, becomes that procedure's.
ADOPT = """
    UPDATE "clinics_service" s SET procedure_id = p.id, price = p.price, name_en = p.name_en
      FROM "clinical_procedure" p
     WHERE s.clinic_id = %(clinic)s AND p.clinic_id = %(clinic)s AND s.section_id = p.section_id AND s.name = p.name_ar
       AND s.kind = 'procedure' AND s.procedure_id IS NULL AND p.is_active{only}
       AND NOT EXISTS (SELECT 1 FROM "clinics_service" m WHERE m.clinic_id = %(clinic)s AND m.procedure_id = p.id)"""

# 2. The services already tied follow their procedure; a new name is taken only when no other service has it.
FOLLOW = """
    UPDATE "clinics_service" s
       SET price = p.price, is_active = p.is_active, name_en = p.name_en,
           name = CASE WHEN EXISTS (SELECT 1 FROM "clinics_service" o WHERE o.clinic_id = %(clinic)s
                                       AND o.section_id = s.section_id AND o.name = p.name_ar AND o.id <> s.id)
                       THEN s.name ELSE p.name_ar END
      FROM "clinical_procedure" p
     WHERE s.clinic_id = %(clinic)s AND p.clinic_id = %(clinic)s AND p.id = s.procedure_id{only}
       AND (s.price, s.is_active, s.name, s.name_en) IS DISTINCT FROM (p.price, p.is_active, p.name_ar, p.name_en)"""

# 3. An active procedure without one gets one. A name another service already has is passed over.
ADD = """
    INSERT INTO "clinics_service" (clinic_id, section_id, name, name_en, kind, price, duration_min, is_active, sort,
                                   procedure_id)
    SELECT p.clinic_id, p.section_id, p.name_ar, p.name_en, 'procedure', p.price, 15, true,
           least(1000 + p.sort, 32000), p.id
      FROM "clinical_procedure" p
     WHERE p.clinic_id = %(clinic)s AND p.is_active{only}
       AND NOT EXISTS (SELECT 1 FROM "clinics_service" m WHERE m.clinic_id = %(clinic)s AND m.procedure_id = p.id)
    ON CONFLICT DO NOTHING"""


def sync(alias, clinic_id, procedure_id=None):
    """Bring the booking services of one clinic (or of one of its procedures) in step with the price list."""
    only = " AND p.id = %(procedure)s" if procedure_id is not None else ""
    params = {"clinic": clinic_id, "procedure": procedure_id}
    with connections[alias].cursor() as cursor:
        for statement in (ADOPT, FOLLOW, ADD):
            cursor.execute(statement.format(only=only), params)


# How many of a service the desk may book (Saged, 2026-11-22): it follows the price list's unit. Teeth, quadrants,
# arches and single units are counted (with their own ceiling); a whole case or a session is one. A procedure service
# not tied to the price list keeps the old count, 1 to 20.
COUNTED = {"tooth": 20, "quadrant": 4, "arch": 2, "unit": 20}


def count_limit(ctx, service_id, kind):
    """The most of this service one booking takes, or None when it is not counted."""
    from core import sql

    if kind != "procedure":
        return None
    found = sql.row(ctx, 'SELECT p.unit FROM "clinics_service" s LEFT JOIN "clinical_procedure" p '
                         'ON p.clinic_id = s.clinic_id AND p.id = s.procedure_id WHERE s.clinic_id = %s AND s.id = %s',
                    [ctx.clinic_id, service_id])
    if found is None or found[0] is None:
        return 20
    return COUNTED.get(found[0])
