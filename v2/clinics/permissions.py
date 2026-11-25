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
    # The price list (Saged, 2026-11-22): the doctor keeps its procedures, the desk its prices.
    "procedures.manage": _("Add, change and take off procedures on the price list"),
    "prices.edit": _("Change the prices on the price list"),
    # A doctor has nothing to do with money unless the owner opens it (Saged, 2026-11-16).
    "money.view": _("See prices, bills and what patients paid or owe"),
    "discounts.suggest": _("Suggest one of the clinic's discounts on the visit"),
    "billing.collect": _("Take payments and add list-price lines"),
    "billing.discount": _("Give discounts"),
    "billing.refund": _("Refund payments and void invoices"),
    "cash.collect": _("Take cash into the register"),
    "cash.shift": _("Open and close the register and record cash in and out"),
    "inventory.use": _("Record stock use"),
    "inventory.manage": _("Manage items, purchases and stock counts"),
    # Suppliers and orders (design H1): the desk's, not the doctor's.
    "inventory.order": _("Prepare, send and receive purchase orders"),
    "finances.view": _("See the month's money and staff pay"),
    "finances.pay": _("Edit staff pay"),
    "expenses.manage": _("Add and void expenses"),
    "packages.manage": _("Edit session packages"),
    "patients.tags": _("Edit the patient tags list"),
    "patients.export": _("Export patient lists"),
    "reports.view": _("See clinic and staff performance and patient insights"),
    "consults.manage": _("Handle online consultation requests"),
    "settings.manage": _("Change the clinic profile, services, prices, schedules and medicines"),
    "team.manage": _("Manage members and permissions"),
    "audit.view": _("Read the change and access logs"),
}
ALL = frozenset(CODES)
# Codes that no longer exist but may stay in stored granted or revoked lists: ignored everywhere.
# appointments.others: a doctor never sees or acts on another doctor's patients (Saged, 2026-11-13).
RETIRED = frozenset({"appointments.others"})

# Only the owner gives or removes these (design 4.2).
OWNER_ONLY = frozenset({"team.manage", "billing.refund", "finances.view", "finances.pay", "audit.view",
                        "patients.export"})

# Codes that handle money: none of them works without money.view (Saged, 2026-11-16). effective_codes drops them for
# someone without it, and wanted_codes keeps the two together, so "no money anywhere" is one rule in one place.
MONEY_VIEW = "money.view"
NEEDS_MONEY = frozenset({"billing.collect", "billing.discount", "billing.refund", "cash.collect", "cash.shift",
                         "expenses.manage", "packages.manage", "finances.view", "finances.pay"})

TEMPLATES = {
    "owner": ALL,
    "doctor": frozenset({
        "appointments.book", "appointments.queue", "patients.view", "patients.edit", "clinical.view",
        "clinical.write", "inventory.use", "consults.manage",
        "visits.vitals", "visits.notes", "prescriptions.write", "settings.medicines", "procedures.record",
        # The doctor prices their own services on the price list and sees no other money (Saged, 2026-11-22);
        # the owner switches it off per doctor on Team.
        "procedures.manage", "prices.edit",
    }),
    # The desk reads the patient's file like the doctor (Saged, 2026-11-19): notes, prescriptions, plans and x-rays.
    # The owner can still switch it off per person on Team.
    "assistant": frozenset({
        "appointments.book", "appointments.queue", "patients.view", "patients.edit", "billing.collect", "inventory.use",
        "visits.vitals", "cash.collect", "cash.shift", "money.view", "clinical.view", "patients.tags", "prices.edit",
        "inventory.order", "inventory.manage",
    }),
}


def effective_codes(role, granted=(), revoked=()):
    """(template ∪ granted) − revoked, limited to known codes; the owner always has everything."""
    if role == "owner":
        return ALL
    codes = frozenset((TEMPLATES.get(role, frozenset()) | set(granted)) - set(revoked)) & ALL
    return codes if MONEY_VIEW in codes else codes - NEEDS_MONEY


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
    unknown = (set(new_granted) | set(new_revoked)) - ALL - RETIRED
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


