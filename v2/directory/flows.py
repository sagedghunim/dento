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


def clean_sections(kind, sections, language="ar"):
    """Validate a place's kind and sections (design D1, D2): a clinic has exactly one, a center 2 to 20.

    `sections` is a list of {"specialty": code, "name": optional}. A section without a name takes its
    specialty's name. Names must differ. Returns [{"specialty", "name"}].
    """
    if kind not in ("clinic", "center"):
        raise ApiError("invalid", _("Choose a clinic or a medical center."), fields={"kind": "choice"})
    if not isinstance(sections, list):
        raise ApiError("invalid", _("Pick the specialty of each section."), fields={"sections": "required"})
    low, high = (1, 1) if kind == "clinic" else (2, MAX_SECTIONS)
    if not low <= len(sections) <= high:
        raise ApiError("invalid", _("A clinic has one specialty; a medical center has 2 to 20 sections."),
                       fields={"sections": "count"})
    cleaned, seen = [], set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict) or section.get("specialty") not in SPECIALTIES:
            raise ApiError("invalid", _("Pick a specialty from the list."), fields={f"sections.{index}.specialty": "choice"})
        name = section.get("name")
        if name is not None and not isinstance(name, str):
            raise ApiError("invalid", "name must be text", fields={f"sections.{index}.name": "text"})
        name = clean_name(name or "") or specialty_name(section["specialty"], language)
        if len(name) > 120:
            raise ApiError("invalid", "name is too long", fields={f"sections.{index}.name": "too_long"})
        if name.casefold() in seen:
            raise ApiError("invalid", _("Give each section its own name."), fields={f"sections.{index}.name": "duplicate"})
        seen.add(name.casefold())
        cleaned.append({"specialty": section["specialty"], "name": name})
    return cleaned


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
            existing = [Section.objects.create(clinic=clinic, name=section["name"], specialty_code=section["specialty"],
                                               sort=sort) for sort, section in enumerate(sections)]
        owner_membership = Membership.objects.filter(clinic=clinic, role="owner").first()
        if owner_membership is None:
            owner_membership = Membership.objects.create(clinic=clinic, user_id=owner.id, role="owner",
                                                         display_name=owner.full_name, title="")
        for section in existing:
            copy_services(clinic, section, language)
        for counter in ("patient_file", "invoice", "prescription"):
            Counter.objects.get_or_create(clinic=clinic, name=counter)
        if default_session and not Schedule.objects.filter(clinic_id=clinic.id, doctor=owner_membership).exists():
            Schedule.objects.bulk_create([
                Schedule(clinic_id=clinic.id, doctor=owner_membership, section=existing[0], weekday=weekday,
                         starts=DEFAULT_SESSION["starts"], ends=DEFAULT_SESSION["ends"], mode=DEFAULT_SESSION["mode"])
                for weekday in DEFAULT_SESSION["weekdays"]])
        return owner_membership.id


def copy_services(clinic, section, language="ar"):
    """Add the section's specialty template services it doesn't have yet. Never deletes or changes any (design 5.8)."""
    template = SPECIALTIES[section.specialty_code]["services"]
    existing = set(Service.objects.filter(clinic=clinic, section=section).values_list("name", flat=True))
    for sort, (arabic, english, kind, price, minutes) in enumerate(template):
        name = arabic if language == "ar" else english
        if name in existing:
            continue
        Service.objects.create(clinic=clinic, section=section, name=name, kind=kind, price=price, duration_min=minutes,
                               sort=sort)
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
