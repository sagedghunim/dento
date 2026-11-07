"""The clinic settings registry (design 4.2): every policy a clinic may change, with its default."""

from core.errors import ApiError

DEFAULTS = {
    "follow_up_days": 14,       # the follow-up window (R4.1, design 5.1)
    "digits": "latn",           # "latn" (0-9) or "arab" (٠-٩) on screens and prints
    "print_language": "ar",     # default language of receipts and prescriptions
    "require_mfa": False,       # two-step sign-in for every member, not only the owner
    "letterhead": False,        # pre-printed paper: no printed header
    "letterhead_top_mm": 0,
    "letterhead_bottom_mm": 0,
}

_RULES = {
    "follow_up_days": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 90,
    "digits": lambda v: v in ("latn", "arab"),
    "print_language": lambda v: v in ("ar", "en"),
    "require_mfa": lambda v: isinstance(v, bool),
    "letterhead": lambda v: isinstance(v, bool),
    "letterhead_top_mm": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 120,
    "letterhead_bottom_mm": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 120,
}


def effective(stored):
    values = dict(DEFAULTS)
    values.update({key: value for key, value in (stored or {}).items() if key in DEFAULTS})
    return values


def validated(changes):
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ApiError("invalid", "Unknown settings: " + ", ".join(sorted(unknown)))
    bad = [key for key, value in changes.items() if not _RULES[key](value)]
    if bad:
        raise ApiError("invalid", "Invalid settings: " + ", ".join(sorted(bad)), fields={key: "invalid" for key in bad})
    return changes
