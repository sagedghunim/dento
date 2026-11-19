"""The clinic settings registry (design 4.2): every policy a clinic may change, with its default."""

from django.utils.translation import gettext as _

from core.errors import ApiError

DEFAULTS = {
    "follow_up_days": 14,       # the follow-up window (R4.1, design 5.1)
    "digits": "latn",           # "latn" (0-9) or "arab" (٠-٩) on screens and prints
    "print_language": "ar",     # default language of receipts and prescriptions
    "require_mfa": False,       # two-step sign-in for every member, not only the owner
    "letterhead": False,        # pre-printed paper: no printed header
    "letterhead_top_mm": 0,
    "letterhead_bottom_mm": 0,
    "solo": False,              # a single clinic run by its owner alone (no assistant); never a center
    # The booking times of a queue session (no slot length of its own), in minutes (Saged, 2026-11-13).
    "booking_step_minutes": 60,
    # An optional deposit taken with a booking (Saged, 2026-11-16): off by default; the amount in piastres.
    "booking_deposit": False,
    "booking_deposit_amount": 0,
    # The consultation costs nothing when a procedure is done in the same visit (Saged, 2026-11-19): off by default.
    "consultation_free_with_procedure": False,
}
# Money policies only the account's owner sets (Saged, 2026-11-19), even for someone with settings.manage.
OWNER_ONLY = {"booking_deposit", "booking_deposit_amount", "consultation_free_with_procedure"}
MAX_DEPOSIT = 100_000_00  # EGP 100,000

_RULES = {
    "follow_up_days": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 90,
    "digits": lambda v: v in ("latn", "arab"),
    "print_language": lambda v: v in ("ar", "en"),
    "require_mfa": lambda v: isinstance(v, bool),
    "letterhead": lambda v: isinstance(v, bool),
    "letterhead_top_mm": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 120,
    "letterhead_bottom_mm": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 120,
    "solo": lambda v: isinstance(v, bool),
    "booking_step_minutes": lambda v: isinstance(v, int) and not isinstance(v, bool) and 10 <= v <= 120 and v % 5 == 0,
    "booking_deposit": lambda v: isinstance(v, bool),
    "booking_deposit_amount": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_DEPOSIT,
    "consultation_free_with_procedure": lambda v: isinstance(v, bool),
}


def effective(stored):
    values = dict(DEFAULTS)
    values.update({key: value for key, value in (stored or {}).items() if key in DEFAULTS})
    return values


def validated(changes, kind="clinic"):
    """The changes, checked against the registry. `kind` is the place's: a center is never solo."""
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ApiError("invalid", "Unknown settings: " + ", ".join(sorted(unknown)))
    bad = [key for key, value in changes.items() if not _RULES[key](value)]
    if bad:
        raise ApiError("invalid", "Invalid settings: " + ", ".join(sorted(bad)), fields={key: "invalid" for key in bad})
    if changes.get("solo") and kind == "center":
        raise ApiError("invalid", _("A medical center can't be run by one person alone."), fields={"solo": "center"})
    return changes


def check_together(values):
    """Rules across settings, on the values as they would be stored: a deposit that is asked for has an amount."""
    if values["booking_deposit"] and values["booking_deposit_amount"] <= 0:
        raise ApiError("invalid", _("Enter the deposit amount."), fields={"booking_deposit_amount": "required"})


def deposit(clinic):
    """The booking deposit this place asks for, in piastres; 0 when it asks for none."""
    values = effective(clinic.settings)
    return values["booking_deposit_amount"] if values["booking_deposit"] else 0
