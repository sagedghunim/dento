"""Field encryption for two-factor secrets, and hashing for recovery codes (design 5.9)."""

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    if not settings.FIELD_KEY:
        raise ImproperlyConfigured("HAMADA_FIELD_KEY is not set.")
    return Fernet(settings.FIELD_KEY.encode())


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ImproperlyConfigured("A stored secret can't be decrypted with HAMADA_FIELD_KEY.")


def new_recovery_code():
    """Ten characters from an unambiguous alphabet, shown as xxxxx-xxxxx."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def hash_recovery_code(code):
    # Codes carry about 49 bits of randomness, so a keyed fast hash is enough.
    cleaned = code.strip().lower().replace("-", "").replace(" ", "")
    return hmac.new(settings.FIELD_KEY.encode(), cleaned.encode(), hashlib.sha256).hexdigest()
