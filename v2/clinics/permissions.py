"""Permission codes, role templates and the grant rules (design 4.2, 5.8)."""

from django.utils.translation import gettext_lazy as _

CODES = {
    "appointments.book": _("Book, reschedule and cancel"),
    "appointments.queue": _("Arrive, call in and finish"),
    "patients.view": _("Search and open patient details"),
    "patients.edit": _("Create and edit patient files"),
    "clinical.view": _("Read visit notes and prescriptions"),
    "clinical.write": _("Write visit notes and prescriptions"),
    # The visit (design E3).
    "visits.vitals": _("Record vital signs"),
    "visits.notes": _("Write the complaint and the visit notes"),
    "prescriptions.write": _("Write prescriptions"),
    "settings.medicines": _("Edit the medicines list and prescription templates"),
    "procedures.record": _("Record procedures, the dental chart and treatment plans"),
    "billing.collect": _("Take payments and add list-price lines"),
    "billing.discount": _("Give discounts and prices not from the list"),
    "billing.refund": _("Refund payments and void invoices"),
    "inventory.use": _("Record stock use"),
    "inventory.manage": _("Manage items, purchases and stock counts"),
    "finances.view": _("See the month's money and staff pay"),
    "finances.pay": _("Edit staff pay"),
    "expenses.manage": _("Add and void expenses"),
    "consults.manage": _("Handle online consultation requests"),
    "settings.manage": _("Change the clinic profile, services, prices, schedules and medicines"),
    "team.manage": _("Manage members and permissions"),
    "audit.view": _("Read the change and access logs"),
}
ALL = frozenset(CODES)

# Only the owner gives or removes these (design 4.2).
OWNER_ONLY = frozenset({"team.manage", "billing.refund", "finances.view", "finances.pay", "audit.view"})

TEMPLATES = {
    "owner": ALL,
    "doctor": frozenset({
        "appointments.book", "appointments.queue", "patients.view", "patients.edit", "clinical.view",
        "clinical.write", "billing.collect", "inventory.use", "consults.manage",
        "visits.vitals", "visits.notes", "prescriptions.write", "settings.medicines", "procedures.record",
    }),
    "assistant": frozenset({
        "appointments.book", "appointments.queue", "patients.view", "patients.edit", "billing.collect", "inventory.use",
        "visits.vitals",
    }),
}


def effective_codes(role, granted=(), revoked=()):
    """(template ∪ granted) − revoked, limited to known codes; the owner always has everything."""
    if role == "owner":
        return ALL
    return frozenset((TEMPLATES.get(role, frozenset()) | set(granted)) - set(revoked)) & ALL


class GrantRefused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def check_change(actor, target, new_role, new_granted, new_revoked):
    """Raise GrantRefused unless `actor` may change `target` to the new role and lists (design 5.8).

    `actor` and `target` are memberships of the same clinic.
    """
    actor_codes = effective_codes(actor.role, actor.granted, actor.revoked)
    if "team.manage" not in actor_codes:
        raise GrantRefused("forbidden")
    if actor.id == target.id:
        raise GrantRefused("not_self")
    if target.role == "owner" or new_role == "owner":
        # Ownership moves only through the platform admin (design 5.12).
        raise GrantRefused("owner_fixed")
    unknown = (set(new_granted) | set(new_revoked)) - ALL
    if unknown:
        raise GrantRefused("unknown_code", ", ".join(sorted(unknown)))
    before = effective_codes(target.role, target.granted, target.revoked)
    after = effective_codes(new_role, new_granted, new_revoked)
    if actor.role == "owner":
        return before, after
    if "team.manage" in before:
        raise GrantRefused("owner_only_target")
    changed = before ^ after  # additions and removals
    if changed & OWNER_ONLY:
        raise GrantRefused("owner_only_code", ", ".join(sorted(changed & OWNER_ONLY)))
    if not changed <= actor_codes:
        raise GrantRefused("not_held", ", ".join(sorted(changed - actor_codes)))
    return before, after
