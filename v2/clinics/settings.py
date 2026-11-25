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
    # Or a percent of the visit's fee (Saged, 2026-11-19): 0 means the fixed amount above.
    "booking_deposit_percent": 0,
    # The consultation costs nothing when a procedure is done in the same visit (Saged, 2026-11-19): off by default.
    "consultation_free_with_procedure": False,
    # Installments (Saged, 2026-11-19): what a new plan starts with; the desk or the doctor changes it per patient.
    "installment_down_percent": 30,
    "installment_every": "month",
    "installment_count": 3,
    # What waits for the owner's permission when someone else does it (Saged, 2026-11-25): "cash" money out of the
    # drawer, "line" a line removed from a bill, "price" a price changed. Not listed: it happens and the owner gets
    # an alert.
    "approvals": [],
    # Prepared orders go to the supplier by themselves (design H1): off; does nothing until a WhatsApp provider is set.
    "orders_auto_send": False,
    # The WhatsApp text of a purchase order (Saged, 2026-11-25), with {supplier}, {clinic} and {items} (or their
    # Arabic names); "" is the built-in text (inventory.orders.default_message).
    "order_message": "",
}
# Money policies only the account's owner sets (Saged, 2026-11-19), even for someone with settings.manage.
OWNER_ONLY = {"booking_deposit", "booking_deposit_amount", "booking_deposit_percent", "consultation_free_with_procedure",
              "installment_down_percent", "installment_every", "installment_count", "approvals",
              "orders_auto_send", "order_message"}
MAX_DEPOSIT = 100_000_00  # EGP 100,000
ORDER_MESSAGE_MAX = 1000
# Every action someone does that can wait (Saged, 2026-11-25: "بإذن" on everything). Not here: money put into the
# drawer, the register closing short or by itself, and a file changed after its visit (events, not requests).
APPROVAL_KINDS = ("cash", "line", "price", "discount", "refund", "void", "expense", "patient", "stock")

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
    "booking_deposit_percent": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100,
    "installment_down_percent": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 90,
    "installment_every": lambda v: v in ("week", "2weeks", "month"),
    "installment_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 24,
    "orders_auto_send": lambda v: isinstance(v, bool),
    "order_message": lambda v: isinstance(v, str) and len(v) <= ORDER_MESSAGE_MAX,
    "approvals": lambda v: isinstance(v, list) and len(set(v)) == len(v) and all(k in APPROVAL_KINDS for k in v),
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
    if values["booking_deposit"] and values["booking_deposit_amount"] <= 0 and not values["booking_deposit_percent"]:
        raise ApiError("invalid", _("Enter the deposit amount."), fields={"booking_deposit_amount": "required"})


def deposit(clinic, fee=None):
    """The booking deposit this place asks for, in piastres; 0 when it asks for none. A percent deposit is that
    percent of the visit's `fee`, to the nearest pound (0 without a fee)."""
    values = effective(clinic.settings)
    if not values["booking_deposit"]:
        return 0
    percent = values["booking_deposit_percent"]
    if percent:
        return (fee * percent + 5000) // 10000 * 100 if fee else 0
    return values["booking_deposit_amount"]


def deposit_percent(clinic):
    """The percent when the deposit is a percent of the fee, else 0."""
    values = effective(clinic.settings)
    return values["booking_deposit_percent"] if values["booking_deposit"] else 0
