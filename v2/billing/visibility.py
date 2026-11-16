"""Who sees money (Saged, 2026-11-16): only holders of money.view.

A doctor employed by the place sees no amount anywhere unless the owner opens "Sees prices and money" for them. The
reads that every page and endpoint share leave the amounts out here, so a payload never carries an amount that a
screen merely hides: an amount becomes None, a list of payments is refused. Hidden by permission, never by role;
the owner holds every code.
"""

from django.utils.translation import gettext as _

from core.errors import ApiError

CODE = "money.view"


def sees(ctx):
    return ctx is not None and ctx.can(CODE)


def require(ctx):
    """For reads that are nothing but money (payments, balances, receipts): 403 without money.view."""
    if not sees(ctx):
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)


def appointment(ctx, body):
    """One appointment payload (scheduling.selectors.payload) without its fee, what is due and the invoice's sums.
    The invoice keeps its id and number only."""
    if body is None or sees(ctx):
        return body
    body["fee"] = None
    body["due"] = None
    if body.get("invoice"):
        body["invoice"] = {"id": body["invoice"]["id"], "number": body["invoice"]["number"]}
    return body


def fields(ctx, row, *names):
    """Set the named amounts of one dict to None for someone without money.view; returns the row."""
    if row is not None and not sees(ctx):
        for name in names:
            if name in row:
                row[name] = None
    return row


def rows(ctx, items, *names):
    if not sees(ctx):
        for row in items:
            fields(ctx, row, *names)
    return items
