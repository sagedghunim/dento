"""Changing the day summaries (design 5.0, 5.3 "counters each action changes").

Every flow writes all its changes to one summary table in one statement, rows in key
order: doctor-days by (day, doctor_id), clinic-days by day. Doctor-days come before
clinic-days in the global lock order, and clinic-days (the hottest rows) are written
last, just before commit. Every clinic-day a flow touches gets version + 1 and
`changed_since_recompute`.
"""

from collections import defaultdict

from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError

DOCTOR_FIELDS = ("booked", "done", "collected", "refunded")
CLINIC_FIELDS = ("done", "collected", "refunded", "expenses")


def _day_full():
    return ApiError("day_full", _("This doctor's day is fully booked."), 409)


class Deltas:
    """One flow's summary changes, so that each table gets one statement."""

    def __init__(self):
        self.doctor = defaultdict(lambda: dict.fromkeys(DOCTOR_FIELDS, 0))
        self.clinic = defaultdict(lambda: dict.fromkeys(CLINIC_FIELDS, 0))
        self.turn = None  # (day, doctor_id) that takes the next turn
        self.cap = None  # max_bookings of that doctor-day

    def doctor_day(self, day, doctor_id, **changes):
        """Changes to a doctor-day; with no changes, the row is only locked (slot moves within a doctor-day)."""
        row = self.doctor[(day, doctor_id)]
        for name, value in changes.items():
            row[name] += value
        return self

    def clinic_day(self, day, **changes):
        row = self.clinic[day]
        for name, value in changes.items():
            row[name] += value
        return self

    def take_turn(self, day, doctor_id, cap=None):
        """The next turn on this doctor-day and one more active booking (design 5.1 step 4)."""
        self.turn = (day, doctor_id)
        self.cap = cap
        return self.doctor_day(day, doctor_id, booked=1)

    def write_doctors(self, ctx):
        """One upsert for every doctor-day of the flow. Returns the new turn number, or None."""
        if not self.doctor:
            return None
        if self.turn is not None and self.cap is not None and self.cap <= 0:
            raise _day_full()
        keys = sorted(self.doctor)
        changes = {key: {"last_queue_no": 1 if key == self.turn else 0, **self.doctor[key]} for key in keys}
        where, where_params = "", []
        if self.turn is not None and self.cap is not None:
            # Only the row taking the turn is capped. A refused row stays locked, unchanged and unreturned.
            where, where_params = " WHERE (d.day, d.doctor_id) <> (%s, %s) OR d.booked < %s", [*self.turn, self.cap]
        returned = {(day, doctor_id): last for day, doctor_id, last in _upsert(
            ctx, "summaries_doctor_day", "d", ("day", "doctor_id"), ("last_queue_no", *DOCTOR_FIELDS), keys, changes,
            where=where, where_params=where_params, returning="day, doctor_id, last_queue_no")}
        if self.turn is None:
            return None
        if self.turn not in returned:
            raise _day_full()
        return returned[self.turn]

    def write_clinics(self, ctx):
        """One upsert for every clinic-day of the flow. Returns {day: new version}."""
        if not self.clinic:
            return {}
        keys = sorted(self.clinic)
        changes = {(day,): {"version": 1, **self.clinic[day]} for day in keys}
        return dict(_upsert(ctx, "summaries_clinic_day", "c", ("day",), ("version", *CLINIC_FIELDS), [(d,) for d in keys],
                            changes, insert_extra={"changed_since_recompute": "true"},
                            set_extra="changed_since_recompute = true", returning="day, version"))

    def write(self, ctx):
        """Both tables, in the global order. Returns (turn or None, {day: version})."""
        turn = self.write_doctors(ctx)
        return turn, self.write_clinics(ctx)


def bump(ctx, *days):
    """Version + 1 on these days only (arrive, call, line, discount, void, patient edit). Returns {day: version}."""
    deltas = Deltas()
    for day in days:
        deltas.clinic_day(day)
    return deltas.write_clinics(ctx)


def _upsert(ctx, table, alias, key_columns, fields, keys, changes, *, insert_extra=None, set_extra="", where="",
            where_params=(), returning=""):
    """INSERT ... ON CONFLICT DO UPDATE adding signed deltas, rows in key order, in one statement.

    PostgreSQL checks CHECK constraints on the proposed row before it finds the conflict, so a
    negative delta can't travel in VALUES (the counters are >= 0). Inserted values are the deltas
    clamped at zero; the update adds the true deltas, through EXCLUDED when none is negative and
    through a CASE on the key otherwise. A decrement only ever meets an existing row, made by the
    booking it undoes; the recompute repairs anything else.
    """
    insert_extra = insert_extra or {}
    columns = ["clinic_id", *key_columns, *fields, *insert_extra]
    row_sql = "(" + ", ".join(["%s"] * (1 + len(key_columns) + len(fields)) + list(insert_extra.values())) + ")"
    params = []
    for key in keys:
        params += [ctx.clinic_id, *key, *(max(changes[key][name], 0) for name in fields)]
    sets, set_params = [], []
    for name in fields:
        if all(changes[key][name] >= 0 for key in keys):
            sets.append(f"{name} = {alias}.{name} + EXCLUDED.{name}")
            continue
        branches = []
        for key in keys:
            if changes[key][name]:
                match = " AND ".join(f"{alias}.{column} = %s" for column in key_columns)
                branches.append(f"WHEN {match} THEN %s")
                set_params += [*key, changes[key][name]]
        sets.append(f"{name} = {alias}.{name} + CASE {' '.join(branches)} ELSE 0 END")
    if set_extra:
        sets.append(set_extra)
    statement = (f'INSERT INTO "{table}" AS {alias} ({", ".join(columns)}) VALUES {", ".join([row_sql] * len(keys))} '
                 f"ON CONFLICT (clinic_id, {', '.join(key_columns)}) DO UPDATE SET {', '.join(sets)}{where}")
    if returning:
        statement += f" RETURNING {returning}"
    return sql.rows(ctx, statement, [*params, *set_params, *where_params])
