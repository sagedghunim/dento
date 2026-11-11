"""The platform admin (design 5.12, R4.11): clinics and owners, never patient data."""

from django.db import transaction
from django.utils.translation import gettext as _

from accounts.models import User
from clinics.models import Clinic
from clinics.specialties import SPECIALTIES
from core.api import SESSION_MFA
from core.errors import ApiError
from core.routing import clinic_transaction
from core.text import is_valid_phone, normalize_phone

from .flows import clean_sections, create_account, create_clinic
from .models import DirectoryClinic, UserClinic


def require_platform_admin(request):
    """A platform admin with two-step sign-in done in this session."""
    user = request.user
    if not user.is_authenticated:
        raise ApiError("signed_out", _("Please sign in."), 401)
    if not user.is_platform_admin:
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)
    if user.must_change_password:
        raise ApiError("password_change_required", _("Change your password to continue."), 403)
    if not user.has_totp:
        raise ApiError("mfa_setup_required", _("Set up two-step sign-in to continue."), 403)
    if not request.session.get(SESSION_MFA):
        raise ApiError("mfa_required", _("Enter the code from your authenticator app."), 403)
    return user


def clinic_rows(limit=100, after=None):
    """Directory facts only: name, shard, status, members. Usage counts come from a job later (design 5.12)."""
    rows = DirectoryClinic.objects.order_by("-id")
    if after:
        rows = rows.filter(id__lt=after)
    rows = list(rows[: limit + 1])
    members = {}
    for clinic_id, is_active in UserClinic.objects.filter(clinic_id__in=[r.id for r in rows[:limit]]).values_list("clinic_id", "is_active"):
        if is_active:
            members[clinic_id] = members.get(clinic_id, 0) + 1
    items = [{"id": r.id, "name": r.name, "kind": r.kind, "shard": r.shard, "status": r.status,
              "members": members.get(r.id, 0),
              "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows[:limit]]
    return {"items": items, "next": str(rows[limit - 1].id) if len(rows) > limit else None}


def create(name, specialty, city, owner_username, owner_name, owner_phone, language, kind="clinic", sections=None):
    """A place with its owner: a clinic with one specialty, or a center with `sections` (design D1, D2)."""
    if sections is None:
        if specialty not in SPECIALTIES:
            raise ApiError("invalid", _("Pick a specialty from the list."), fields={"specialty": "choice"})
        sections = [{"specialty": specialty}]
    sections = clean_sections(kind, sections, language)
    phone = normalize_phone(owner_phone) if owner_phone else None
    if phone and not is_valid_phone(phone):
        raise ApiError("invalid", _("Enter a valid mobile number."), fields={"owner_phone": "phone"})
    if phone and User.objects.filter(phone=phone).exists():
        raise ApiError("phone_taken", _("That mobile number already has an account."), 409, fields={"owner_phone": "taken"})
    with transaction.atomic(using="default"):
        owner = create_account(owner_username, owner_name, phone=phone, language=language)
        temporary = _temporary_password(owner)
    entry = create_clinic(name=name, owner=owner, kind=kind, sections=sections, city=city, language=language,
                          default_session=True)
    return entry, owner, temporary


def _temporary_password(owner):
    """create_account set a random temporary password; set a known one to hand to the owner once."""
    from .flows import temporary_password

    password = temporary_password()
    owner.set_password(password)
    owner.save(update_fields=["password"])
    return password


def set_status(clinic_id, status):
    """Directory first, then the shard (design 5.12). Requests check the shard's status."""
    entry = DirectoryClinic.objects.filter(id=clinic_id).first()
    if entry is None:
        raise ApiError("not_found", _("Clinic not found."), 404)
    if entry.status == DirectoryClinic.Status.MOVING:
        raise ApiError("busy", _("The clinic is being moved. Try again later."), 409, retry_after=60)
    DirectoryClinic.objects.filter(id=clinic_id).update(status=status)
    with clinic_transaction(entry.id, entry.shard):
        Clinic.objects.filter(id=entry.id).update(status=status)
    entry.status = status
    return entry
