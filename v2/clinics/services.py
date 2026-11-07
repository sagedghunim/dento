"""Clinic-level writes other apps need inside their own transaction (design 4.2)."""

from core import sql

COUNTERS = ("patient_file", "invoice", "prescription")  # also the lock order between them (design 5.0)


def take_numbers(ctx, name, count=1):
    """Gap-free numbers from a clinic counter, taken inside the transaction that uses them.

    The counter row stays locked until commit, and a rollback gives the numbers back.
    Returns the numbers as a list.
    """
    assert name in COUNTERS and count >= 1
    value = sql.row(ctx, 'INSERT INTO "clinics_counter" AS k (clinic_id, name, value) VALUES (%s, %s, %s) '
                         "ON CONFLICT (clinic_id, name) DO UPDATE SET value = k.value + EXCLUDED.value RETURNING value",
                    [ctx.clinic_id, name, count])[0]
    return list(range(value - count + 1, value + 1))
