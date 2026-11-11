"""Clinics and centers as the screens show them (design Appendix D).

Until the sections backend lands, a place without `kind` is a clinic and a place without
sections has one implied section, so every screen hides the section choices.
"""

from django.utils.translation import get_language
from django.utils.translation import gettext as _

from clinics.specialties import SPECIALTIES

from . import format as fmt


def specialty_name(code, language=None):
    spec = SPECIALTIES.get(code)
    if not spec:
        return code or ""
    language = (language or get_language() or "ar")[:2]
    return spec["name"].get(language) or spec["name"]["en"]


def specialty_choices(language=None):
    """For the sign-up cards: code, name and a few of the template's services in this language."""
    language = (language or get_language() or "ar")[:2]
    index = 0 if language == "ar" else 1
    return [{"code": code, "name": specialty_name(code, language),
             "services": [service[index] for service in spec["services"] if service[2] != "follow_up"][:3]}
            for code, spec in SPECIALTIES.items()]


def is_center(clinic):
    return getattr(clinic, "kind", "clinic") == "center"


def sections(ctx):
    """The place's active sections in order; [] while the backend has no sections."""
    try:
        from clinics.models import Section
    except ImportError:
        return []
    found = Section.objects.filter(clinic_id=ctx.clinic_id, is_active=True).order_by("sort", "id")
    return [{"id": s.id, "name": s.name, "specialty": s.specialty_code} for s in found]


def describe(ctx):
    clinic = ctx.clinic
    center = is_center(clinic)
    if center:
        kind = _("Medical center")
        line = _("Sections: %(count)s") % {"count": fmt.digits(len(sections(ctx)))}
    else:
        kind = _("Clinic")
        line = _("%(specialty)s clinic") % {"specialty": specialty_name(clinic.specialty_code)}
    if clinic.city:
        line = f"{line} · {clinic.city}"
    return {"center": center, "kind": kind, "line": line}
