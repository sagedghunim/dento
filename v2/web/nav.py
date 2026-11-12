"""The side navigation (requirements 1): the demo's sections, in its order, shown by permission.

This is the one list the whole app reads. A milestone switches its section on by setting `ready`;
until then the entry shows disabled with "Soon". A person sees an entry only if they hold one of
its codes; the server checks the codes again on every page and action.
"""

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    url: str
    codes: tuple  # any one of these shows the entry
    ready: bool   # built in the current milestone


SECTIONS = (
    Section("today", _("Today"), "/", ("appointments.book", "appointments.queue"), True),
    Section("overview", _("Overview"), "/overview", ("reports.view",), True),                              # M3
    Section("patients", _("Patients"), "/patients", ("patients.view",), True),
    Section("inventory", _("Inventory"), "/inventory", ("inventory.use", "inventory.manage"), True),        # M3
    Section("finances", _("Finances"), "/finances", ("finances.view", "expenses.manage", "cash.collect", "cash.shift"), True),  # M3
    Section("prescription", _("Prescription"), "/prescriptions", ("settings.medicines",), True),           # M2
    Section("consults", _("Online consultations"), "/consults", ("consults.manage",), False),               # M3
    Section("team", _("Team & settings"), "/team", ("team.manage", "settings.manage"), True),               # M3
)


# An owner who sees no patients of their own manages the place: their side bar is these, in this order.
MANAGER_SECTIONS = ("overview", "finances", "team", "patients")


def is_manager(ctx):
    """An owner with no doctor sessions (schedules) here: Overview is their home, Today and booking are hidden.

    An owner who also works as a doctor keeps Today first. One indexed lookup (schedule_session_uniq).
    """
    membership = getattr(ctx, "membership", None)
    if membership is None or getattr(membership, "role", None) != "owner":
        return False
    from clinics.models import Schedule

    return not Schedule.objects.filter(clinic_id=ctx.clinic_id, doctor_id=ctx.membership_id).exists()


def visible(ctx):
    """The sections this person may see, in order."""
    shown = [section for section in SECTIONS if any(ctx.can(code) for code in section.codes)]
    if is_manager(ctx):
        order = {key: i for i, key in enumerate(MANAGER_SECTIONS)}
        shown = sorted((s for s in shown if s.key in order), key=lambda s: order[s.key])
    return shown
