"""The one hook for sending WhatsApp messages through a provider (design H1; Confirmed EG later).

Env: WHATSAPP_PROVIDER, WHATSAPP_TOKEN, WHATSAPP_SENDER. While they are unset nothing is sent: `send` returns None and
the page opens a wa.me link instead, which the person sends from their own phone.
"""

import os
from urllib.parse import quote


def configured():
    return bool(os.environ.get("WHATSAPP_PROVIDER") and os.environ.get("WHATSAPP_TOKEN"))


def send(phone, template, params):
    """Send one message; returns the provider's id, or None when no provider is set. Raises when the provider fails."""
    if not configured():
        return None
    raise NotImplementedError(f"WhatsApp provider {os.environ.get('WHATSAPP_PROVIDER')!r} isn't wired yet")


def link(phone, text):
    """A wa.me link that opens the chat with the message written; without a number WhatsApp asks whom to send it to."""
    return f"https://wa.me/{phone or ''}?text={quote(text)}"
