"""Flows that touch the directory and a shard (design 5.12).

There is no cross-database transaction. Each flow is a sequence of steps that are
safe to run again: a step that finds its row already there leaves it alone.
"""

import datetime as dt
import secrets

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import User
from clinics.models import Clinic, Counter, Membership, Schedule, Section, Service
from clinics.specialties import SPECIALTIES
from core.errors import ApiError
from core.routing import clinic_transaction
from core.text import clean_name

from .models import DirectoryClinic, UserClinic
from .services import least_full_shard

TEMP_PASSWORD_HOURS = 72
MAX_SECTIONS = 20
# A new place can book from day one: the owner works Saturday to Thursday, 17:00-23:00, as a queue (D2).
DEFAULT_SESSION = {"weekdays": (5, 6, 0, 1, 2, 3), "starts": dt.time(17, 0), "ends": dt.time(23, 0), "mode": "queue"}


def temporary_password():
    return secrets.token_urlsafe(9)


def create_account(username, full_name, *, phone=None, password=None, language="ar"):
    """A new account with a temporary password it must change (72 hours) unless a password is given."""
    if User.objects.filter(username=username.strip().lower()).exists():
        raise ApiError("username_taken", _("That username is taken."), 409, fields={"username": "taken"})
    temporary = password is None
    user = User.objects.create_user(
        username, password=password or temporary_password(), full_name=full_name, phone=phone or None, language=language,
        must_change_password=temporary,
        temp_password_expires_at=timezone.now() + dt.timedelta(hours=TEMP_PASSWORD_HOURS) if temporary else None,
    )
    return user


def specialty_name(code, language="ar"):
    names = SPECIALTIES[code]["name"]
    return names.get(language, names["en"])


def _expand(sections, language):
    """[{specialty, count, name?}] into one {specialty, name, index} per section (design E2).

    A count of N gives "<name> 1" ... "<name> N", a count of 1 just the name; the name defaults to the
    specialty's. One entry per section with count 1 and its own name (what the sign-up screen sends) keeps
    each edited name exactly.
    """
    if not isinstance(sections, list) or not sections:
        raise ApiError("invalid", _("Pick the specialty of each section."), fields={"sections": "required"})
    expanded = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict) or section.get("specialty") not in SPECIALTIES:
            raise ApiError("invalid", _("Pick a specialty from the list."), fields={f"sections.{index}.specialty": "choice"})
        count = section.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_SECTIONS:
            raise ApiError("invalid", _("Each specialty has 1 to 20 sections."), fields={f"sections.{index}.count": "range"})
        name = section.get("name")
        if name is not None and not isinstance(name, str):
            raise ApiError("invalid", "name must be text", fields={f"sections.{index}.name": "text"})
        typed = clean_name(name or "")
        base = typed or specialty_name(section["specialty"], language)
        # A default name gets both languages; a typed one is kept as typed (design G3).
        base_en = "" if typed else specialty_name(section["specialty"], "en")
        for number in range(1, count + 1):
            expanded.append({"specialty": section["specialty"], "name": base if count == 1 else f"{base} {number}",
                             "name_en": (base_en if count == 1 else f"{base_en} {number}") if base_en else "",
                             "index": index})
    return expanded


def _check_place(sections):
    if len(sections) > MAX_SECTIONS:
        raise ApiError("invalid", _("A place has at most 20 sections."), fields={"sections": "count"})
    seen = set()
    for section in sections:
        if len(section["name"]) > 120:
            raise ApiError("invalid", "name is too long", fields={f"sections.{section['index']}.name": "too_long"})
        if section["name"].casefold() in seen:
            raise ApiError("invalid", _("Give each section its own name."),
                           fields={f"sections.{section['index']}.name": "duplicate"})
        seen.add(section["name"].casefold())
    return [{"specialty": s["specialty"], "name": s["name"], "name_en": s.get("name_en", "")} for s in sections]


def plan_places(kind, sections, language="ar", layout="one_place"):
    """Validate the sign-up cards (design D2, E2) and return the places to create, each a list of sections.

    A clinic has exactly one section. A center has at least 2 in all. With layout "one_place" they are one
    center; with "branches" each specialty becomes its own place under the same owner, with that card's
    sections (a place with one section is a clinic). A place has at most 20 sections, with distinct names.
    """
    if kind not in ("clinic", "center"):
        raise ApiError("invalid", _("Choose a clinic or a medical center."), fields={"kind": "choice"})
    if layout not in ("one_place", "branches"):
        raise ApiError("invalid", _("Choose one place or separate branches."), fields={"layout": "choice"})
    expanded = _expand(sections, language)
    if (kind == "clinic" and len(expanded) != 1) or (kind == "center" and len(expanded) < 2):
        raise ApiError("invalid", _("A clinic has one specialty; a medical center has 2 to 20 sections."),
                       fields={"sections": "count"})
    if kind == "clinic" or layout == "one_place":
        return [_check_place(expanded)]
    groups = {}
    for section in expanded:
        groups.setdefault(section["specialty"], []).append(section)
    return [_check_place(group) for group in groups.values()]


def clean_sections(kind, sections, language="ar"):
    """One place's sections (design D1): a clinic has exactly one, a center 2 to 20. Returns [{"specialty", "name"}]."""
    return plan_places(kind, sections, language)[0]


def create_planned(*, name, owner, kind, places, city="", phone="", language="ar", default_session=False, shard=None):
    """Create places already validated by plan_places. Branches are named "<name> - <specialty>", each a clinic or
    a center by its number of sections."""
    entries = []
    for place in places:
        branch_kind = kind if len(places) == 1 else ("center" if len(place) > 1 else "clinic")
        place_name = name if len(places) == 1 else f"{name} - {specialty_name(place[0]['specialty'], language)}"[:120]
        entries.append(create_clinic(name=place_name, owner=owner, kind=branch_kind, sections=place, city=city,
                                     phone=phone, language=language, default_session=default_session, shard=shard))
    return entries


