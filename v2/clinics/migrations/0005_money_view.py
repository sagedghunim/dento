# A doctor and money (Saged, 2026-11-16): the doctor's template no longer holds billing.collect, cash.collect or
# cash.shift, and no money code works without the new money.view (clinics.permissions.NEEDS_MONEY).
#
# Stored lists are relative to the template (granted = wanted - template, revoked = template - wanted), so a doctor's
# `granted` never held the three codes the template gave: every existing doctor loses them by the template change
# alone, with no write. The only doctors this migration touches are those the owner explicitly granted another money
# code (finances.view, billing.discount, ...): they get money.view too, so what they were given keeps working.
# Assistants and owners need nothing: the assistant's template now holds money.view and the owner holds every code.
# The code lists are frozen here, as they were when this migration was written.

from django.db import migrations

NEEDS_MONEY = frozenset({"billing.collect", "billing.discount", "billing.refund", "cash.collect", "cash.shift",
                         "expenses.manage", "packages.manage", "finances.view", "finances.pay"})


def lists(role, granted, revoked):
    """The doctor's new `granted`, or None when nothing changes."""
    if role != "doctor" or "money.view" in granted or not (set(granted) - set(revoked)) & NEEDS_MONEY:
        return None
    return sorted({*granted, "money.view"})


def forward(apps, schema_editor):
    """Row-level security is forced and a migration has no clinic set, so FORCE is lifted for this transaction (as in
    clinics 0002 and 0004). One read of the doctors with a non-empty `granted`, one write per changed doctor (few);
    the version moves so an open Team page reloads before it writes."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('ALTER TABLE "clinics_membership" NO FORCE ROW LEVEL SECURITY')
        cursor.execute("SELECT clinic_id, id, role, granted, revoked FROM clinics_membership "
                       "WHERE role = 'doctor' AND cardinality(granted) > 0")
        for clinic_id, member_id, role, granted, revoked in cursor.fetchall():
            new = lists(role, list(granted), list(revoked))
            if new is not None:
                cursor.execute("UPDATE clinics_membership SET granted = %s, version = version + 1 "
                               "WHERE clinic_id = %s AND id = %s", [new, clinic_id, member_id])
        cursor.execute('ALTER TABLE "clinics_membership" FORCE ROW LEVEL SECURITY')


class Migration(migrations.Migration):

    dependencies = [
        ("clinics", "0004_fill_english"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
