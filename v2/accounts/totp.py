"""Time-based one-time passwords (RFC 6238), as used by authenticator apps.

Reused from the warehouse project (identity/totp.py).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from core.text import ascii_digits

STEP_SECONDS = 30
DIGITS = 6
# Phones whose clock runs a little fast or slow still sign in: codes up to two minutes either way are accepted
# (Saged, 2026-11-25, after a doctor's code was refused again and again).
WINDOW = 4
# Further off than that, the code is still refused, but the person is told their phone's clock is wrong.
DRIFT_WINDOW = 20


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret):
    cleaned = secret.strip().replace(" ", "").upper()
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned)


def code_at(secret, step, digits=DIGITS):
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return f"{number:0{digits}d}"


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP_SECONDS)


def verify(secret, code, *, last_step=0, now=None, window=WINDOW):
    """Return the time step the code belongs to, or None.

    Steps at or before `last_step` are refused, so a code can't be used twice.
    """
    code = clean(code)
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - window, step + window + 1):
        if candidate > last_step and hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def clean(code):
    """The digits of a code as people type or paste it: "123 456", "123-456", Arabic digits (Saged, 2026-11-24)."""
    return "".join(ascii_digits(code or "").split()).replace("-", "")


def used_recently(secret, code, *, last_step, now=None, window=WINDOW):
    """True when the code is right but its step was already used: on another phone, or a second tap."""
    code = clean(code)
    if len(code) != DIGITS or not code.isdigit():
        return False
    step = current_step(now)
    return any(candidate <= last_step and hmac.compare_digest(code_at(secret, candidate), code)
               for candidate in range(step - window, step + window + 1))


def clock_off_minutes(secret, code, *, now=None):
    """Minutes the phone's clock is off when a code is right for a time beyond the window, else None."""
    code = clean(code)
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    step = current_step(now)
    for distance in range(WINDOW + 1, DRIFT_WINDOW + 1):
        for candidate in (step - distance, step + distance):
            if hmac.compare_digest(code_at(secret, candidate), code):
                return max(1, round(distance * STEP_SECONDS / 60))
    return None


def provisioning_uri(secret, account, issuer="Dento"):
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