# The "Who can do what" table (design G1): one row per side-navigation section, each a set of codes.
# Every code is in exactly one group (a test checks it).
GROUPS = (
    ("today", _("Today"), _("The day's list, booking and the queue"),
     ("appointments.book", "appointments.queue")),
    ("patients", _("Patients"), _("Patient files, tags and exports"),
     ("patients.view", "patients.edit", "patients.tags", "patients.export")),
    ("visit", _("The visit"), _("Clinical notes, vital signs, prescriptions, procedures and online consultations"),
     ("clinical.view", "clinical.write", "visits.vitals", "visits.notes", "prescriptions.write", "procedures.record",
      "procedures.manage", "consults.manage")),
    # A doctor's two money switches (Saged, 2026-11-16), both off in the doctor's template.
    ("prices", _("Sees prices and money"), _("Prices, bills, payments and the cash register"),
     ("money.view", "billing.collect", "cash.collect", "cash.shift")),
    # The price list's prices alone (Saged, 2026-11-22): a doctor's own services, without any other money.
    ("price_list", _("Sets the prices of their services"),
     _("Changes the prices of their own services on the price list; sees no other money"), ("prices.edit",)),
    ("suggest", _("Suggests discounts"), _("Suggest one of the clinic's discounts on the visit; the desk applies it"),
     ("discounts.suggest",)),
    ("money", _("Finances"), _("Discounts, refunds, expenses, packages, the month's money and staff pay"),
     ("billing.discount", "billing.refund", "expenses.manage", "packages.manage", "finances.view", "finances.pay")),
    ("stock", _("Inventory"), _("Stock use, items, purchases and counts"),
     ("inventory.use", "inventory.manage", "inventory.order")),
    ("reports", _("Reports"), _("Clinic and staff performance and patient insights"), ("reports.view",)),
    ("settings", _("Settings"), _("The clinic profile, services, prices, schedules and medicines"),
     ("settings.manage", "settings.medicines")),
    ("team", _("Team"), _("Members, permissions and the logs"), ("team.manage", "audit.view")),
)
GROUP_CODES = {key: frozenset(codes) for key, _label, _description, codes in GROUPS}


def group_state(codes, group_key):
    """"on" when the person holds every code of the group, "off" when none, else "partial"."""
    held = len(GROUP_CODES[group_key] & codes)
    return "on" if held == len(GROUP_CODES[group_key]) else "off" if held == 0 else "partial"


def wanted_codes(current, groups=None, codes=None):
    """The codes after switching whole groups, then single codes: {group: bool}, {code: bool}.

    Money codes and money.view go together (Saged, 2026-11-16): switching a money code on (its group or itself)
    switches money.view on, unless the same request switches the single code money.view off; without money.view
    every money code goes. Raises GrantRefused("unknown_group" / "unknown_code") for names it doesn't know.
    """
    wanted, opened = set(current), set()
    for key, on in (groups or {}).items():
        if key not in GROUP_CODES:
            raise GrantRefused("unknown_group", key)
        wanted = wanted | GROUP_CODES[key] if on else wanted - GROUP_CODES[key]
        if on:
            opened |= GROUP_CODES[key]
    for code, on in (codes or {}).items():
        if code not in ALL:
            raise GrantRefused("unknown_code", code)
        wanted = wanted | {code} if on else wanted - {code}
        if on:
            opened.add(code)
    if opened & NEEDS_MONEY and (codes or {}).get(MONEY_VIEW) is not False:
        wanted.add(MONEY_VIEW)
    if MONEY_VIEW not in wanted:
        wanted -= NEEDS_MONEY
    return frozenset(wanted)


def lists_for(role, wanted):
    """(granted, revoked) that give `role` exactly `wanted`, relative to its template, sorted."""
    template = TEMPLATES.get(role, frozenset())
    return sorted(wanted - template), sorted(template - wanted)


def check_add(actor, role):
    """A new member's role: never owner; a non-owner gives only codes they hold and no owner-only code (5.8)."""
    actor_codes = effective_codes(actor.role, actor.granted, actor.revoked)
    if "team.manage" not in actor_codes:
        raise GrantRefused("forbidden")
    if role not in ("doctor", "assistant"):
        raise GrantRefused("owner_fixed" if role == "owner" else "unknown_role")
    after = TEMPLATES[role]
    if actor.role == "owner":
        return after
    if after & OWNER_ONLY:
        raise GrantRefused("owner_only_code", ", ".join(sorted(after & OWNER_ONLY)))
    if not after <= actor_codes:
        raise GrantRefused("not_held", ", ".join(sorted(after - actor_codes)))
    return after
