"""Display formatting on the server (design 6.1, 7.2): the clinic's digits, money, phones, times and days.

The browser never converts a time zone or works out "today". Times arrive as ISO 8601 with the
clinic's offset (core.days.local_iso), so only their wall-clock part is shown. The clinic's digit
setting applies to everything a page renders for it, through `use()`.
"""

import contextlib
import contextvars
import datetime as dt

from django.utils import formats
from django.utils.html import format_html
from django.utils.translation import gettext as _

_digits = contextvars.ContextVar("hamada_digits", default="latn")
_ARABIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


@contextlib.contextmanager
def using(clinic):
    """Format for this clinic inside the block. Reset after, because a worker thread serves many requests."""
    from clinics import settings as clinic_settings

    token = _digits.set(clinic_settings.effective(clinic.settings)["digits"] if clinic is not None else "latn")
    try:
        yield
    finally:
        _digits.reset(token)


def digit_setting():
    return _digits.get()


def digits(value):
    """Western digits, or Arabic-Indic ones when the clinic prefers them."""
    text = "" if value is None else str(value)
    return text.translate(_ARABIC) if _digits.get() == "arab" else text


def amount_text(piastres):
    """1250050 -> "12,500.50"; whole pounds without decimals. Our own formatter: Intl's adds hidden marks."""
    piastres = int(piastres or 0)
    sign = "-" if piastres < 0 else ""
    pounds, rest = divmod(abs(piastres), 100)
    text = f"{pounds:,}" + (f".{rest:02d}" if rest else "")
    if _digits.get() == "arab":
        text = text.replace(",", "٬").replace(".", "٫").translate(_ARABIC)
    return sign + text


def money(piastres):
    """Safe HTML: the amount isolated left to right, the currency placed by the language (Arabic after it)."""
    number = format_html('<bdi class="ltr num">{}</bdi>', amount_text(piastres))
    pattern = _("EGP %(amount)s")
    before, _sep, after = pattern.partition("%(amount)s")
    return format_html('<span class="money">{}{}{}</span>', before, number, after)


def money_plain(piastres):
    return _("EGP %(amount)s") % {"amount": amount_text(piastres)}


def phone(value):
    """Egyptian mobiles in groups, 010 1234 5678; anything else as stored."""
    value = value or ""
    if len(value) == 11 and value.startswith("01") and value.isdigit():
        value = f"{value[:3]} {value[3:7]} {value[7:]}"
    return digits(value)


def _parse(value):
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.time):
        return dt.datetime.combine(dt.date(2000, 1, 1), value)
    text = str(value)
    if len(text) == 5 and text[2] == ":":  # a session's "17:00"
        return dt.datetime.combine(dt.date(2000, 1, 1), dt.time.fromisoformat(text))
    return dt.datetime.fromisoformat(text)


def _clock_parts(value, clinic=None):
    instant = _parse(value)
    if instant is None:
        return None
    if clinic is not None and instant.tzinfo is not None and not isinstance(value, str):
        from core.days import zone

        instant = instant.astimezone(zone(clinic))
    hour = instant.hour % 12 or 12
    return digits(f"{hour}:{instant.minute:02d}"), _("AM") if instant.hour < 12 else _("PM")


def clock_text(value, clinic=None):
    """The wall-clock time of an instant: "8:30 م" / "8:30 PM". ISO strings already carry the clinic's offset."""
    parts = _clock_parts(value, clinic)
    return f"{parts[0]} {parts[1]}" if parts else ""


def clock(value, clinic=None):
    """Safe HTML: the digits isolated left to right, the AM/PM marker after them in the reading order."""
    parts = _clock_parts(value, clinic)
    if parts is None:
        return ""
    return format_html('<span class="nowrap"><bdi class="ltr num">{}</bdi> {}</span>', *parts)


def day(value, with_weekday=True, with_year=True):
    """A business day in words, in the active language: "السبت 7 نوفمبر 2026"."""
    if isinstance(value, str):
        value = dt.date.fromisoformat(value[:10])
    if value is None:
        return ""
    pattern = ("l " if with_weekday else "") + "j F" + (" Y" if with_year else "")
    return digits(formats.date_format(value, pattern))


def age(years):
    return "" if years is None else digits(years)
