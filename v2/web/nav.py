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
    Section("followups", _("Follow-ups"), "/follow-ups", ("appointments.book",), True),                    # plans
    Section("overview", _("Overview"), "/overview", ("reports.view",), True),                              # M3
    Section("patients", _("Patients"), "/patients", ("patients.view",), True),
    Section("inventory", _("Inventory"), "/inventory", ("inventory.use", "inventory.manage"), True),        # M3
    Section("finances", _("Finances"), "/finances", ("finances.view", "expenses.manage", "cash.collect", "cash.shift"), True),  # M3
    Section("prescription", _("Prescription"), "/prescriptions", ("settings.medicines",), True),           # M2
    Section("consults", _("Online consultations"), "/consults", ("consults.manage",), False),               # M3
    # The owner's alerts (Saged, 2026-11-19): "owner" is not a code; visible() reads it as the owner role.
    Section("alerts", _("Alerts"), "/alerts", ("owner",), True),
    Section("team", _("Team & settings"), "/team", ("team.manage", "settings.manage", "procedures.record"), True),               # M3
)


# The owner's side bar (Saged, 2026-11-13), in this order. A solo owner sees every section, as SECTIONS orders them.
MANAGER_SECTIONS = ("overview", "alerts", "finances", "team", "patients")
TREATING_OWNER_SECTIONS = ("today", "prescription", "overview", "alerts", "finances", "team", "patients")


def owner_mode(ctx):
    """"solo", "doctor", "manager", or None for anyone but the owner: the one rule the side bar, the pages and the
    screens' permissions read (scheduling.rules.owner_mode)."""
    from scheduling.rules import owner_mode as mode

    return mode(ctx) if getattr(ctx, "membership", None) is not None else None


def is_manager(ctx):
    """The owner only manages the place: Overview is their home; Today, booking, walk-in and Prescription are hidden."""
    return owner_mode(ctx) == "manager"


def visible(ctx):
    """The sections this person may see, in order."""
    owner = getattr(ctx, "membership", None) is not None and ctx.membership.is_owner
    shown = [section for section in SECTIONS
             if any(ctx.can(code) or (code == "owner" and owner) for code in section.codes)]
    keys = {"manager": MANAGER_SECTIONS, "doctor": TREATING_OWNER_SECTIONS}.get(owner_mode(ctx))
    if keys:
        order = {key: i for i, key in enumerate(keys)}
        shown = sorted((s for s in shown if s.key in order), key=lambda s: order[s.key])
    return shown
