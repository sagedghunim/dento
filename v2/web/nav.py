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
    Section("patients", _("Patients"), "/patients", ("patients.view",), True),
    Section("inventory", _("Inventory"), "/inventory", ("inventory.use", "inventory.manage"), False),       # M3
    Section("finances", _("Finances"), "/finances", ("finances.view", "expenses.manage"), False),           # M3
    Section("prescription", _("Prescription"), "/prescriptions", ("clinical.write",), False),              # M2
    Section("consults", _("Online consultations"), "/consults", ("consults.manage",), False),               # M3
    Section("team", _("Team & settings"), "/team", ("team.manage", "settings.manage"), False),              # M3
)


def visible(ctx):
    """The sections this person may see, in order."""
    return [section for section in SECTIONS if any(ctx.can(code) for code in section.codes)]