def create_clinic(*, name, owner, specialty_code=None, kind="clinic", sections=None, city="", phone="", language="ar",
                  shard=None, timezone_name="Africa/Cairo", default_session=False):
    """Create a place with its owner. `owner` is an existing User (the platform admin or sign-up creates it first).

    A clinic takes `specialty_code` (or one section); a center takes `sections` (design D1). Steps:
    1 the directory row; 2 the place on its shard with its sections, the owner's membership, each
    section's services and, with `default_session`, the owner's first sessions; 3 the owner's picker
    row. Returns the DirectoryClinic.
    """
    if sections is None:
        sections = [{"specialty": specialty_code}]
    sections = clean_sections(kind, sections, language)
    if owner.is_platform_admin:
        raise ApiError("invalid", "Platform admins can't be clinic members.", fields={"owner": "platform_admin"})
    with transaction.atomic(using="default"):
        # The directory's copy of the kind and time zone (the day end starts at the same default as the clinic's).
        entry = DirectoryClinic.objects.create(shard=shard or least_full_shard(), name=name, kind=kind,
                                               timezone=timezone_name)
    membership_id = ensure_clinic_on_shard(entry, sections, owner, kind=kind, city=city, phone=phone, language=language,
                                           timezone_name=timezone_name, default_session=default_session)
    UserClinic.objects.get_or_create(user=owner, clinic=entry, defaults={"membership_id": membership_id, "role": "owner"})
    return entry


def ensure_clinic_on_shard(entry, sections, owner, *, kind="clinic", city="", phone="", language="ar",
                           timezone_name="Africa/Cairo", default_session=False):
    """Step 2, idempotent by clinic id: a step that finds its rows leaves them alone. Returns the owner's membership id."""
    with clinic_transaction(entry.id, entry.shard):
        clinic = Clinic.objects.filter(id=entry.id).first()
        if clinic is None:
            clinic = Clinic.objects.create(id=entry.id, name=entry.name, kind=kind, city=city, phone=phone,
                                           specialty_code=sections[0]["specialty"], timezone=timezone_name,
                                           settings={"print_language": language})
        existing = list(Section.objects.filter(clinic=clinic).order_by("sort", "id"))
        if not existing:
            existing = [Section.objects.create(clinic=clinic, name=section["name"], name_en=section.get("name_en", ""),
                                               specialty_code=section["specialty"], sort=sort)
                        for sort, section in enumerate(sections)]
        owner_membership = Membership.objects.filter(clinic=clinic, role="owner").first()
        if owner_membership is None:
            owner_membership = Membership.objects.create(clinic=clinic, user_id=owner.id, role="owner",
                                                         display_name=owner.full_name, title="")
        for section in existing:
            copy_services(clinic, section, language)
            copy_catalogues(clinic, section, language)
        for counter in ("patient_file", "invoice", "prescription"):
            Counter.objects.get_or_create(clinic=clinic, name=counter)
        from finance.services import seed_categories

        seed_categories(clinic.id, language)  # the default expense categories (F3)
        if default_session and not Schedule.objects.filter(clinic_id=clinic.id, doctor=owner_membership).exists():
            Schedule.objects.bulk_create([
                Schedule(clinic_id=clinic.id, doctor=owner_membership, section=existing[0], weekday=weekday,
                         starts=DEFAULT_SESSION["starts"], ends=DEFAULT_SESSION["ends"], mode=DEFAULT_SESSION["mode"])
                for weekday in DEFAULT_SESSION["weekdays"]])
        return owner_membership.id


def copy_catalogues(clinic, section, language="ar"):
    """The section's procedures, medicines and prescription templates, from its specialty (design E3, E4)."""
    from clinical.services import copy_specialty

    copy_specialty(clinic.id, section, language)


def copy_services(clinic, section, language="ar"):
    """Add the section's specialty template services it doesn't have yet. Never deletes or changes any (design 5.8)."""
    template = SPECIALTIES[section.specialty_code]["services"]
    existing = set(Service.objects.filter(clinic=clinic, section=section).values_list("name", flat=True))
    for sort, (arabic, english, kind, price, minutes) in enumerate(template):
        # Both languages (design G3); an English place types English, so its name is the English one.
        name = arabic if language == "ar" else english
        if arabic in existing or english in existing:
            continue
        Service.objects.create(clinic=clinic, section=section, name=name, name_en=english, kind=kind, price=price,
                               duration_min=minutes, sort=sort)
    # A consultation's follow-up is the first follow-up service of the same section.
    follow_up = (Service.objects.filter(clinic=clinic, section=section, kind="follow_up", is_active=True)
                 .order_by("sort", "id").first())
    if follow_up:
        Service.objects.filter(clinic=clinic, section=section, kind="consultation", follow_up_service__isnull=True).update(
            follow_up_service=follow_up)


def add_member(entry, user, role, display_name, title=""):
    """Attach an account this flow created (design 5.12, B6): shard membership, then the picker row."""
    if user.is_platform_admin:
        raise ApiError("invalid", "Platform admins can't be clinic members.")
    with clinic_transaction(entry.id, entry.shard):
        membership, _ = Membership.objects.get_or_create(
            clinic_id=entry.id, user_id=user.id, defaults={"role": role, "display_name": display_name, "title": title})
    UserClinic.objects.get_or_create(user=user, clinic=entry, defaults={"membership_id": membership.id, "role": role})
    return membership
