"""Flows that touch the directory and a shard (design 5.12).

There is no cross-database transaction. Each flow is a sequence of steps that are
safe to run again: a step that finds its row already there leaves it alone.
"""

import datetime as dt
import secrets

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from clinics.models import Clinic, Counter, Membership, Service
from clinics.specialties import SPECIALTIES
from core.errors import ApiError
from core.routing import clinic_transaction

from .models import DirectoryClinic, UserClinic
from .services import least_full_shard

TEMP_PASSWORD_HOURS = 72


def temporary_password():
    return secrets.token_urlsafe(9)


def create_account(username, full_name, *, phone=None, password=None, language="ar"):
    """A new account with a temporary password it must change (72 hours) unless a password is given."""
    if User.objects.filter(username=username.strip().lower()).exists():
        raise ApiError("username_taken", "That username is taken.", 409, fields={"username": "taken"})
    temporary = password is None
    user = User.objects.create_user(
        username, password=password or temporary_password(), full_name=full_name, phone=phone or None, language=language,
        must_change_password=temporary,
        temp_password_expires_at=timezone.now() + dt.timedelta(hours=TEMP_PASSWORD_HOURS) if temporary else None,
    )
    return user


def create_clinic(*, name, specialty_code, owner, city="", language="ar", shard=None, timezone_name="Africa/Cairo"):
    """Create a clinic with its owner. `owner` is an existing User (the platform admin creates it first).

    Steps: 1 the directory row; 2 the clinic on its shard with the owner's membership and the
    specialty's services; 3 the owner's picker row. Returns the DirectoryClinic.
    """
    if specialty_code not in SPECIALTIES:
        raise ApiError("invalid", "Unknown specialty.", fields={"specialty": "choice"})
    if owner.is_platform_admin:
        raise ApiError("invalid", "Platform admins can't be clinic members.", fields={"owner": "platform_admin"})
    with transaction.atomic(using="default"):
        entry = DirectoryClinic.objects.create(shard=shard or least_full_shard(), name=name)
    membership_id = ensure_clinic_on_shard(entry, specialty_code, owner, city=city, language=language,
                                           timezone_name=timezone_name)
    UserClinic.objects.get_or_create(user=owner, clinic=entry, defaults={"membership_id": membership_id, "role": "owner"})
    return entry


def ensure_clinic_on_shard(entry, specialty_code, owner, *, city="", language="ar", timezone_name="Africa/Cairo"):
    """Step 2, idempotent by clinic id. Returns the owner's membership id."""
    with clinic_transaction(entry.id, entry.shard):
        clinic = Clinic.objects.filter(id=entry.id).first()
        if clinic is None:
            clinic = Clinic.objects.create(id=entry.id, name=entry.name, city=city, specialty_code=specialty_code,
                                           timezone=timezone_name, settings={"print_language": language})
        owner_membership = Membership.objects.filter(clinic=clinic, role="owner").first()
        if owner_membership is None:
            owner_membership = Membership.objects.create(clinic=clinic, user_id=owner.id, role="owner",
                                                         display_name=owner.full_name, title="")
        copy_services(clinic, specialty_code, language)
        for counter in ("patient_file", "invoice", "prescription"):
            Counter.objects.get_or_create(clinic=clinic, name=counter)
        return owner_membership.id


def copy_services(clinic, specialty_code, language="ar"):
    """Add the template's services the clinic doesn't have yet. Never deletes or changes any (design 5.8)."""
    template = SPECIALTIES[specialty_code]["services"]
    existing = set(Service.objects.filter(clinic=clinic).values_list("name", flat=True))
    created = {}
    for sort, (arabic, english, kind, price, minutes) in enumerate(template):
        name = arabic if language == "ar" else english
        if name in existing:
            continue
        created[kind, name] = Service.objects.create(clinic=clinic, name=name, kind=kind, price=price,
                                                     duration_min=minutes, sort=sort)
    follow_up = Service.objects.filter(clinic=clinic, kind="follow_up", is_active=True).order_by("sort", "id").first()
    if follow_up:
        Service.objects.filter(clinic=clinic, kind="consultation", follow_up_service__isnull=True).update(
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
