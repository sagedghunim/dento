"""Template filters for the clinic's formats (design 7.2) and the side navigation."""

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from web import format as fmt
from web import nav

register = template.Library()


@register.filter
def digits(value):
    return fmt.digits(value)


@register.filter
def money(piastres):
    return fmt.money(piastres)


@register.filter
def phone(value):
    """A phone in its own left-to-right island, so its digit groups never reverse (design 7.2)."""
    if not value:
        return ""
    return format_html('<bdi class="ltr num nowrap">{}</bdi>', fmt.phone(value))


@register.filter
def whatsapp(value):
    return fmt.whatsapp(value)


@register.filter
def clock(value):
    return fmt.clock(value)


@register.filter
def longday(value):
    return fmt.day(value)


@register.filter
def shortday(value):
    return fmt.day(value, with_weekday=False)


@register.filter
def num(value):
    """A number isolated left to right (file, turn and invoice numbers)."""
    if value in (None, ""):
        return ""
    return format_html('<bdi class="ltr num">{}</bdi>', fmt.digits(value))


@register.filter
def late_days(n):
    """How late a follow-up is (Saged, 2026-11-24), counted as Arabic counts days; book.js lateText says the same."""
    if not n:
        return ""
    if n == 1:
        return _("A day late")
    if n == 2:
        return _("Two days late")
    if 3 <= n % 100 <= 10:
        return _("{n} days late").format(n=fmt.digits(n))
    return _("Late by {n} days").format(n=fmt.digits(n))


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(key, "")


@register.simple_tag
def nav_sections(ctx):
    return nav.visible(ctx) if ctx is not None else []


@register.simple_tag
def alerts_unseen(ctx):
    """The owner's new alerts, for the side bar's count (web/alerts.py)."""
    from web import alerts

    return alerts.unseen(ctx) if ctx is not None else 0


@register.simple_tag
def order_drafts(ctx):
    """Purchase orders waiting to be sent plus items nobody will bring, for the side bar (design H1 and H1 backup):
    one count on each partial index."""
    if ctx is None or not ctx.can("inventory.order"):
        return 0
    from inventory import orders

    return orders.draft_count(ctx) + orders.need_count(ctx)


@register.simple_tag
def money_pattern():
    """For scripts. {% translate %} escapes "%" in literals, so it can't look this one up."""
    from django.utils.translation import gettext as _

    return _("EGP %(amount)s")


@register.simple_tag
def app_version():
    from django.conf import settings

    return settings.APP_VERSION


@register.simple_tag
def digit_setting():
    return fmt.digit_setting()


@register.simple_tag
def business_day(ctx):
    from core.days import business_day as day_of

    return day_of(ctx.clinic) if ctx is not None else None


@register.simple_tag
def place(ctx):
    """What the side panel says about the place: clinic or center, its specialty or sections, its city."""
    from web.places import describe

    return describe(ctx)


@register.filter
def role_label(role):
    from django.utils.translation import gettext as _

    return {"owner": _("Owner"), "doctor": _("Doctor"), "assistant": _("Assistant")}.get(role, role)
