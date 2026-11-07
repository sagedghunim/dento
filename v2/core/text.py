"""Normalising names, digits and phone numbers (design 5.7)."""

import re
import unicodedata

# Arabic-Indic and Eastern Arabic-Indic digits, as typed on Arabic keyboards.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Letters that people write interchangeably in names.
_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})
# Harakat, Quranic marks, superscript alef and tatweel.
_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_EGYPT_MOBILE = re.compile(r"^01[0125]\d{8}$")


def ascii_digits(value):
    """Return `value` with Arabic digits replaced by 0-9."""
    return (value or "").translate(_DIGITS)


def normalize_name(value):
    """The search form of a name: the same for every common way of writing it."""
    text = unicodedata.normalize("NFKC", value or "")
    text = _MARKS.sub("", text).translate(_LETTERS).translate(_DIGITS).casefold()
    return " ".join(text.split())


def clean_name(value):
    """The display form: spaces collapsed, nothing else changed."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def normalize_phone(value):
    """Digits only. Egyptian numbers in their national form (01xxxxxxxxx).

    Returns "" for empty input. Callers validate with `is_valid_phone`.
    """
    digits = "".join(ch for ch in ascii_digits(value) if ch.isdigit())
    if digits.startswith("0020"):
        digits = "0" + digits[4:]
    elif digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits


def is_valid_phone(phone_norm):
    """An Egyptian mobile number, or an international number of 8-15 digits."""
    if not phone_norm:
        return False
    if phone_norm.startswith("01"):
        return bool(_EGYPT_MOBILE.match(phone_norm))
    return 8 <= len(phone_norm) <= 15


def looks_like_phone(query):
    """A search query made only of digits, spaces, + and - is a phone or a file number."""
    text = ascii_digits(query).strip()
    return bool(text) and all(ch.isdigit() or ch in " +-" for ch in text) and any(ch.isdigit() for ch in text)
